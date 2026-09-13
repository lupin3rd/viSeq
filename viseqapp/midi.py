"""MIDI control engine for viseq (REFACTOR_LATEST.md commit 10/13).

Binding resolution, the profile-driven controller runtime (connect,
disconnect, grid LED mirror, playhead flash) and the config mirrors.
WORKER-SAFE: no dpg import (HIGH-1) — the learn/action/UI routing and
the worker loops live in the composition root and call these.
"""

import contextlib
import threading
import time
from typing import Any

from viseqapp import iomonitor, state
from viseqapp.config import load_config, save_config
from viseqapp.constants import (
    DEST_MIDI,
    GRID_FLASH_SECONDS,
    GRID_LED_AMBER,
    GRID_LED_GREEN,
    GRID_LED_OFF,
    GRID_LED_WHITE,
    MIDI_ACTION_SEQ_TOGGLE,
    MIDI_KIND_NOTE,
    MIDI_OPEN_RETRY_COOLDOWN_SECONDS,
    MIDI_PITCH_MAX,
    MIDI_PITCH_MIN,
    MIDI_PITCH_NUMBER,
    MIDI_PITCH_VALUE_STEPS,
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
    BUG-2026-09-06T124150: pitchwheel (DJ pitch levers) is 14-bit signed -8192..8191
    with the channel as its only discriminator; it normalizes onto the app-wide
    0..127 value scale (centre of travel ~64) so executors and trigger thresholds
    are unchanged.
    """
    if msg.type == "note_on":
        if msg.velocity > 0:
            return ("note", int(msg.note), int(msg.velocity))
        return (None, 0, 0)
    if msg.type == "note_off":
        return (None, 0, 0)
    if msg.type == "control_change":
        return ("cc", int(msg.control), int(msg.value))
    if msg.type == "pitchwheel":
        pitch = int(msg.pitch)
        value = round(
            (pitch - MIDI_PITCH_MIN) * MIDI_PITCH_VALUE_STEPS / (MIDI_PITCH_MAX - MIDI_PITCH_MIN)
        )
        return ("pitch", MIDI_PITCH_NUMBER, int(value))
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


def scan_midi_inputs() -> tuple[list[str], str | None]:
    """Live MIDI input scan that NEVER raises (BUG-2026-09-07T152802).

    Returns (port names, error). A healthy scan yields the names with error None.
    When mido/ALSA cannot create a sequencer client (kernel table exhausted by
    leaked rtmidi clients — mido #256), the call raises inside rtmidi; the error
    is captured and returned so the UI can show why the controller is not
    detected instead of silently presenting an empty device list.
    """
    try:
        import mido

        names = list(mido.get_input_names())
    except Exception as e:
        return [], str(e)
    return names, None


def midi_open_retry_due(last_fail_at: float | None, now: float) -> bool:
    """True when an input open may be attempted: first try or cooldown elapsed.

    Gates the worker retry (BUG-2026-09-07T152802): a failing open can leak an
    ALSA sequencer client upstream, so a dead ALSA must be re-attempted at the
    cooldown cadence, never on every 2-second worker tick.
    """
    if last_fail_at is None:
        return True
    return now - last_fail_at >= MIDI_OPEN_RETRY_COOLDOWN_SECONDS


def available_controller_ports() -> list[str]:
    """MIDI input ports not yet added as controllers (e14s03)."""
    names, _error = scan_midi_inputs()
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


def project_mapper_bindings() -> list[dict[str, Any]]:
    """Project-scoped MIDI rows: bindings that drive a Mapper mapping, port-tagged.

    A binding belongs to the project when its params carry a ``mapping_id`` — it
    is the MIDI side of one of the project's Mapper mappings. These rows are
    stored WITH the project (user 2026-09-07) so opening a project restores
    exactly its routing; generic rows (transport/sequencer/beat) stay global in
    the app config, because their meaning is not project-bound.
    """
    rows: list[dict[str, Any]] = []
    for controller in midi_controllers:
        for binding in controller.get("bindings") or []:
            params = binding.get("params")
            if isinstance(params, dict) and params.get("mapping_id") is not None:
                rows.append({"port": controller["port"], **dict(binding)})
    return rows


def _binding_mapping_key(binding: dict[str, Any]) -> tuple[str, int]:
    """Identity of a Mapper-binding row within one port: (action, mapping_id)."""
    return (
        str(binding.get("action") or ""),
        int((binding.get("params") or {}).get("mapping_id") or 0),
    )


def apply_project_mapper_bindings(rows: Any, live_mapping_ids: set[int]) -> None:
    """Merge the project's Mapper-binding rows onto the controllers (user 2026-09-07).

    Each row targets the controller whose port matches. A row whose mapping_id
    does not exist in the loaded project is dropped (stale); a row already
    present for the same (action, mapping_id) on a port is replaced so re-
    learning inside the project updates the routing instead of stacking rows.
    """
    if not isinstance(rows, list):
        return
    incoming_by_port: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        params = row.get("params")
        if not (
            isinstance(params, dict) and int(params.get("mapping_id") or 0) in live_mapping_ids
        ):
            continue
        body = {k: v for k, v in row.items() if k != "port"}
        incoming_by_port.setdefault(str(row.get("port") or ""), []).append(body)
    for controller in midi_controllers:
        incoming = incoming_by_port.get(controller["port"])
        if not incoming:
            continue
        existing = controller.setdefault("bindings", [])
        for row in incoming:
            key = _binding_mapping_key(row)
            existing[:] = [b for b in existing if _binding_mapping_key(b) != key]
            existing.append(row)


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
    the auto grid bindings (BUG-2026-08-29T102156). The controller list is
    persisted on every toggle too — re-writing the config from a stale copy used
    to silently discard bindings learned since the last save (user 2026-09-07).
    """
    state.midi_enabled = enabled
    if not enabled:
        disconnect_all_controllers()
    else:
        reconnect_all_controllers()
    cfg = load_config()
    cfg["midi"]["enabled"] = enabled
    save_config(cfg)
    save_midi_controllers()


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


def _mapping_wants_output(port_name: str) -> bool:
    """True when a Mapping writes MIDI to this controller port (e40s01)."""
    return any(
        mapping.get("destination") == DEST_MIDI
        and str((mapping.get("destination_spec") or {}).get("controller_port") or "") == port_name
        for mapping in state.mapper_mappings
    )


def controller_connect(controller: dict[str, Any], mido: Any) -> None:
    """Open the controller's LED output, send setup SysEx, register grid bindings (e14s02).

    e40s01 relaxes the LED-only guard: the output opens when the device declares
    ``features.leds`` OR a Mapping targets it (the setup SysEx and the grid
    bindings still need the profile). BUG-2026-09-07T152802: the output-port
    lookup is INSIDE the guard — with ALSA refusing sequencer clients the lookup
    raises and must log + disconnect, never escape into the MIDI re-enable UI
    callback.
    """
    controller_disconnect(controller)
    profile = controller_profile_of(controller)
    leds = bool(profile is not None and profile.get("features", {}).get("leds"))
    if not leds and not _mapping_wants_output(str(controller.get("port") or "")):
        return
    try:
        out_name = _find_output_port(controller["port"], mido)
        if out_name is None:
            return
        with _controller_lock:
            controller["output"] = mido.open_output(out_name)
        if leds and profile is not None and profile.get("setup_sysex"):
            setup = profile["setup_sysex"]
            _send_observed(
                controller["output"],
                mido.Message("sysex", data=setup),
                str(controller.get("port") or "?"),
                "sysex",
                0,
                0,
                f"controller setup ({len(setup)} bytes)",
            )
        if (
            leds
            and profile is not None
            and controller.get("role") == "grid"
            and profile.get("features", {}).get("grid")
        ):
            _register_grid_bindings(controller, profile)
        name = profile.get("name", controller["port"]) if profile else controller["port"]
        append_log("MIDI", f"{name} output on {out_name}")
    except Exception as e:
        log_error("MIDI", f"output {controller['port']}: {e}")
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


_mapping_output_fail_at: dict[str, float] = {}


def ensure_mapping_output(controller: dict[str, Any]) -> bool:
    """Open a controller's output when a Mapping needs it (e40s01, throttled).

    Returns True when an output is available. Reuses the LED path's port lookup
    and lock; an absent/failing port backs off with the shared open cooldown so
    the main loop never hammers ALSA (BUG-2026-09-07T152802).
    """
    if not state.midi_enabled:
        return False
    if controller.get("output") is not None:
        return True
    port = str(controller.get("port") or "")
    now = time.monotonic()
    if not midi_open_retry_due(_mapping_output_fail_at.get(port), now):
        return False
    try:
        import mido

        out_name = _find_output_port(port, mido)
        if out_name is None:
            _mapping_output_fail_at[port] = now
            return False
        with _controller_lock:
            controller["output"] = mido.open_output(out_name)
        append_log("MIDI", f"mapping output on {out_name}")
        return True
    except Exception as e:
        _mapping_output_fail_at[port] = now
        log_error("MIDI", f"mapping output {port}: {e}")
        return False


def _send_observed(
    output: Any,
    message: Any,
    port: str,
    msg_type: str,
    number: int,
    value: int,
    detail: str,
) -> None:
    """Send one MIDI message under the controller lock and report it (e39s05).

    The ONE place an outgoing MIDI message leaves viseq, so the I/O Monitor sees
    every send (Mapping Destination, grid LED, controller setup) without a second
    wrapper per caller. Observation only: a monitor failure must never break a
    send, so the recording is best-effort.
    """
    with _controller_lock:
        output.send(message)
    with contextlib.suppress(Exception):
        iomonitor.record_tx(
            str(port),
            str(msg_type),
            int(getattr(message, "channel", 0) or 0),
            int(number),
            float(value),
            detail=str(detail),
        )


def send_mapping_midi(
    controller: dict[str, Any],
    kind: str,
    channel: int,
    number: int,
    value: int,
    detail: str = "mapping",
) -> bool:
    """Send a note (velocity) or CC (value) on a controller's output (e40s01).

    Best-effort, never raising into the main loop: MIDI disabled, no open output
    or a failing send return False (logged). The value, number and channel are
    clamped into the MIDI ranges.
    """
    if not state.midi_enabled:
        return False
    output = controller.get("output")
    if output is None:
        return False
    value = max(0, min(127, int(value)))
    number = max(0, min(127, int(number)))
    channel = max(0, min(15, int(channel)))
    try:
        import mido

        if kind == MIDI_KIND_NOTE:
            message = mido.Message("note_on", channel=channel, note=number, velocity=value)
            msg_type = "note"
        else:
            message = mido.Message("control_change", channel=channel, control=number, value=value)
            msg_type = "cc"
        _send_observed(
            output, message, str(controller.get("port") or "?"), msg_type, number, value, detail
        )
        return True
    except Exception as e:
        log_error("MIDI", f"mapping output {controller.get('port')}: {e}")
        return False


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
        note = grid_note(controller, row, col)
        msg = mido.Message("note_on", note=note, velocity=velocity)
        _send_observed(
            output,
            msg,
            str(controller.get("port") or "?"),
            "note",
            note,
            velocity,
            f"grid led r{row}c{col}",
        )
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
    """The MIDI input the clock listens on: clock_source, else the first input."""
    if state.midi_clock_source:
        return state.midi_clock_source
    names, _error = scan_midi_inputs()
    return names[0] if names else None
