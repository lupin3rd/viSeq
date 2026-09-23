"""MIDI control engine for viseq (REFACTOR_LATEST.md commit 10/13).

Binding resolution, the profile-driven controller runtime (connect,
disconnect, grid LED mirror, playhead flash) and the config mirrors.
WORKER-SAFE: no dpg import (HIGH-1) — the learn/action/UI routing and
the worker loops live in the composition root and call these.
"""

import contextlib
import copy
import threading
import time
from collections.abc import Iterable
from typing import Any

from viseqapp import actions, iomonitor, mapper, state
from viseqapp.config import load_config, save_config
from viseqapp.constants import (
    DEST_MIDI,
    GRID_FLASH_SECONDS,
    GRID_LED_AMBER,
    GRID_LED_GREEN,
    GRID_LED_OFF,
    GRID_LED_WHITE,
    MAPPER_MAX_MAPPINGS,
    MIDI_ACTION_MAPPER_ENABLE,
    MIDI_ACTION_MAPPER_MAPPING,
    MIDI_ACTION_MAPPING_TOGGLE,
    MIDI_ACTION_MODE_TOGGLE,
    MIDI_ACTION_SEQ_ROW_DISABLE,
    MIDI_ACTION_SEQ_ROW_ENABLE,
    MIDI_ACTION_SEQ_TOGGLE,
    MIDI_ACTION_TRANSPORT_PLAY,
    MIDI_KIND_NOTE,
    MIDI_LED_BEHAVIOR_PRESS,
    MIDI_LED_BEHAVIOR_STATE,
    MIDI_LED_BEHAVIORS,
    MIDI_LED_MIRROR_MODES,
    MIDI_LED_PRESS_MS,
    MIDI_MODE_LED_KINDS,
    MIDI_MODE_LED_MAX_CHANNEL,
    MIDI_MODE_LED_MAX_NUMBER,
    MIDI_MODE_LED_MAX_VALUE,
    MIDI_MODE_LED_MIN_CHANNEL,
    MIDI_MODE_LED_MIN_NUMBER,
    MIDI_MODE_LED_MIN_VALUE,
    MIDI_MODE_LED_OFF_DEFAULT,
    MIDI_MODE_LED_ON_DEFAULT,
    MIDI_OPEN_RETRY_COOLDOWN_SECONDS,
    MIDI_PITCH_MAX,
    MIDI_PITCH_MIN,
    MIDI_PITCH_NUMBER,
    MIDI_PITCH_VALUE_STEPS,
    MIDI_STEP_ENCODING_ABSOLUTE,
    MIDI_STEP_ENCODINGS,
    MIDI_STEP_MAX_DELTA,
    MIDI_STEP_MAX_PER_SECOND,
    MIDI_STEP_MAX_STEPS_PER_MESSAGE,
    MIDI_STEP_SIZE,
    NUM_STEPS,
    NUM_TRACKS,
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
    midi_controllers,
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
    the worker passes per-controller lists (e14s02). e52s02: the mode gate is applied
    here too (an inactive tag is dropped); `params` stay the stored ones — the
    `source_type` key belongs to resolve_midi_message_report only.
    """
    return _resolve_midi_message(msg, port_name, bindings, inject_source_type=False)[0]


def resolve_midi_message_report(
    msg: Any, port_name: str, bindings: list[dict[str, Any]] | None = None
) -> tuple[list[tuple[str, dict[str, Any], int]], set[str]]:
    """Resolve one message and report which modes the gate swallowed (e52s02).

    Returns ((action, params, value) rows, suppressed mode names). The mode gate
    is applied to the MATCHED bindings before the rows are built; the momentary
    executors need to tell a note from a CC, so `source_type` rides a COPY of
    params (never the stored Binding). `resolve_midi_message` is the same
    resolver WITHOUT that key, so the long-standing facade contract holds.
    """
    return _resolve_midi_message(msg, port_name, bindings, inject_source_type=True)


def _resolve_midi_message(
    msg: Any,
    port_name: str,
    bindings: list[dict[str, Any]] | None,
    *,
    inject_source_type: bool,
) -> tuple[list[tuple[str, dict[str, Any], int]], set[str]]:
    """Match, gate and build the rows — the ONE resolution path (e52s02)."""
    msg_type, number, value = _parse_midi_msg(msg)
    if msg_type is None:
        return [], set()
    channel = int(getattr(msg, "channel", 0))
    # BUG-2026-09-18T194700: no legacy flat list any more — a caller that passes
    # None simply has no bindings to match (the worker always passes the owning
    # controller's list; an unowned port is reported as NOBIND by the monitor).
    if bindings is None:
        bindings = []
    matched = [
        binding
        for binding in bindings
        if _binding_device_ok(binding, port_name)
        and binding_matches(binding, msg_type, number, channel)
    ]
    eligible, muted = gate_matched_bindings(matched, state.midi_modes)
    rows: list[tuple[str, dict[str, Any], int]] = []
    for binding in eligible:
        action = str(binding.get("action", ""))
        params = dict(binding.get("params") or {})
        if inject_source_type:
            params["source_type"] = msg_type
        spec = actions.action_spec(action)
        if spec is not None and spec.kind == actions.KIND_STEP:  # e52s04 / e54s01
            steps = advance_step(
                binding,
                value,
                binding_step_encoding(binding, port_name),
                binding_step_size(binding, port_name),
                binding_step_rate(binding, port_name),
            )
            if steps == 0:
                continue  # nothing turned: no dispatch, no monitor row
            params["steps"] = steps
        rows.append((action, params, value))
    return rows, muted


def binding_source_from_message(msg: Any, port_name: str) -> dict[str, Any] | None:
    """The (device, channel, type, number) half of a binding from a message; releases -> None."""
    msg_type, number, _ = _parse_midi_msg(msg)
    if msg_type is None:
        return None
    return {"device": port_name, "channel": int(msg.channel), "type": msg_type, "number": number}


# ---------- e52s01: MIDI modes (named, additive layers that gate Bindings) ----------
# A mode is a NAME plus an optional MIDI LED. The active set is session state
# (never persisted); the definitions travel with the project (e52s03). A Binding
# carries the gate tag in its own top-level "mode" key (e52s02), never here.


def _mode_key(name: Any) -> str:
    """Case- and whitespace-insensitive identity of a mode name."""
    return str(name or "").strip().casefold()


def canonical_mode_name(name: str) -> str | None:
    """The STORED name of a registered mode matching `name`, or None (case-insensitive)."""
    key = _mode_key(name)
    if not key:
        return None
    for definition in state.midi_mode_defs:
        if _mode_key(definition.get("name")) == key:
            return str(definition.get("name"))
    return None


def mode_names() -> list[str]:
    """The registered mode names, in creation order."""
    return [str(definition.get("name") or "") for definition in state.midi_mode_defs]


def mode_definition(name: str) -> dict[str, Any] | None:
    """The registry row of a mode (case-insensitive), or None when unregistered."""
    canonical = canonical_mode_name(name)
    if canonical is None:
        return None
    for definition in state.midi_mode_defs:
        if str(definition.get("name") or "") == canonical:
            return definition
    return None


def add_mode(name: str) -> str | None:
    """Register a mode by name; a blank or case-insensitive duplicate returns None."""
    trimmed = str(name or "").strip()
    if not trimmed or canonical_mode_name(trimmed) is not None:
        return None
    state.midi_mode_defs.append({"name": trimmed, "led": None})
    return trimmed


def set_mode_led(name: str, led: dict[str, Any] | None) -> bool:
    """Store (or clear with None) a mode's LED spec; False when the mode is unknown."""
    definition = mode_definition(name)
    if definition is None:
        return False
    definition["led"] = dict(led) if isinstance(led, dict) else None
    return True


def mode_usage(name: str) -> dict[str, int]:
    """What deleting a mode would touch: tagged Bindings + its toggle rows (e52s03)."""
    counts = {"bindings_untagged": 0, "toggle_rows_dropped": 0}
    canonical = canonical_mode_name(name)
    if canonical is None:
        return counts
    key = _mode_key(canonical)
    for controller in midi_controllers:
        for binding in controller.get("bindings") or []:
            params = binding.get("params")
            params = params if isinstance(params, dict) else {}
            if (
                binding.get("action") == MIDI_ACTION_MODE_TOGGLE
                and _mode_key(params.get("mode")) == key
            ):
                counts["toggle_rows_dropped"] += 1
            elif _mode_key(binding.get("mode")) == key:
                counts["bindings_untagged"] += 1
    return counts


def remove_mode(name: str) -> dict[str, int]:
    """Delete a mode: clear its Binding tags, drop its toggle rows, forget it.

    Returns the affected counts (the UI confirmation states them) so a delete is
    never silent. A Binding whose tag is cleared falls back to the base layer.
    """
    counts = mode_usage(name)
    canonical = canonical_mode_name(name)
    if canonical is None:
        return counts
    key = _mode_key(canonical)
    for controller in midi_controllers:
        kept: list[dict[str, Any]] = []
        for binding in controller.get("bindings") or []:
            params = binding.get("params")
            params = params if isinstance(params, dict) else {}
            is_toggle = (
                binding.get("action") == MIDI_ACTION_MODE_TOGGLE
                and _mode_key(params.get("mode")) == key
            )
            if is_toggle:
                continue
            if _mode_key(binding.get("mode")) == key:
                binding["mode"] = None
            kept.append(binding)
        controller["bindings"] = kept
    state.midi_modes.discard(canonical)
    _forget_mode_order(canonical)
    state.midi_mode_defs[:] = [
        definition
        for definition in state.midi_mode_defs
        if str(definition.get("name") or "") != canonical
    ]
    return counts


def mode_is_active(name: str) -> bool:
    """True while the named (registered) mode is in the active set."""
    canonical = canonical_mode_name(name)
    return canonical is not None and canonical in state.midi_modes


def set_mode_active(name: str, active: bool) -> bool:
    """Add/remove a mode from the active set; returns the new state (False if unknown).

    e52s08: a real activation appends to `state.midi_mode_order`, so the last one
    activated can be recovered (the learn tag); a deactivation forgets it.
    """
    canonical = canonical_mode_name(name)
    if canonical is None:
        return False
    if active:
        if canonical not in state.midi_modes:
            state.midi_modes.add(canonical)
            state.midi_mode_order.append(canonical)
    else:
        state.midi_modes.discard(canonical)
        _forget_mode_order(canonical)
    return active


def _forget_mode_order(name: str) -> None:
    """Drop one name from the activation order (e52s08)."""
    state.midi_mode_order[:] = [item for item in state.midi_mode_order if item != name]


def most_recent_mode() -> str | None:
    """The name of the mode activated last, or None (e52s08).

    Reads `state.midi_modes` as the source of truth, so an order entry left
    behind by a direct mutation of the set can never gate a Controller to an
    inactive mode.
    """
    for name in reversed(state.midi_mode_order):
        if name in state.midi_modes:
            return name
    return None


def toggle_mode(name: str) -> bool | None:
    """Flip a mode's active flag; None for an unregistered name."""
    canonical = canonical_mode_name(name)
    if canonical is None:
        return None
    return set_mode_active(canonical, canonical not in state.midi_modes)


def send_mode_led(name: str, active: bool) -> None:
    """Best-effort MIDI out for a mode's LED (on while active, off otherwise).

    No LED spec, an absent controller or a closed output is a silent no-op; the
    value rides the existing send_mapping_midi path, so the I/O Monitor observes
    it like any other outgoing message.
    """
    definition = mode_definition(name)
    if definition is None:
        return
    led = definition.get("led")
    if not isinstance(led, dict):
        return
    port = str(led.get("controller_port") or "")
    controller = find_controller_by_port(port) if port else None
    if controller is None or not ensure_mapping_output(controller):
        return
    send_mapping_midi(
        controller,
        str(led.get("kind") or MIDI_KIND_NOTE),
        int(led.get("channel") or 0),
        int(led.get("number") or 0),
        int(led.get("on_value" if active else "off_value") or 0),
        detail=f"mode {name} led",
    )


def reapply_mode_leds() -> None:
    """Re-send the on LED of every active mode (a re-plugged controller must not go dark)."""
    for name in sorted(state.midi_modes):
        send_mode_led(name, True)


def clear_all_modes() -> None:
    """Turn every active mode's LED off and empty the set (disable, load, panic)."""
    for name in sorted(state.midi_modes):
        send_mode_led(name, False)
    state.midi_modes.clear()
    state.midi_mode_order.clear()


# ---------- e53s01: per-Binding LED feedback ----------
# A Binding may carry a top-level `led` spec (same Destination = MIDI shape as a
# mode LED) and a `led_behavior`. When no explicit spec is set, a controller
# profile's `feedback.led_mirror` derives the LED from the input control (same
# kind/channel/number) — that is what lets a whole surface light with no
# per-binding setup. Sends ride `send_mapping_midi`, so the I/O Monitor observes
# them like any other outgoing message.


# e53s02: the actions whose Binding defaults to the `state` LED behaviour, so a
# button that OWNS a boolean state shows it instead of just flashing on press
# (an arm button stays lit while its Mapping is armed). A momentary action
# defaults to `press`; an explicit `led_behavior` always wins. The oracle in
# `action_state` is the single reader of the state.
MIDI_LED_STATEFUL_ACTIONS: tuple[str, ...] = (
    MIDI_ACTION_MAPPING_TOGGLE,
    MIDI_ACTION_MAPPER_ENABLE,
    MIDI_ACTION_MODE_TOGGLE,
    MIDI_ACTION_SEQ_TOGGLE,
    MIDI_ACTION_TRANSPORT_PLAY,
    MIDI_ACTION_SEQ_ROW_ENABLE,
    MIDI_ACTION_SEQ_ROW_DISABLE,
)


def binding_led_behavior(binding: dict[str, Any]) -> str:
    """The Binding's LED behaviour (e53s01/e53s02).

    An explicit `led_behavior` wins; otherwise a stateful action defaults to
    `state` (the LED mirrors the state) and everything else to `press` (the
    flash). That makes "arm a Mapping -> its button lights" work with no
    configuration, while a trigger button still flashes on the press edge.
    """
    value = str(binding.get("led_behavior") or "").strip().lower()
    if value in MIDI_LED_BEHAVIORS:
        return value
    action = actions.canonical_action(str(binding.get("action") or ""))
    if action in MIDI_LED_STATEFUL_ACTIONS:
        return MIDI_LED_BEHAVIOR_STATE
    return MIDI_LED_BEHAVIOR_PRESS


def led_from_profile(binding: dict[str, Any], profile: dict[str, Any] | None) -> dict | None:
    """Derive a Binding's LED from its controller profile's `led_mirror` rule (e53s01).

    The LED is the INPUT control (same kind, channel and number) with the
    profile's on/off values, so a note button lights its own note. None when the
    profile has no mirror, the mirror mode is unknown, or the Binding's type does
    not match it.
    """
    if not isinstance(profile, dict):
        return None
    feedback = profile.get("feedback")
    if not isinstance(feedback, dict):
        return None
    mirror = str(feedback.get("led_mirror") or "").strip().lower()
    if mirror not in MIDI_LED_MIRROR_MODES or str(binding.get("type") or "") != mirror:
        return None
    return _sanitize_mode_led(
        {
            "controller_port": str(binding.get("device") or ""),
            "channel": binding.get("channel"),
            "kind": mirror,
            "number": binding.get("number"),
            "on_value": feedback.get("on_value"),
            "off_value": feedback.get("off_value"),
        }
    )


def resolve_binding_led(binding: dict[str, Any], profile: dict[str, Any] | None) -> dict | None:
    """The LED spec to send for a Binding: explicit `led` first, else the profile rule."""
    explicit = binding.get("led")
    if isinstance(explicit, dict):
        return _sanitize_mode_led(explicit)
    return led_from_profile(binding, profile)


def binding_led_key(led: dict[str, Any]) -> str:
    """Identity of a physical LED target: kind|channel|number (e53s02)."""
    return f"{led.get('kind')}|{led.get('channel')}|{led.get('number')}"


def send_binding_led(
    controller: dict[str, Any], led: dict[str, Any], on: bool, *, cancel_flashes: bool = True
) -> bool:
    """Send a Binding LED's on/off value; best-effort, opening the output on demand (e53s01).

    A real write bumps the target's flash generation (e53s02), so a press flash
    scheduled earlier can no longer turn this LED off when its timer fires.
    """
    if not state.midi_enabled:
        return False
    if controller.get("output") is None and not ensure_mapping_output(controller):
        return False
    if cancel_flashes:
        key = binding_led_key(led)
        state.midi_led_flash_token[key] = state.midi_led_flash_token.get(key, 0) + 1
    value = int(led.get("on_value" if on else "off_value") or 0)
    return send_mapping_midi(
        controller,
        str(led.get("kind") or MIDI_KIND_NOTE),
        int(led.get("channel") or 0),
        int(led.get("number") or 0),
        value,
        detail=f"binding led {'on' if on else 'off'}",
    )


def flash_binding_led(
    controller: dict[str, Any], led: dict[str, Any], delay: float | None = None
) -> None:
    """Light a Binding LED on the trigger edge, then turn it off after the flash (e53s01).

    The off is a `threading.Timer` (the `grid_flash_playhead` pattern), so the
    message path never waits; the timer holds no dpg and takes the controller
    lock only for its own send. e53s02: the timer re-checks the target's flash
    generation, so a state write (or a newer flash) supersedes a stale off.
    """
    send_binding_led(controller, led, True)
    key = binding_led_key(led)
    token = state.midi_led_flash_token.get(key, 0)

    def _restore() -> None:
        if state.midi_led_flash_token.get(key, 0) != token:
            return  # a state write or a newer flash owns the LED now
        send_binding_led(controller, led, False, cancel_flashes=False)

    seconds = MIDI_LED_PRESS_MS / 1000.0 if delay is None else float(delay)
    threading.Timer(seconds, _restore).start()


def led_bindings_for_message(
    msg: Any, port_name: str, bindings: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """The eligible (matched + mode-gated) Bindings of a message, for LED feedback (e53s01).

    Reuses the resolver's matching and the e52s02 gate so a Binding suppressed by
    an inactive mode never lights, and a release edge (no parsed type) returns
    nothing. Order is preserved; the inputs are never mutated.
    """
    msg_type, number, _ = _parse_midi_msg(msg)
    if msg_type is None or bindings is None:
        return []
    channel = int(getattr(msg, "channel", 0))
    matched = [
        binding
        for binding in bindings
        if _binding_device_ok(binding, port_name)
        and binding_matches(binding, msg_type, number, channel)
    ]
    eligible, _muted = gate_matched_bindings(matched, state.midi_modes)
    return eligible


# ---------- e53s02: the action-state oracle (state LEDs) ----------
# A Binding whose led_behavior is `state` mirrors the boolean state its action
# owns: a Mapping armed, a mode active, a step/row active, the transport
# playing. ONE oracle reads every state, and refresh_binding_leds re-sends the
# LEDs, so the mouse and MIDI paths can never disagree. `enable_correction` is a
# one-shot OSC send with no viseq-side state, so it returns None (the Binding
# falls back to the e53s01 press flash).


def action_state(action: str, params: dict[str, Any]) -> bool | None:
    """The boolean state a Binding mirrors, or None when the action is not stateful (e53s02)."""
    action = actions.canonical_action(str(action or ""))
    if action in (
        MIDI_ACTION_MAPPING_TOGGLE,
        MIDI_ACTION_MAPPER_ENABLE,
        MIDI_ACTION_MAPPER_MAPPING,
    ):
        mapping = mapper.find_mapping(int(params.get("mapping_id", 0) or 0))
        if mapping is None:
            return None
        return bool(mapping.get("enabled", False))
    if action == MIDI_ACTION_MODE_TOGGLE:
        name = str(params.get("mode") or "")
        return mode_is_active(name) if name else None
    if action == MIDI_ACTION_SEQ_TOGGLE:
        row = int(params.get("row", -1) or 0)
        col = int(params.get("col", -1) or 0)
        if 0 <= row < NUM_TRACKS and 0 <= col < NUM_STEPS:
            return bool(state.tracks_data[row]["steps"][col]["active"])
        return None
    if action == MIDI_ACTION_TRANSPORT_PLAY:
        return bool(state.is_playing)
    if action in (MIDI_ACTION_SEQ_ROW_ENABLE, MIDI_ACTION_SEQ_ROW_DISABLE):
        row = int(params.get("row", -1) or 0)
        if 0 <= row < NUM_TRACKS:
            return any(state.tracks_data[row]["steps"][col]["active"] for col in range(NUM_STEPS))
        return None
    return None


def binding_state_leds(controller: dict[str, Any]) -> list[tuple[dict[str, Any], bool]]:
    """The (led, on) pairs a controller's `state` Bindings should show right now (e53s02)."""
    profile = controller_profile_of(controller)
    rows: list[tuple[dict[str, Any], bool]] = []
    bindings = list(controller.get("bindings") or []) + list(controller.get("auto_bindings") or [])
    for binding in bindings:
        if binding_led_behavior(binding) != MIDI_LED_BEHAVIOR_STATE:
            continue
        led = resolve_binding_led(binding, profile)
        if led is None:
            continue
        params = binding.get("params")
        params = params if isinstance(params, dict) else {}
        on = action_state(str(binding.get("action") or ""), params)
        if on is None:
            continue
        rows.append((led, bool(on)))
    return rows


def refresh_binding_leds() -> None:
    """Re-send the `state` LED of every Binding from the oracle (e53s02).

    Called after every state mutation (MIDI executor OR mouse path) and on a
    controller reconnect, so the controller shows the truth instead of going
    dark. A missing output is opened on demand by send_binding_led.
    """
    for controller in midi_controllers:
        for led, on in binding_state_leds(controller):
            send_binding_led(controller, led, on)


# ---------- e52s02: the Binding gate ----------
# A Binding carries its gate tag in its own top-level "mode" key (None = base).
# Most-specific-wins: among the Bindings matched by ONE message, an untagged
# survivor is suppressed as soon as at least one TAGGED survivor exists — and
# only then, so a workspace with no mode tag resolves exactly as before.


def binding_mode(binding: dict[str, Any]) -> str | None:
    """The gate tag of a Binding, trimmed; None means the base layer."""
    text = str(binding.get("mode") or "").strip()
    return text or None


def set_binding_mode(binding: dict[str, Any], name: str) -> None:
    """Write the gate tag, canonicalizing it to the registered name when possible."""
    trimmed = str(name or "").strip()
    binding["mode"] = canonical_mode_name(trimmed) or trimmed or None


def gate_matched_bindings(
    matched: list[dict[str, Any]], active: set[str]
) -> tuple[list[dict[str, Any]], set[str]]:
    """Filter matched Bindings by the active mode set; report the swallowed modes.

    Returns (eligible, muted): `muted` holds the mode names of the Bindings the
    gate dropped, for the I/O Monitor's MUTED outcome. Order is preserved and the
    inputs are never mutated. When at least one eligible Binding is tagged, the
    untagged ones are suppressed (the most specific layer wins).
    """
    active_keys = {_mode_key(name) for name in active}
    eligible: list[dict[str, Any]] = []
    muted: set[str] = set()
    for binding in matched:
        tag = binding_mode(binding)
        if tag is None or _mode_key(tag) in active_keys:
            eligible.append(binding)
        else:
            muted.add(tag)
    if any(binding_mode(binding) is not None for binding in eligible):
        eligible = [binding for binding in eligible if binding_mode(binding) is not None]
    return eligible, muted


def modes_for_mapping(mapping_id: int) -> str | None:
    """The gate tag on the Binding(s) that drive a Mapper Mapping, or None (for the card badge)."""
    for controller in midi_controllers:
        for binding in controller.get("bindings") or []:
            params = binding.get("params")
            if not isinstance(params, dict):
                continue
            if int(params.get("mapping_id") or 0) != int(mapping_id):
                continue
            tag = binding_mode(binding)
            if tag is not None:
                return tag
    return None


# ---------- e52s04: rotation stepping ----------
# A step-kind Binding consumes the SIGNED delta of its CC, converted into
# detents (MIDI_STEP_SIZE) with the remainder carried over; a jump beyond
# MIDI_STEP_MAX_DELTA is a wrap/glitch and resets the cursor instead of firing a
# burst. The cursor lives in state.midi_step_last, keyed per physical control
# AND per mode, and is owned by the single MIDI worker thread.


def step_key(binding: dict[str, Any]) -> str:
    """Identity of a step Binding's rotation cursor (device/type/number/action/mode)."""
    tag = _mode_key(binding_mode(binding) or "")
    return (
        f"{binding.get('device', '')}|{binding.get('type', '')}|"
        f"{binding.get('number', '')}|{binding.get('action', '')}|{tag}"
    )


def step_delta(
    prev: tuple[int, int] | None,
    value: int,
    encoding: str = MIDI_STEP_ENCODING_ABSOLUTE,
    step_size: int = MIDI_STEP_SIZE,
) -> tuple[int, tuple[int, int]]:
    """Signed detents and the next cursor from the previous one (pure).

    `prev is None` calibrates on the first message (no step). The remainder of a
    partial detent carries over, so a slow turn accumulates instead of being
    lost, and the reverse rotation cancels it. e54s01 adds the encoding: an
    `absolute` controller reports a position (delta between messages, wrap-
    guarded); `relative64` / `relative2s` report the movement itself, so one
    message with a large magnitude is a fast spin. The per-message step count is
    capped in every encoding.
    """
    value = int(value)
    if prev is None:
        return 0, (value, 0)
    last, residual = prev
    if encoding == "relative64":
        delta = value - 64
    elif encoding == "relative2s":
        delta = value if value < 64 else value - 128
    else:
        delta = value - int(last)
        if abs(delta) > MIDI_STEP_MAX_DELTA:
            return 0, (value, 0)
    total = residual + delta
    steps = int(total / step_size)
    if steps > MIDI_STEP_MAX_STEPS_PER_MESSAGE:
        steps = MIDI_STEP_MAX_STEPS_PER_MESSAGE
    elif steps < -MIDI_STEP_MAX_STEPS_PER_MESSAGE:
        steps = -MIDI_STEP_MAX_STEPS_PER_MESSAGE
    return steps, (value, total - steps * step_size)


def advance_step(
    binding: dict[str, Any],
    value: int,
    encoding: str = MIDI_STEP_ENCODING_ABSOLUTE,
    step_size: int = MIDI_STEP_SIZE,
    max_per_second: int = MIDI_STEP_MAX_PER_SECOND,
    now: float | None = None,
) -> int:
    """Advance one step Binding's cursor and return its signed detent count (e54s01).

    The raw count from `step_delta` is then spent against a per-control token
    bucket, so a fast spin cannot run away: at most `max_per_second` steps per
    second are granted (with a burst up to the same amount). `now` is injectable
    for tests; production passes the single MIDI worker's clock.
    """
    key = step_key(binding)
    steps, cursor = step_delta(state.midi_step_last.get(key), value, encoding, step_size)
    state.midi_step_last[key] = cursor
    if now is None:
        now = time.monotonic()
    return _rate_limit_step(key, steps, now, max_per_second)


def _rate_limit_step(key: str, steps: int, now: float, max_per_second: int) -> int:
    """Spend `steps` against the control's per-second budget; return what fit (e54s01)."""
    cap = float(max_per_second)
    last_at, tokens = state.midi_step_budget.get(key, (now, cap))
    tokens = min(cap, tokens + max(0.0, now - last_at) * max_per_second)
    granted = min(abs(steps), int(tokens))
    tokens -= granted
    state.midi_step_budget[key] = (now, tokens)
    return granted if steps >= 0 else -granted


def binding_step_size(binding: dict[str, Any], port_name: str) -> int:
    """CC units per detent for a step Binding: explicit, else the profile, else the default.

    e54s01 (rig feedback 2026-09-21): a wheel that feels too fast is slowed by a
    larger `step_size`; the controller profile carries the tuned value so no
    controller-specific constant enters the engine.
    """
    explicit = binding.get("step_size")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    controller = find_controller_by_port(port_name)
    profile = controller_profile_of(controller) if controller is not None else None
    if isinstance(profile, dict):
        feedback = profile.get("feedback")
        if isinstance(feedback, dict):
            size = feedback.get("step_size")
            if isinstance(size, int) and size > 0:
                return size
    return MIDI_STEP_SIZE


def binding_step_rate(binding: dict[str, Any], port_name: str) -> int:
    """Max steps per second for a step Binding: explicit, else the profile, else the default.

    e54s01 (rig feedback 2026-09-21): `step_size` tunes the slow-turn feel, this
    caps the fast-spin runaway. The controller profile carries the tuned value.
    """
    explicit = binding.get("max_steps_per_second")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    controller = find_controller_by_port(port_name)
    profile = controller_profile_of(controller) if controller is not None else None
    if isinstance(profile, dict):
        feedback = profile.get("feedback")
        if isinstance(feedback, dict):
            rate = feedback.get("max_steps_per_second")
            if isinstance(rate, int) and rate > 0:
                return rate
    return MIDI_STEP_MAX_PER_SECOND


def binding_step_encoding(binding: dict[str, Any], port_name: str) -> str:
    """Resolve a step Binding's rotation encoding: explicit, else the profile hint (e54s01).

    `binding["encoding"]` wins when valid; otherwise the owning controller's
    profile `feedback.relative_cc` marks CC controls that are relative encoders
    (the jog wheel) as `relative64`. No hint means `absolute` — the e52s04
    behaviour, byte-identical.
    """
    explicit = str(binding.get("encoding") or "").strip().lower()
    if explicit in MIDI_STEP_ENCODINGS:
        return explicit
    if str(binding.get("type") or "") != "cc":
        return MIDI_STEP_ENCODING_ABSOLUTE
    controller = find_controller_by_port(port_name)
    profile = controller_profile_of(controller) if controller is not None else None
    feedback = profile.get("feedback") if isinstance(profile, dict) else None
    if isinstance(feedback, dict):
        relative = feedback.get("relative_cc")
        number = int(binding.get("number", -1) or 0)
        if isinstance(relative, list) and number in [int(item) for item in relative]:
            return "relative64"
    return MIDI_STEP_ENCODING_ABSOLUTE


def reset_step_memory(mode: str | None = None) -> None:
    """Forget the rotation cursors and rate budgets (one mode's, or all with None)."""
    if mode is None:
        state.midi_step_last.clear()
        state.midi_step_budget.clear()
        return
    suffix = f"|{_mode_key(mode)}"
    for key in [key for key in state.midi_step_last if key.endswith(suffix)]:
        del state.midi_step_last[key]
    for key in [key for key in state.midi_step_budget if key.endswith(suffix)]:
        del state.midi_step_budget[key]


# ---------- e52s03: modes and mode Bindings travel with the project ----------
# Definitions incl. the LED specs, plus the MODE_TOGGLE rows and every
# mode-tagged row, are project-scoped (P-3): a load REPLACES the mode-scoped
# rows, so switching projects never stacks or resurrects a stale layer. The
# live active set is never persisted and is cleared on load.


def capture_mode_defs() -> list[dict[str, Any]]:
    """A deep copy of the mode registry for the project document (e52s03)."""
    return [copy.deepcopy(definition) for definition in state.midi_mode_defs]


def project_mode_bindings() -> list[dict[str, Any]]:
    """The port-tagged Binding rows that belong to the project's modes (e52s03)."""
    rows: list[dict[str, Any]] = []
    for controller in midi_controllers:
        for binding in controller.get("bindings") or []:
            is_toggle = binding.get("action") == MIDI_ACTION_MODE_TOGGLE
            if binding_mode(binding) is None and not is_toggle:
                continue
            rows.append({"port": controller["port"], **dict(binding)})
    return rows


def _clamp_int(raw: Any, low: int, high: int, default: int) -> int:
    """Coerce a stored value into [low, high], falling back on the default."""
    try:
        return max(low, min(high, int(raw)))
    except (TypeError, ValueError):
        return default


def _sanitize_mode_led(raw: Any) -> dict[str, Any] | None:
    """Heal one stored LED spec onto the Destination = MIDI shape (e52s03)."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or MIDI_MODE_LED_KINDS[0])
    if kind not in MIDI_MODE_LED_KINDS:
        kind = MIDI_MODE_LED_KINDS[0]
    return {
        "controller_port": str(raw.get("controller_port") or ""),
        "channel": _clamp_int(
            raw.get("channel"),
            MIDI_MODE_LED_MIN_CHANNEL,
            MIDI_MODE_LED_MAX_CHANNEL,
            MIDI_MODE_LED_MIN_CHANNEL,
        ),
        "kind": kind,
        "number": _clamp_int(
            raw.get("number"),
            MIDI_MODE_LED_MIN_NUMBER,
            MIDI_MODE_LED_MAX_NUMBER,
            MIDI_MODE_LED_MIN_NUMBER,
        ),
        "on_value": _clamp_int(
            raw.get("on_value"),
            MIDI_MODE_LED_MIN_VALUE,
            MIDI_MODE_LED_MAX_VALUE,
            MIDI_MODE_LED_ON_DEFAULT,
        ),
        "off_value": _clamp_int(
            raw.get("off_value"),
            MIDI_MODE_LED_MIN_VALUE,
            MIDI_MODE_LED_MAX_VALUE,
            MIDI_MODE_LED_OFF_DEFAULT,
        ),
    }


def restore_mode_defs(rows: Any) -> int:
    """Replace the registry with a project's definitions; returns how many installed.

    Every row is sanitized: a bad name is skipped, a bad LED spec is dropped or
    clamped, and the list is bounded. The active set is emptied first (a load
    never starts with a mode on).
    """
    state.midi_modes.clear()
    state.midi_mode_order.clear()
    state.midi_mode_defs.clear()
    if not isinstance(rows, list):
        return 0
    installed = 0
    for raw in rows[:MAPPER_MAX_MAPPINGS]:
        if not isinstance(raw, dict):
            continue
        name = add_mode(str(raw.get("name") or ""))
        if name is None:
            continue
        set_mode_led(name, _sanitize_mode_led(raw.get("led")))
        installed += 1
    return installed


def _mode_row_key(binding: dict[str, Any]) -> tuple[str, str, int, int, str]:
    """Identity of a mode-scoped Binding row: (action, type, number, channel, mode)."""
    return (
        str(binding.get("action") or ""),
        str(binding.get("type") or ""),
        _clamp_int(binding.get("number"), MIDI_MODE_LED_MIN_NUMBER, MIDI_MODE_LED_MAX_NUMBER, 0),
        _clamp_int(binding.get("channel"), MIDI_MODE_LED_MIN_CHANNEL, MIDI_MODE_LED_MAX_CHANNEL, 0),
        _mode_key(binding_mode(binding) or ""),
    )


def apply_project_mode_bindings(rows: Any, live_mode_names: list[str]) -> None:
    """Replace the mode-scoped Bindings with the project's rows (e52s03).

    A row whose gate mode or whose toggle target is not a known mode is dropped
    as stale. Every mode-scoped row already on a controller is removed first, so
    opening another project can neither stack rows nor resurrect a stale layer.
    """
    live_keys = {_mode_key(name) for name in live_mode_names}
    incoming_by_port: dict[str, list[dict[str, Any]]] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            params = row.get("params")
            params = params if isinstance(params, dict) else {}
            if row.get("action") == MIDI_ACTION_MODE_TOGGLE and (
                _mode_key(params.get("mode")) not in live_keys
            ):
                continue
            tag = binding_mode(row)
            if tag is not None and _mode_key(tag) not in live_keys:
                continue
            body = {key: value for key, value in row.items() if key != "port"}
            incoming_by_port.setdefault(str(row.get("port") or ""), []).append(body)
    for controller in midi_controllers:
        existing = controller.setdefault("bindings", [])
        existing[:] = [binding for binding in existing if not _is_mode_scoped(binding)]
        for row in incoming_by_port.get(controller["port"], []):
            key = _mode_row_key(row)
            existing[:] = [binding for binding in existing if _mode_row_key(binding) != key]
            existing.append(row)


def _is_mode_scoped(binding: dict[str, Any]) -> bool:
    """True when a Binding belongs to the project's mode layer (a tag or a toggle row)."""
    return binding_mode(binding) is not None or binding.get("action") == MIDI_ACTION_MODE_TOGGLE


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


def stale_ports(open_ports: Iterable[str], live: Iterable[str]) -> list[str]:
    """Open input ports no longer present, so the worker drops and reopens them (e53).

    A powered-off controller can leave a stale open port behind (`iter_pending`
    returns nothing instead of raising), so a power-cycle would never re-trigger
    the LED handshake (the setup SysEx) until viseq restarted. Comparing against
    the live port list is the reliable signal.
    """
    live_set = set(live)
    return [port for port in open_ports if port not in live_set]


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
    """The controller whose bindings the Bindings section edits (e14s03).

    e53 rig fix: a grid controller keeps its bindings in `auto_bindings` (never
    editable here) and always has an empty `bindings` list, so it is a dead end
    whenever a real controller exists — the Xponent's bindings were unreachable.
    Prefer the selected port, then the first controller with editable bindings,
    then the grid, then the first.
    """
    if state.midi_selected_port is not None:
        controller = find_controller_by_port(state.midi_selected_port)
        if controller is not None:
            return controller
    for controller in midi_controllers:
        if controller.get("bindings"):
            return controller
    return grid_controller() or (midi_controllers[0] if midi_controllers else None)


def selected_bindings() -> list[dict[str, Any]]:
    """The bindings list the Bindings section edits: the selected controller's.

    BUG-2026-09-18T194700: bindings live on controllers only. With no controller
    configured there is nothing to show, which is the honest empty state (the
    legacy flat list this used to fall back to was never persisted).
    """
    controller = selected_controller()
    if controller is not None:
        return controller.setdefault("bindings", [])
    return []


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
    reapply_mode_leds()  # e52s01: a re-plugged device shows the active modes again
    refresh_binding_leds()  # e53s02: and the state LEDs show the truth


def set_midi_enabled(enabled: bool) -> None:
    """Enable/disable the MIDI control engine and persist the flag (main thread).

    Disabling closes every controller output so the device stops lighting up
    immediately (e14 bug fix); re-enabling reconnects the outputs and re-registers
    the auto grid bindings (BUG-2026-08-29T102156). The controller list is
    persisted on every toggle too — re-writing the config from a stale copy used
    to silently discard bindings learned since the last save (user 2026-09-07).
    """
    if not enabled:
        # e52s01/e52s04: send the off LEDs while the engine is still enabled and
        # the outputs are open, and forget the rotation cursors.
        clear_all_modes()
        reset_step_memory()
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
    """Resolve a controller's profile by its profile id, else by its port (e14s02/e53).

    e53: a controller added BEFORE its profile existed (or with a stale/empty
    `profile_id`, e.g. the user's Xponent) must still pick up a dropped profile
    file, otherwise `feedback.led_mirror` and the `relative_cc` hint are dead for
    every already-configured controller. The id wins; the port name is the
    fallback (the same matcher the add-controller flow uses).
    """
    profiles = controller_profiles()
    profile = profiles.get(str(controller.get("profile_id") or ""))
    if profile is None:
        profile = match_controller_profile(str(controller.get("port") or ""), profiles)
    return profile


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
