"""Mapper core for viseq (e16): property catalog + mapping CRUD + OSC send.

A mapping associates a vimix source (target_id) with one source property and
a compact graphic control (slider/knob/button); moving the control sends the
corresponding OSC message to vimix. WORKER-SAFE: this module never imports
dpg (HIGH-1) — mapping state lives in ``state.mapper_mappings`` and OSC
sends go through the shared viOSC client plus the log queue, mirroring the
sequencer pattern.
"""

import copy
from typing import Any

from viseqapp import leap, state
from viseqapp.constants import MAPPER_MAX_MAPPINGS, MAPPER_PERSISTED_KEYS
from viseqapp.osc import osc_client
from viseqapp.queues import append_log

# Property catalog: single-float vimix source attributes usable with a
# video/image source. Ranges are the vimix Open-Sound-Control-API wiki
# contract — the sequencer/monitor players depend on those exact values, so
# this table must never drift from the docs (brightness is -1..+1, not 0..1).
MAPPER_PROPERTIES: dict[str, dict[str, Any]] = {
    "alpha": {"label": "Alpha", "min": -1.0, "max": 1.0},
    "transparency": {"label": "Transparency", "min": 0.0, "max": 2.0},
    "brightness": {"label": "Brightness", "min": -1.0, "max": 1.0},
    "contrast": {"label": "Contrast", "min": -1.0, "max": 1.0},
    "saturation": {"label": "Saturation", "min": -1.0, "max": 1.0},
    "hue": {"label": "Hue", "min": 0.0, "max": 1.0},
    "gamma": {"label": "Gamma", "min": -1.0, "max": 1.0},
    "threshold": {"label": "Threshold", "min": 0.0, "max": 1.0},
    "posterize": {"label": "Posterize", "min": 1.0, "max": 256.0},
    "invert": {"label": "Invert", "min": 0.0, "max": 2.0},
    "depth": {"label": "Depth", "min": 0.0, "max": 12.0},
    "angle": {"label": "Angle", "min": -3.1416, "max": 3.1416},
    "lock": {"label": "Lock", "min": 0.0, "max": 1.0},
    "correction": {"label": "Correction", "min": 0.0, "max": 1.0},
    "play": {"label": "Play", "min": 0.0, "max": 1.0},
    "seek": {"label": "Seek", "min": 0.0, "max": 1.0},
    "speed": {"label": "Speed", "min": 0.1, "max": 10.0},
}

MAPPER_CONTROLS: tuple[str, str, str] = ("slider", "knob", "button")


def _spec_of(prop: str) -> dict[str, Any]:
    """The catalog entry for a property (KeyError = catalog bug, not a user path)."""
    return MAPPER_PROPERTIES[prop]


def _clamp(value: float, prop_min: float, prop_max: float) -> float:
    """Clamp a value into the vimix contract range of the property."""
    return max(prop_min, min(prop_max, value))


def _midpoint(prop_min: float, prop_max: float) -> float:
    """The neutral default for a property (brightness 0.0, alpha 0.0, ...)."""
    return (prop_min + prop_max) / 2.0


def _build_mapping(
    mapping_id: int, target_id: str | None, prop: str, control: str
) -> dict[str, Any]:
    """A fresh mapping dict with the catalog defaults, no side effects (e28s01).

    Shared by add_mapping (which assigns the id + appends) and sanitize_mapping
    (which heals a stored row onto the exact runtime shape) so the persistence
    schema can never drift from the live model.
    """
    spec = _spec_of(prop)
    return {
        "id": mapping_id,
        "target_id": target_id,
        "property": prop,
        "control": control,
        "value": _midpoint(spec["min"], spec["max"]),
        "band": None,  # e18: audio-band source (2 or 3), exclusive with midi/leap
        "midi": None,  # e18: learned MIDI source {device, type, number}
        "leap": None,  # e26s03: leap signal '<hand>.<field>' (e.g. 'left.pinch')
        # e23: value remap. output_from/to = the OSC range the control travel
        # sweeps (default = the vimix catalog range; editable to sub-ranges or
        # reversed). input_from/to = the raw source range a bound band/MIDI
        # source maps through (seeded on bind: band 0..1, MIDI 0..127).
        # e24: enabled = the mapping's master switch (default False: the
        # control stores values but sends no OSC until armed).
        "output_from": spec["min"],
        "output_to": spec["max"],
        "input_from": None,
        "input_to": None,
        "enabled": False,
    }


def add_mapping(target_id: str, prop: str, control: str) -> dict[str, Any]:
    """Create a mapping entry and append it to the mapper state (e16s01)."""
    state.mapper_counter += 1
    mapping = _build_mapping(state.mapper_counter, target_id, prop, control)
    state.mapper_mappings.append(mapping)
    return mapping


def capture_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """The persisted projection of one mapping (e28s01): the model keys only.

    The schema boundary between the runtime model and a project file — every
    key a mapping can carry lives in MAPPER_PERSISTED_KEYS, so the projection
    is a deep copy of exactly those keys.
    """
    return {key: copy.deepcopy(mapping[key]) for key in MAPPER_PERSISTED_KEYS if key in mapping}


def _to_float_or(value: Any, default: float) -> float:
    """Coerce a stored value to float, falling back on garbage (e28s01)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sanitize_mapping(raw: Any) -> dict[str, Any] | None:
    """Heal one stored mapping row onto the live runtime shape (e28s01).

    A row that cannot become a valid mapping (unknown property or control,
    non-dict input) returns None so the loader drops it. Valid rows heal every
    missing field to the model default, restore the FIRST present source
    (band > midi > leap — the model is exclusive), seed the input range like
    the bind functions when a source exists without one, clamp the value into
    the (possibly reversed) output interval and drop unknown keys.
    """
    if not isinstance(raw, dict):
        return None
    prop = str(raw.get("property") or "")
    control = str(raw.get("control") or "")
    if prop not in MAPPER_PROPERTIES or control not in MAPPER_CONTROLS:
        return None
    spec = MAPPER_PROPERTIES[prop]
    target_id = str(raw.get("target_id") or "") or None
    mapping = _build_mapping(int(raw.get("id") or 0), target_id, prop, control)
    if raw.get("band") in (2, 3):
        mapping["band"] = int(raw["band"])
    elif isinstance(raw.get("midi"), dict):
        midi = raw["midi"]
        mapping["midi"] = {
            "device": str(midi.get("device") or "") or None,
            "type": str(midi.get("type") or ""),
            "number": int(midi.get("number") or 0),
        }
    elif raw.get("leap"):
        mapping["leap"] = str(raw["leap"])
    if mapping["band"] is not None or mapping["midi"] is not None or mapping["leap"] is not None:
        if mapping["band"] is not None:
            seed_from, seed_to = 0.0, 1.0
        elif mapping["midi"] is not None:
            seed_from, seed_to = 0.0, 127.0
        else:
            parts = leap.binding_parts(mapping["leap"] or "")
            seed_from, seed_to = 0.0, 1.0
            if parts is not None:
                seed_from, seed_to = leap.signal_default_range(parts[1]) or (0.0, 1.0)
        mapping["input_from"] = _to_float_or(raw.get("input_from"), seed_from)
        mapping["input_to"] = _to_float_or(raw.get("input_to"), seed_to)
    mapping["output_from"] = _to_float_or(raw.get("output_from"), spec["min"])
    mapping["output_to"] = _to_float_or(raw.get("output_to"), spec["max"])
    lo, hi = _output_bounds(mapping)
    neutral = _midpoint(spec["min"], spec["max"])
    mapping["value"] = _clamp(_to_float_or(raw.get("value"), neutral), lo, hi)
    mapping["enabled"] = bool(raw.get("enabled", False))
    return mapping


def restore_mappings(rows: Any) -> list[dict[str, Any]]:
    """Replace the live mapper with sanitized project rows (e28s01).

    Order is preserved (render order), invalid rows drop, the list is capped at
    MAPPER_MAX_MAPPINGS, and the id counter resumes above the max restored id
    so add_mapping never collides. Worker-safe: no dpg. A non-list (or None)
    empties the mapper and keeps the current counter.
    """
    restored: list[dict[str, Any]] = []
    max_id = state.mapper_counter
    if isinstance(rows, list):
        for raw in rows[:MAPPER_MAX_MAPPINGS]:
            mapping = sanitize_mapping(raw)
            if mapping is None:
                continue
            restored.append(mapping)
            if mapping["id"] > max_id:
                max_id = mapping["id"]
    state.mapper_mappings[:] = restored
    state.mapper_counter = max_id
    return restored


def remove_mapping(mapping_id: int) -> None:
    """Remove a mapping by id; unknown ids are a no-op (e16s01)."""
    state.mapper_mappings[:] = [m for m in state.mapper_mappings if m["id"] != mapping_id]


def find_mapping(mapping_id: int) -> dict[str, Any] | None:
    """The mapping with the given id, or None."""
    for m in state.mapper_mappings:
        if m["id"] == mapping_id:
            return m
    return None


def prune_mappings(live_ids: set[str]) -> list[dict[str, Any]]:
    """Drop mappings whose source no longer exists; returns the removed entries.

    Empty list when nothing was pruned — the L-1 live-sources prune in
    ``update_vimix_sources_ui`` calls this so a removed source takes its
    mappings with it automatically.
    """
    removed = [m for m in state.mapper_mappings if m["target_id"] not in live_ids]
    if removed:
        removed_ids = {m["id"] for m in removed}
        state.mapper_mappings[:] = [m for m in state.mapper_mappings if m["id"] not in removed_ids]
    return removed


def retarget_source(old_target_id: str, new_target_id: str) -> int:
    """Re-point every mapping of a source row onto another source (e29s01).

    The Mapper row-thumb click (like the sequencer clip-slot click) applies
    the media-grid selection to a whole row: every mapping whose target_id
    equals old_target_id gets new_target_id, preserving list order, and the
    count of moved mappings is returned. OSC addresses derive from target_id
    at send time (mapper._send), so no stored address migrates; per-mapping
    property/control/value, band/MIDI/leap source, output/input ranges and the
    e24 enabled flag are untouched. Equal or empty ids and an unknown old
    target are no-ops (0) — the no-selection guard of the click lives in the
    composition root.
    """
    if not old_target_id or not new_target_id or old_target_id == new_target_id:
        return 0
    moved = 0
    for mapping in state.mapper_mappings:
        if mapping["target_id"] == old_target_id:
            mapping["target_id"] = new_target_id
            moved += 1
    return moved


def row_targets() -> list[str]:
    """The Mapper window's row order: distinct target ids, first appearance (e31s01).

    One source row per target in the exact order refresh_mapper_ui renders
    (e22s01). The composition root consumes this both for the body grouping
    and for the tile context-menu "Add to Mapper" line items, so a menu
    "line N" always names window row N. Falsy targets are skipped (they never
    reach the live state through add_mapping/sanitize_mapping).
    """
    seen: list[str] = []
    for mapping in state.mapper_mappings:
        target = mapping.get("target_id")
        if target and target not in seen:
            seen.append(target)
    return seen


def set_mapping_value(mapping_id: int, value: float) -> None:
    """Store a clamped value on the mapping (no OSC; e16s01).

    e23: the clamp bounds are the mapping's OUTPUT interval (min/max of the
    output range), not the catalog range — the stored value is the output.
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    lo, hi = _output_bounds(mapping)
    mapping["value"] = _clamp(float(value), lo, hi)


def set_mapping_output(mapping_id: int, out_from: float, out_to: float) -> None:
    """Set the OSC output range of a mapping (e23); from > to reverses the sweep.

    The stored value is re-clamped into the (possibly reversed) new interval.
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    mapping["output_from"] = float(out_from)
    mapping["output_to"] = float(out_to)
    lo, hi = _output_bounds(mapping)
    mapping["value"] = _clamp(mapping["value"], lo, hi)


def _output_bounds(mapping: dict[str, Any]) -> tuple[float, float]:
    """The sorted output interval of a mapping (e23): min/max of its range."""
    return min(mapping["output_from"], mapping["output_to"]), max(
        mapping["output_from"], mapping["output_to"]
    )


def toggle_mapping_value(mapping_id: int) -> float:
    """Button behavior: flip the stored value between output_from (OFF) and
    output_to (ON); returns the new value (e23s01).

    First press (the default midpoint, neither end) turns the button ON
    (output_to), matching the old 'first press = on' behaviour.
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    frm, to = mapping["output_from"], mapping["output_to"]
    if mapping["value"] == frm:
        new_value = to
    elif mapping["value"] == to:
        new_value = frm
    else:  # default/undetermined state: first press = ON
        new_value = to
    mapping["value"] = new_value
    return new_value


def set_mapping_enabled(mapping_id: int, enabled: bool) -> None:
    """Arm or mute a mapping (e24): disabled mappings send no OSC."""
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    mapping["enabled"] = bool(enabled)


def reset_mapping_value(mapping_id: int) -> float:
    """Reset a mapping to its neutral default and send it (e27s01).

    The neutral default is the value the mapping was CREATED at: the catalog
    midpoint of the property (brightness/alpha 0.0, hue 0.5, transparency
    1.0, gamma 0.0, ...). A button mapping returns to the OFF end
    (output_from) instead — the un-pressed state, consistent with
    toggle_mapping_value. A remapped output range (e23) clamps the neutral
    into the mapping's own interval, exactly like set_mapping_output
    re-clamps. Returns the effective stored value (0.0 for an unknown id);
    the e24 gate applies: a disabled mapping stores + moves but sends no OSC.
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    if mapping["control"] == "button":
        neutral = mapping["output_from"]
    else:
        spec = _spec_of(mapping["property"])
        neutral = _midpoint(spec["min"], spec["max"])
    lo, hi = _output_bounds(mapping)
    mapping["value"] = _clamp(neutral, lo, hi)
    _send(mapping)
    return mapping["value"]


def _send(mapping: dict[str, Any]) -> None:
    """Send the mapping's current value to vimix and log it (worker-safe).

    e24: a DISABLED mapping is muted here — the value is stored by the caller
    and the control moves, but no OSC message leaves and nothing is logged
    until the mapping is enabled.
    """
    if not mapping.get("enabled", False):
        return
    addr = f"/vimix/{mapping['target_id']}/{mapping['property']}"
    osc_client.send_message(addr, float(mapping["value"]))
    append_log("OUT", f"{addr} [{mapping['value']:.2f}]")


def send_mapping_value(mapping_id: int, value: float) -> float:
    """Store (clamped) + send a slider/knob value; returns the effective value."""
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    set_mapping_value(mapping_id, value)
    _send(mapping)
    return mapping["value"]


def send_button_mapping(mapping_id: int) -> float:
    """Toggle + send a button mapping; returns the new value (0.0 when unknown)."""
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    new_value = toggle_mapping_value(mapping_id)
    _send(mapping)
    return new_value


def set_mapping_band(mapping_id: int, band_id: int | None) -> None:
    """Set/clear the audio-band source of a mapping (e18).

    Band, MIDI and leap sources are mutually exclusive (e26s03): setting a
    band clears any MIDI or leap source. e23s02: binding a band seeds the
    input range to 0..1 (the raw band level scale).
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    mapping["band"] = band_id
    if band_id is not None:
        mapping["midi"] = None
        mapping["leap"] = None
        mapping["input_from"] = 0.0
        mapping["input_to"] = 1.0


def set_mapping_midi(mapping_id: int, binding: dict[str, Any]) -> None:
    """Set the MIDI source of a mapping from a learned binding (e18).

    e23s02: binding a MIDI source seeds the input range to 0..127 (the raw
    MIDI value scale). e26s03: also clears mapping['leap'] (three-way
    exclusivity).
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    mapping["midi"] = {
        "device": binding.get("device"),
        "type": binding.get("type"),
        "number": binding.get("number"),
    }
    mapping["band"] = None
    mapping["leap"] = None
    mapping["input_from"] = 0.0
    mapping["input_to"] = 127.0


def set_mapping_leap(mapping_id: int, signal_key: str) -> None:
    """Set the Leap Motion source of a mapping from a signal key (e26s03).

    ``'<hand>.<field>'`` from the leap catalog (e.g. 'left.pinch'), exclusive
    with band/midi. Binding seeds the input range from the signal's catalog
    default (palm mm, velocity mm/s, normal -1..1, pinch/grab/confidence
    0..1), like the band 0..1 and MIDI 0..127 seeding.
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    mapping["leap"] = signal_key
    mapping["band"] = None
    mapping["midi"] = None
    parts = leap.binding_parts(signal_key)
    if parts is not None:
        frm, to = leap.signal_default_range(parts[1]) or (0.0, 1.0)
    else:
        frm, to = 0.0, 1.0
    mapping["input_from"] = frm
    mapping["input_to"] = to


def set_mapping_input(mapping_id: int, in_from: float, in_to: float) -> None:
    """Set the input range a bound source maps through (e23s02); from > to reverses it."""
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    mapping["input_from"] = float(in_from)
    mapping["input_to"] = float(in_to)


def clear_mapping_source(mapping_id: int) -> None:
    """Drop all three external sources; the control returns to manual (e18/e26s03)."""
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    mapping["band"] = None
    mapping["midi"] = None
    mapping["leap"] = None


def apply_input_value(mapping_id: int, raw: float) -> float:
    """Drive a mapping from a raw source value (band level / MIDI value, e23s02).

    The raw value is remapped through the mapping's input range onto a clamped
    0..1 unit, then through the output range (apply_unit_value) and sent as
    OSC. Sub-ranges restrict the travel, reversed ranges (from > to) invert
    the response; a degenerate range yields unit 0 (output_from). Returns the
    effective value (0.0 for an unknown id). Worker-safe, HIGH-1.
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    in_from, in_to = mapping["input_from"], mapping["input_to"]
    if in_from is None or in_to is None or in_from == in_to:
        unit = 0.0
    else:
        unit = _clamp((float(raw) - in_from) / (in_to - in_from), 0.0, 1.0)
    return apply_unit_value(mapping_id, unit)


def apply_unit_value(mapping_id: int, unit: float) -> float:
    """Drive a mapping from a clamped 0..1 unit value (e18).

    e23: the unit is remapped onto the mapping's OUTPUT range
    (output_from + unit*(output_to-output_from)), stored on the mapping and
    sent as OSC; a reversed output range sweeps the other way. Returns the
    effective value (0.0 for an unknown id). Worker-safe, HIGH-1.
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    u = _clamp(unit, 0.0, 1.0)
    value = mapping["output_from"] + u * (mapping["output_to"] - mapping["output_from"])
    mapping["value"] = value
    _send(mapping)
    return value
