"""MIDI control engine for viseq (REFACTOR_LATEST.md commit 10/13).

Binding resolution, the profile-driven controller runtime (connect,
disconnect, grid LED mirror, playhead flash) and the config mirrors.
WORKER-SAFE: no dpg import (HIGH-1) — the learn/action/UI routing and
the worker loops live in the composition root and call these.
"""

import contextlib
import threading
from typing import Any

from viseqapp import state
from viseqapp.config import load_config, save_config
from viseqapp.constants import (
    GRID_FLASH_SECONDS,
    GRID_LED_AMBER,
    GRID_LED_GREEN,
    GRID_LED_OFF,
    GRID_LED_WHITE,
    MIDI_ACTION_SEQ_TOGGLE,
)
from viseqapp.profiles import (
    _DEFAULT_GRID_NOTE_FORMULA,
    _grid_note,
    load_controller_profiles,
    match_controller_profile,
)
from viseqapp.queues import append_log, log_error
from viseqapp.state import (
    _controller_lock,
    midi_bindings,
    midi_controllers,
    midi_selected_port,
    tracks_data,
)


def binding_matches(binding: dict[str, Any], msg_type: str, number: int, channel: int) -> bool:
    """True when a parsed MIDI message (type, number, channel) matches the binding."""
    if binding.get("type") != msg_type:
        return False
    if int(binding.get("number", -1)) != number:
        return False
    return int(binding.get("channel", 0)) == channel


def _binding_device_ok(binding: dict[str, Any], port_name: str) -> bool:
    """A binding matches the port when its device is empty (wildcard) or equals it."""
    dev = binding.get("device")
    return not dev or dev == port_name or dev == "*"


def _parse_midi_msg(msg: Any) -> tuple[str | None, int, int]:
    """Map a mido message to (type, number, value); release edges and other types -> None.

    note_on velocity>0 is the trigger edge (velocity 0 and note_off are releases and must
    never fire a binding — Launchpad sends note_on with velocity 0 on release).
    """
    if msg.type == "note_on":
        if msg.velocity > 0:
            return ("note", int(msg.note), int(msg.velocity))
        return (None, 0, 0)
    if msg.type == "note_off":
        return (None, 0, 0)
    if msg.type == "control_change":
        return ("cc", int(msg.control), int(msg.value))
    return (None, 0, 0)


def resolve_midi_message(
    msg: Any, port_name: str, bindings: list[dict[str, Any]] | None = None
) -> list[tuple[str, dict[str, Any], int]]:
    """Bindings matching a raw mido message on the given port -> (action, params, value).

    bindings defaults to the legacy flat lists so pre-e14 paths and tests keep working;
    the worker passes per-controller lists (e14s02).
    """
    msg_type, number, value = _parse_midi_msg(msg)
    if msg_type is None:
        return []
    channel = int(getattr(msg, "channel", 0))
    if bindings is None:
        bindings = list(midi_bindings)
    out: list[tuple[str, dict[str, Any], int]] = []
    for binding in bindings:
        if not _binding_device_ok(binding, port_name):
            continue
        if binding_matches(binding, msg_type, number, channel):
            out.append((str(binding.get("action", "")), dict(binding.get("params") or {}), value))
    return out


def binding_source_from_message(msg: Any, port_name: str) -> dict[str, Any] | None:
    """The (device, channel, type, number) half of a binding from a message; releases -> None."""
    msg_type, number, _ = _parse_midi_msg(msg)
    if msg_type is None:
        return None
    return {"device": port_name, "channel": int(msg.channel), "type": msg_type, "number": number}


def available_controller_ports() -> list[str]:
    """MIDI input ports not yet added as controllers (e14s03)."""
    try:
        import mido

        names = list(mido.get_input_names())
    except Exception:
        names = []
    used = {controller["port"] for controller in midi_controllers}
    return [name for name in names if name not in used]


def save_midi_controllers(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Persist the controllers list (port, profile, role, per-controller bindings) (e14s03)."""
    cfg = load_config()
    cfg["midi"]["controllers"] = [
        {
            "port": controller["port"],
            "profile_id": controller.get("profile_id") or "",
            "role": controller.get("role"),
            "bindings": list(controller.get("bindings") or []),
        }
        for controller in midi_controllers
    ]
    save_config(cfg)


def selected_controller() -> dict[str, Any] | None:
    """The controller whose bindings the Bindings section edits (e14s03)."""
    if midi_selected_port is not None:
        controller = find_controller_by_port(midi_selected_port)
        if controller is not None:
            return controller
    return grid_controller() or (midi_controllers[0] if midi_controllers else None)


def selected_bindings() -> list[dict[str, Any]]:
    """The bindings list the Bindings section edits (controller or legacy) (e14s03)."""
    controller = selected_controller()
    if controller is not None:
        return controller.setdefault("bindings", [])
    return midi_bindings


def midi_init_from_config(cfg: dict[str, Any]) -> None:
    """Load the MIDI control mirrors from the config (boot; e09 -> e14s02 multi-controller)."""
    midi_cfg = cfg.get("midi") or {}
    state.midi_enabled = bool(midi_cfg.get("enabled", False))
    state.midi_clock_source = midi_cfg.get("clock_source") or None
    state._controller_profiles = load_controller_profiles()
    controllers_raw = midi_cfg.get("controllers")
    if not isinstance(controllers_raw, list) or not controllers_raw:
        controllers_raw = _migrate_legacy_controller(midi_cfg)
    rebuilt: list[dict[str, Any]] = []
    for raw in controllers_raw:
        if not isinstance(raw, dict) or not raw.get("port"):
            continue
        rebuilt.append(
            {
                "port": str(raw["port"]),
                "profile_id": str(raw.get("profile_id") or ""),
                "role": "grid" if raw.get("role") == "grid" else None,
                "bindings": list(raw.get("bindings") or []),
                "output": None,
                "auto_bindings": [],
            }
        )
    midi_controllers[:] = rebuilt
    # Legacy mirror so pre-e14 paths keep working until fully removed (e14s04).
    midi_bindings[:] = rebuilt[0]["bindings"] if rebuilt else []


def _migrate_legacy_controller(midi_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Legacy single-controller config (input_port + bindings) -> controllers[] (e14s02)."""
    port = midi_cfg.get("input_port") or None
    if not port:
        return []
    profile = match_controller_profile(port, controller_profiles())
    role = "grid" if profile and profile.get("features", {}).get("grid") else None
    return [
        {
            "port": str(port),
            "profile_id": profile["id"] if profile else "",
            "role": role,
            "bindings": list(midi_cfg.get("bindings") or []),
        }
    ]


def disconnect_all_controllers() -> None:
    """Close every controller's output and drop its auto bindings (e14)."""
    for controller in midi_controllers:
        controller_disconnect(controller)


def reconnect_all_controllers() -> None:
    """Re-open every controller's LED output and re-register its auto grid bindings.

    Called on MIDI re-enable: the worker keeps input ports open across a disable, so it
    would skip the connect step and leave the output closed and the grid bindings empty
    — pads would stop toggling steps (BUG-2026-08-29T102156 Defect A).
    """
    try:
        import mido
    except ImportError:
        return
    for controller in midi_controllers:
        controller_connect(controller, mido)


def set_midi_enabled(enabled: bool) -> None:
    """Enable/disable the MIDI control engine and persist the flag (main thread).

    Disabling closes every controller output so the device stops lighting up
    immediately (e14 bug fix); re-enabling reconnects the outputs and re-registers
    the auto grid bindings (BUG-2026-08-29T102156).
    """
    state.midi_enabled = enabled
    if not enabled:
        disconnect_all_controllers()
    else:
        reconnect_all_controllers()
    cfg = load_config()
    cfg["midi"]["enabled"] = enabled
    save_config(cfg)


def _close_midi_input(port: Any) -> None:
    """Close a midi input port, ignoring errors (e14s02)."""
    with contextlib.suppress(Exception):
        port.close()


def controller_profiles() -> dict[str, dict[str, Any]]:
    """The loaded controller profiles (lazy, cached; re-loadable for tests) (e14s02)."""
    if not state._controller_profiles:
        state._controller_profiles = load_controller_profiles()
    return state._controller_profiles


def grid_controller() -> dict[str, Any] | None:
    """The controller designated as the sequencer grid (role 'grid'), or None (e14s02)."""
    for controller in midi_controllers:
        if controller.get("role") == "grid":
            return controller
    return None


def find_controller_by_port(port_name: str) -> dict[str, Any] | None:
    """The controller bound to a MIDI input port name, or None (e14s02)."""
    for controller in midi_controllers:
        if controller["port"] == port_name:
            return controller
    return None


def controller_profile_of(controller: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a controller's profile dict by its profile id (e14s02)."""
    return controller_profiles().get(str(controller.get("profile_id") or ""))


def grid_note(controller: dict[str, Any], row: int, col: int) -> int:
    """Grid MIDI note for pad (row, col) under the controller's profile formula (e14s02)."""
    profile = controller_profile_of(controller)
    if profile is None:
        return eval(
            _DEFAULT_GRID_NOTE_FORMULA, {"__builtins__": {}}, {"row": row, "col": col, "int": int}
        )
    return _grid_note(profile, row, col)


def _controller_velocity(controller: dict[str, Any], color: str) -> int:
    """Semantic color -> velocity under the controller's profile palette (e14s02)."""
    profile = controller_profile_of(controller)
    if profile is None:
        return 0
    return int((profile.get("colors") or {}).get(color, 0))


def _find_output_port(input_name: str, mido: Any) -> str | None:
    """Output port matching the input name, else any port with the same brand, else first."""
    out_names = mido.get_output_names()
    if input_name in out_names:
        return input_name
    brand = _port_brand(input_name)
    for cand in out_names:
        if brand and brand in cand.lower():
            return cand
    return out_names[0] if out_names else None


def _port_brand(port_name: str) -> str | None:
    """First word of the port name (brand guess) lowercased, or None (e14s02)."""
    stripped = (port_name or "").strip()
    if not stripped:
        return None
    return stripped.split()[0].lower()


def _register_grid_bindings(controller: dict[str, Any], profile: dict[str, Any]) -> None:
    """Auto 8x8 grid bindings for a grid-role controller; never persisted (e14s02)."""
    rows = int(profile["grid"]["rows"])
    cols = int(profile["grid"]["cols"])
    controller["auto_bindings"] = [
        {
            "device": controller["port"],
            "channel": 0,
            "type": "note",
            "number": _grid_note(profile, r, c),
            "action": MIDI_ACTION_SEQ_TOGGLE,
            "params": {"row": r, "col": c},
            "auto": True,
        }
        for r in range(rows)
        for c in range(cols)
    ]


def controller_connect(controller: dict[str, Any], mido: Any) -> None:
    """Open the controller's LED output, send setup SysEx, register grid bindings (e14s02)."""
    controller_disconnect(controller)
    profile = controller_profile_of(controller)
    if profile is None or not profile.get("features", {}).get("leds"):
        return
    out_name = _find_output_port(controller["port"], mido)
    if out_name is None:
        return
    try:
        with _controller_lock:
            controller["output"] = mido.open_output(out_name)
        if profile.get("setup_sysex"):
            controller["output"].send(mido.Message("sysex", data=profile["setup_sysex"]))
        if controller.get("role") == "grid" and profile.get("features", {}).get("grid"):
            _register_grid_bindings(controller, profile)
        append_log("MIDI", f"{profile.get('name', controller['port'])} output on {out_name}")
    except Exception as e:
        log_error("MIDI", f"output {out_name}: {e}")
        controller_disconnect(controller)


def controller_disconnect(controller: dict[str, Any]) -> None:
    """Close the controller's output and drop its auto bindings (idempotent) (e14s02)."""
    with _controller_lock:
        output = controller.get("output")
        if output is not None:
            with contextlib.suppress(Exception):
                output.close()
            controller["output"] = None
    controller["auto_bindings"] = []


def grid_led(row: int, col: int, color: str) -> None:
    """Set one grid pad LED on the grid controller (semantic color; best-effort).

    The mirror runs only while MIDI is enabled — a disabled engine must never light
    the device (e14 bug fix).
    """
    if not state.midi_enabled:
        return
    controller = grid_controller()
    if controller is None:
        return
    output = controller.get("output")
    if output is None:
        return
    try:
        import mido

        velocity = _controller_velocity(controller, color)
        msg = mido.Message("note_on", note=grid_note(controller, row, col), velocity=velocity)
        with _controller_lock:
            output.send(msg)
    except Exception as e:
        log_error("MIDI", f"grid LED ({row},{col}): {e}")


def grid_mirror_step(row: int, col: int, is_active: bool, is_head: bool) -> None:
    """Mirror one step cell on the grid controller (any thread; no-op without one) (e14s02)."""
    if grid_controller() is None:
        return
    if is_head:
        grid_led(row, col, GRID_LED_AMBER)
    elif is_active:
        grid_led(row, col, GRID_LED_GREEN)
    else:
        grid_led(row, col, GRID_LED_OFF)


def grid_flash_playhead() -> None:
    """White pulse on the current playhead column, restored by a timer (e14s02)."""
    controller = grid_controller()
    if controller is None or state.current_step < 0:
        return
    profile = controller_profile_of(controller) or {}
    rows = int(profile.get("grid", {}).get("rows", 8))
    for r in range(rows):
        grid_led(r, state.current_step, GRID_LED_WHITE)
    threading.Timer(GRID_FLASH_SECONDS, _grid_restore_playhead).start()


def _grid_restore_playhead() -> None:
    """Timer thread: re-apply the playhead amber after a beat flash (e14s02)."""
    controller = grid_controller()
    if controller is None or state.current_step < 0:
        return
    profile = controller_profile_of(controller) or {}
    rows = int(profile.get("grid", {}).get("rows", 8))
    for r in range(rows):
        active = tracks_data[r]["steps"][state.current_step]["active"]
        grid_mirror_step(r, state.current_step, active, True)


def _clock_port_name() -> str | None:
    """The MIDI input the clock listens on: clock_source, else the first input (e14s04)."""
    if state.midi_clock_source:
        return state.midi_clock_source
    try:
        import mido

        names = mido.get_input_names()
        return names[0] if names else None
    except Exception:
        return None
