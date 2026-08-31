"""Mapper core for viseq (e16): property catalog + mapping CRUD + OSC send.

A mapping associates a vimix source (target_id) with one source property and
a compact graphic control (slider/knob/button); moving the control sends the
corresponding OSC message to vimix. WORKER-SAFE: this module never imports
dpg (HIGH-1) — mapping state lives in ``state.mapper_mappings`` and OSC
sends go through the shared viOSC client plus the log queue, mirroring the
sequencer pattern.
"""

from typing import Any

from viseqapp import state
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


def add_mapping(target_id: str, prop: str, control: str) -> dict[str, Any]:
    """Create a mapping entry and append it to the mapper state (e16s01)."""
    spec = _spec_of(prop)
    state.mapper_counter += 1
    mapping = {
        "id": state.mapper_counter,
        "target_id": target_id,
        "property": prop,
        "control": control,
        "value": _midpoint(spec["min"], spec["max"]),
        "band": None,  # e18: audio-band source (2 or 3), exclusive with midi
        "midi": None,  # e18: learned MIDI source {device, type, number}
    }
    state.mapper_mappings.append(mapping)
    return mapping


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


def set_mapping_value(mapping_id: int, value: float) -> None:
    """Store a clamped value on the mapping (no OSC; e16s01)."""
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    spec = _spec_of(mapping["property"])
    mapping["value"] = _clamp(float(value), spec["min"], spec["max"])


def toggle_mapping_value(mapping_id: int) -> float:
    """Button behavior: flip the stored value between min and max; returns the new value."""
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    spec = _spec_of(mapping["property"])
    low, high = spec["min"], spec["max"]
    # Strict >: a mapping at the neutral midpoint (default) flips to max first
    # (first press = "on"), then alternates min/max.
    new_value = low if mapping["value"] > _midpoint(low, high) else high
    mapping["value"] = new_value
    return new_value


def _send(mapping: dict[str, Any]) -> None:
    """Send the mapping's current value to vimix and log it (worker-safe)."""
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

    Band and MIDI sources are mutually exclusive: setting a band clears any
    MIDI source, and vice versa.
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    mapping["band"] = band_id
    if band_id is not None:
        mapping["midi"] = None


def set_mapping_midi(mapping_id: int, binding: dict[str, Any]) -> None:
    """Set the MIDI source of a mapping from a learned binding (e18)."""
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    mapping["midi"] = {
        "device": binding.get("device"),
        "type": binding.get("type"),
        "number": binding.get("number"),
    }
    mapping["band"] = None


def clear_mapping_source(mapping_id: int) -> None:
    """Drop both external sources; the control returns to manual dragging (e18)."""
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return
    mapping["band"] = None
    mapping["midi"] = None


def apply_unit_value(mapping_id: int, unit: float) -> float:
    """Drive a mapping from a 0..1 unit value (band level / MIDI 0..127).

    The unit value is remapped onto the property range (min + unit*(max-min)),
    clamped, stored on the mapping and sent as OSC; returns the effective
    value (0.0 for an unknown id). Worker-safe, HIGH-1.
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    spec = _spec_of(mapping["property"])
    value = spec["min"] + _clamp(unit, 0.0, 1.0) * (spec["max"] - spec["min"])
    mapping["value"] = value
    _send(mapping)
    return value
