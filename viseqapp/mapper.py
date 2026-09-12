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

from viseqapp import catalog, leap, state
from viseqapp.constants import (
    DEST_MIDI,
    DEST_OSC,
    DEST_VIMIX,
    DESTINATIONS,
    MAPPER_MAX_MAPPINGS,
    MAPPER_PERSISTED_KEYS,
    MIDI_MONITOR_OUTCOME_HOLD,
    MIDI_MONITOR_OUTCOME_MUTED,
    MIDI_MONITOR_OUTCOME_SENT,
    ORIGIN_CLOCK,
    ORIGIN_CONST,
    ORIGIN_CONTROL,
    ORIGIN_STATE,
    ORIGINS,
    ROUTE_DEFAULT_CADENCE_MS,
    ROUTE_DIRECTIONS,
    ROUTE_MIDI_EMIT_EPSILON,
    ROUTE_MIN_CADENCE_MS,
    ROUTE_OSC_EMIT_EPSILON,
    ROUTE_STEPS_CONTINUOUS,
)
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

MAPPER_CONTROL_ROUTE = "route"  # e40s01: a state/clock/const Route has no physical control kind
MAPPER_CONTROLS: tuple[str, ...] = (
    "slider",
    "knob",
    "button",
    "cue list",  # e34s04: a button-like trigger whose card opens its cue-list window
    MAPPER_CONTROL_ROUTE,  # e40s01: Route rows driven by a State/Clock/Constant Origin
)

# e35s01: the extensible cue row kinds — the window editor and the engine
# iterate this tuple so new kinds stay additive (user: "wait", "mapper", ecc.).
CUE_ROW_KINDS: tuple[str, str, str, str] = ("property", "wait", "mapper", "burst")

# e34s04: control -> widget-tag kind — the tags the UI derives from a control
# (mapper_slider_N / mapper_knob_N / mapper_btn_N / mapper_cue_N) come from ONE
# map so the renderer, the right-click registry, the value relabel and the
# sync/reconfigure paths can never disagree. "btn" is the e23 naming quirk.
MAPPER_CONTROL_TAG_KINDS: dict[str, str] = {
    "slider": "slider",
    "knob": "knob",
    "button": "btn",
    "cue list": "cue",
    MAPPER_CONTROL_ROUTE: "route",  # e40s01: Route rows render their own row editor
}


def mappable_properties() -> list[str]:
    """Catalog properties a Mapper mapping may target (e36s03).

    Everything except the RATE family — loom/turn/grab/resize/ffwd are
    Cue-only (user decision 2). Order = catalog order (the New-Mapping dialog
    lists exactly this).
    """
    return [
        prop
        for prop, entry in catalog.PROPERTY_CATALOG.items()
        if entry["family"] != catalog.FAMILY_RATE
    ]


def control_tag_kind(control: str) -> str:
    """The widget-tag kind of a control (KeyError = catalog bug, not a user path)."""
    return MAPPER_CONTROL_TAG_KINDS[control]


def button_like(control: str) -> bool:
    """True for the momentary two-state controls (e34s04): the button and the
    cue-list trigger share the toggle/reset/OFF-end semantics."""
    return control in ("button", "cue list")


def _component_spec(prop: str, component: str | None) -> dict[str, Any]:
    """The catalog spec of one mapping component (KeyError = catalog bug).

    e36s02: multi-value properties (position/size/color/corner, the rate
    vectors grab/resize) resolve the component key; scalar/toggle/trigger/
    enum properties always resolve their single component.
    """
    entry = catalog.PROPERTY_CATALOG[prop]
    key = component if component is not None else entry["components"][0]["key"]
    for c in entry["components"]:
        if c["key"] == key:
            return dict(c)
    raise KeyError(f"component {key!r} of {prop!r}")


def _mapping_component_spec(mapping: dict[str, Any]) -> dict[str, Any]:
    """The component spec a mapping controls (vector rows store their key)."""
    return _component_spec(str(mapping["property"]), mapping.get("component"))


def _clamp(value: float, prop_min: float, prop_max: float) -> float:
    """Clamp a value into the vimix contract range of the property."""
    return max(prop_min, min(prop_max, value))


def _midpoint(prop_min: float, prop_max: float) -> float:
    """The neutral default for a property (brightness 0.0, alpha 0.0, ...)."""
    return (prop_min + prop_max) / 2.0


def fresh_cue() -> dict[str, Any]:
    """A pristine cue: no rows and the default 0 ms between levels (e35s01)."""
    return {"rows": [], "gap_ms": 0.0}


def cue_rows_empty(cue: dict[str, Any]) -> bool:
    """True when a cue has no rows (e35s01)."""
    return not cue.get("rows")


def _cue_level(raw: Any) -> int:
    """Coerce a stored level to int, clamped at 0 (never negative)."""
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _cue_property_payload(raw: Any) -> dict[str, Any] | None:
    """Heal a property payload against the catalog (e36s05).

    Any catalog property except the RATE family (rates belong to burst rows):
    scalar/enum/toggle/trigger carry {property, value} clamped into the
    component range; multi-value properties (position/size/color/corner)
    carry the FULL {property, values} vector with every component clamped. An
    optional {ms} > 0 rides ms-capable properties (the native animation
    duration); toggle/trigger/enum/seek never animate so a stray ms drops.
    """
    if not isinstance(raw, dict):
        return None
    prop = str(raw.get("property") or "")
    entry = catalog.PROPERTY_CATALOG.get(prop)
    if entry is None or entry["family"] == catalog.FAMILY_RATE:
        return None
    comps = entry["components"]
    out: dict[str, Any] = {"property": prop}
    if len(comps) > 1:
        values = raw.get("values")
        if not isinstance(values, list) or len(values) != len(comps):
            return None
        healed: list[float] = []
        for comp, value in zip(comps, values, strict=True):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            healed.append(_clamp(value, float(comp["min"]), float(comp["max"])))
        out["values"] = healed
    else:
        comp = comps[0]
        value = raw.get("value")
        if value is None:
            value = comp["neutral"]
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = comp["neutral"]
        out["value"] = _clamp(value, float(comp["min"]), float(comp["max"]))
    if entry["ms"]:
        ms = raw.get("ms")
        try:
            ms = float(ms) if ms is not None else 0.0
        except (TypeError, ValueError):
            ms = 0.0
        if ms > 0.0:
            out["ms"] = ms
    return out


def _cue_burst_payload(raw: Any) -> dict[str, Any] | None:
    """Heal a burst payload: a rate-family property + duration ms > 0.

    A burst runs ONE bounded rate message (loom/turn/grab/resize/ffwd with
    ms) — a duration is REQUIRED because a no-duration rate message is a
    single-frame nudge (spike). Values: single float for scalar rates, the
    full values list for the vector rates grab/resize.
    """
    if not isinstance(raw, dict):
        return None
    prop = str(raw.get("property") or "")
    entry = catalog.PROPERTY_CATALOG.get(prop)
    if entry is None or entry["family"] != catalog.FAMILY_RATE:
        return None
    ms = raw.get("ms")
    try:
        ms = float(ms) if ms is not None else 0.0
    except (TypeError, ValueError):
        return None
    if ms <= 0.0:
        return None
    comps = entry["components"]
    out: dict[str, Any] = {"property": prop, "ms": ms}
    if len(comps) > 1:
        values = raw.get("values")
        if not isinstance(values, list) or len(values) != len(comps):
            return None
        healed: list[float] = []
        for comp, value in zip(comps, values, strict=True):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            healed.append(_clamp(value, float(comp["min"]), float(comp["max"])))
        out["values"] = healed
    else:
        comp = comps[0]
        value = raw.get("value")
        try:
            value = float(value) if value is not None else comp["neutral"]
        except (TypeError, ValueError):
            value = comp["neutral"]
        out["value"] = _clamp(value, float(comp["min"]), float(comp["max"]))
    return out


def _cue_wait_payload(raw: Any) -> dict[str, Any] | None:
    """Heal a wait payload: non-negative ms."""
    if not isinstance(raw, dict):
        return None
    ms = raw.get("ms")
    if ms is None:
        return None
    try:
        ms = float(ms)
    except (TypeError, ValueError):
        return None
    return {"ms": max(0.0, ms)}


def _cue_mapper_payload(raw: Any) -> dict[str, Any] | None:
    """Heal a mapper payload: a positive target mapping id."""
    if not isinstance(raw, dict):
        return None
    mapping_id = raw.get("mapping_id")
    if mapping_id is None:
        return None
    try:
        mapping_id = int(mapping_id)
    except (TypeError, ValueError):
        return None
    if mapping_id < 1:
        return None
    return {"mapping_id": mapping_id}


_CUE_PAYLOAD_HEALERS: dict[str, Any] = {
    "property": _cue_property_payload,
    "wait": _cue_wait_payload,
    "mapper": _cue_mapper_payload,
    "burst": _cue_burst_payload,  # e36s05
}


def _sanitize_cue_row(raw: Any) -> dict[str, Any] | None:
    """One row onto the canonical shape; None for an unknown kind or unusable
    payload (the row drops, mirroring the mapping sanitizer philosophy)."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "")
    healer = _CUE_PAYLOAD_HEALERS.get(kind)
    if healer is None:
        return None
    payload = healer(raw.get("payload"))
    if payload is None:
        return None
    return {"kind": kind, "level": _cue_level(raw.get("level")), "payload": payload}


def sanitize_cue(raw: Any) -> dict[str, Any]:
    """Heal a stored cue onto the runtime shape (e35s01).

    Missing or garbage input yields fresh_cue(); rows with unknown kinds or
    unusable payloads drop; levels coerce to int >= 0 and the gap clamps >= 0.
    """
    if not isinstance(raw, dict):
        return fresh_cue()
    rows_raw = raw.get("rows")
    if isinstance(rows_raw, list):
        rows = [row for r in rows_raw if (row := _sanitize_cue_row(r)) is not None]
    else:
        rows = []
    gap_raw = raw.get("gap_ms")
    try:
        gap_ms = float(gap_raw) if gap_raw is not None else 0.0
    except (TypeError, ValueError):
        gap_ms = 0.0
    return {"rows": rows, "gap_ms": max(0.0, gap_ms)}


def add_cue_row(
    cue: dict[str, Any], kind: str, payload: Any, level: int = 0
) -> dict[str, Any] | None:
    """Append one sanitized row to a cue; None for an unknown kind or unusable
    payload leaves the cue unchanged. Worker-safe (e35s01)."""
    row = _sanitize_cue_row({"kind": kind, "level": level, "payload": payload})
    if row is None:
        return None
    cue["rows"].append(row)
    return row


def remove_cue_row(cue: dict[str, Any], index: int) -> None:
    """Drop the row at index; an out-of-range index is a no-op (e35s01)."""
    rows = cue.get("rows", [])
    if 0 <= index < len(rows):
        del rows[index]


def shift_cue_row_level(cue: dict[str, Any], index: int, delta: int) -> int | None:
    """Move a row's level by delta, clamped at 0; returns the new level, or
    None for an out-of-range index (e35s01)."""
    rows = cue.get("rows", [])
    if not 0 <= index < len(rows):
        return None
    row = rows[index]
    level = max(0, int(row["level"]) + int(delta))
    row["level"] = level
    return level


def quantize_unit(unit: float, steps: int) -> float:
    """Quantise a 0..1 unit into ``steps`` segments (e40s01, ADR decision 6).

    ``steps <= 1`` is continuous (the unit is only clamped); ``2`` is on/off;
    ``N`` yields N evenly spaced levels, so an LED bar can be lit segment by
    segment without the user touching epsilon or the rate cap.
    """
    clamped = _clamp(float(unit), 0.0, 1.0)
    if int(steps) <= ROUTE_STEPS_CONTINUOUS:
        return clamped
    segments = int(steps) - 1
    return round(clamped * segments) / segments


def route_output_value(route: dict[str, Any], raw: float) -> float:
    """The Destination value a raw Origin value maps to (pure, no send, e40s01).

    Mirrors the dispatch math (``_raw_unit`` zone ownership + quantisation +
    output range); a value outside the Origin window HOLDS the current value.
    """
    unit = _raw_unit(route, raw)
    if unit is None:
        return float(route["value"])
    unit = quantize_unit(unit, int(route.get("steps", ROUTE_STEPS_CONTINUOUS)))
    out_from, out_to = float(route["output_from"]), float(route["output_to"])
    return out_from + unit * (out_to - out_from)


def prop_flag(value: Any) -> bool:
    """A live property read as a boolean (play/lock are 0/1 floats or strings)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) > 0.5
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def dead_reckon(
    route: dict[str, Any], last_value: float, elapsed_s: float, props: dict[str, Any]
) -> float:
    """Extrapolate a derivable State Origin between refreshes (e40s01).

    ADR decision 5: the catalog marks the properties viseq may extrapolate
    (``seek`` is a linear timeline position). While the source plays, the value
    advances by ``speed * elapsed`` from the last REAL value, clamped to the
    Origin window; a paused source, a non-derivable property and a negative
    elapsed all return the last value unchanged.
    """
    if not catalog.is_derivable(str(route["property"])) or not prop_flag(props.get("play")):
        return float(last_value)
    speed = props.get("speed")
    speed = float(speed) if isinstance(speed, (int, float)) else 1.0
    lo, hi = _input_window(float(route["input_from"]), float(route["input_to"]))
    return _clamp(float(last_value) + speed * max(0.0, float(elapsed_s)), lo, hi)


def route_emit_epsilon(destination: str) -> float:
    """The minimum Destination value change that makes a Route emit (e40s01).

    ADR decision 6: a named constant per Destination kind — one MIDI step for
    notes/CC (an integer scale), a real float change for OSC.
    """
    if destination == DEST_MIDI:
        return ROUTE_MIDI_EMIT_EPSILON
    return ROUTE_OSC_EMIT_EPSILON


def origin_of(mapping: dict[str, Any]) -> str:
    """The Route Origin of a mapping; a legacy row (no key) is Control (e40s01)."""
    origin = mapping.get("origin", ORIGIN_CONTROL)
    return origin if origin in ORIGINS else ORIGIN_CONTROL


def destination_of(mapping: dict[str, Any]) -> str:
    """The Route Destination of a mapping; a legacy row (no key) is Vimix (e40s01)."""
    destination = mapping.get("destination", DEST_VIMIX)
    return destination if destination in DESTINATIONS else DEST_VIMIX


def _default_input_range(origin: str, spec: dict[str, Any]) -> tuple[float | None, float | None]:
    """The default Origin window of a Route (e40s01).

    A State Origin reads a catalog property, so its window is the property
    range; Clock/Constant Origins produce a 0..1 unit; a Control Origin has no
    window until a band/MIDI/Leap source seeds it.
    """
    if origin == ORIGIN_STATE:
        return float(spec["min"]), float(spec["max"])
    if origin in (ORIGIN_CLOCK, ORIGIN_CONST):
        return 0.0, 1.0
    return None, None


def _default_output_range(destination: str, spec: dict[str, Any]) -> tuple[float, float]:
    """The default Destination range of a Route (e40s01).

    A MIDI Destination is the 0..127 value scale (note velocity / CC), an OSC
    Destination defaults to the 0..1 unit, and a Vimix Destination keeps the
    catalog range (today's behaviour, byte-identical).
    """
    if destination == DEST_MIDI:
        return 0.0, 127.0
    if destination == DEST_OSC:
        return 0.0, 1.0
    return float(spec["min"]), float(spec["max"])


def _build_mapping(
    mapping_id: int,
    target_id: str | None,
    prop: str,
    control: str,
    component: str | None = None,
    origin: str = ORIGIN_CONTROL,
    destination: str = DEST_VIMIX,
) -> dict[str, Any]:
    """A fresh mapping dict with the catalog defaults, no side effects (e28s01).

    e36s02: ``component`` selects the controlled component of a multi-value
    property (position X/Y, color R/G/B, corner A.x..D.y); scalar/toggle/
    trigger/enum properties store None. A vector property with no component
    given defaults to its FIRST component (whole-vector preset mappings are
    out of e36 scope). The default value and output range come from the
    component's catalog neutral and min/max — no-effect neutrals (speed 1.0,
    hue 0.0, posterize 0), not arithmetic midpoints. Shared by add_mapping
    and sanitize_mapping so the persistence schema never drifts.

    e40s01: a mapping is a Route (Origin -> Remap -> Destination). ``origin``
    and ``destination`` default to the legacy Control -> Vimix pair; a State
    Origin seeds its Origin window from the catalog property, an output
    Destination its range from the MIDI/OSC scale.
    """
    entry = catalog.PROPERTY_CATALOG[prop]
    if len(entry["components"]) > 1:
        keys = {c["key"] for c in entry["components"]}
        component = component if component is not None else entry["components"][0]["key"]
        if component not in keys:
            raise KeyError(f"component {component!r} of {prop!r}")
    else:
        component = None
    spec = _component_spec(prop, component)
    in_from, in_to = _default_input_range(origin, spec)
    out_from, out_to = _default_output_range(destination, spec)
    neutral = _clamp(float(spec["neutral"]), min(out_from, out_to), max(out_from, out_to))
    return {
        "id": mapping_id,
        # e40s01 Route discriminators (legacy rows hydrate to control -> vimix)
        "origin": origin,
        "destination": destination,
        "destination_spec": {},
        "origin_spec": {},
        "cadence": ROUTE_DEFAULT_CADENCE_MS if origin == ORIGIN_STATE else None,
        "steps": ROUTE_STEPS_CONTINUOUS,
        "target_id": target_id,
        "property": prop,
        "component": component,
        "control": control,
        "value": neutral,
        "band": None,  # e18: audio-band source (2 or 3), exclusive with midi/leap
        "midi": None,  # e18: learned MIDI source {device, type, number}
        "leap": None,  # e26s03: leap signal '<hand>.<field>' (e.g. 'left.pinch')
        # e23: value remap. output_from/to = the range the Destination sweeps
        # (catalog range for Vimix, 0..127 for MIDI; editable/reversible).
        # input_from/to = the Origin window (raw control range, or the catalog
        # property range for a State Origin).
        # e24: enabled = the mapping's master switch (default False: the
        # control stores values but sends no OSC until armed).
        "output_from": out_from,
        "output_to": out_to,
        "input_from": in_from,
        "input_to": in_to,
        "enabled": False,
        "cue": fresh_cue(),  # e35s01: uniform key set — inert for non-cue-list controls
    }


def _validate_route(origin: str, destination: str) -> None:
    """Reject an inconsistent Origin/Destination pair (developer error, e40s01).

    v1 keeps the directions of ROUTE_DIRECTIONS: a Vimix Destination needs a
    Control Origin (target_id is the write target), an output Destination needs
    a State/Clock/Constant Origin. A Control Origin driving MIDI/OSC is additive
    later (ADR-route-model).
    """
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin {origin!r} (expected one of {ORIGINS})")
    if destination not in DESTINATIONS:
        raise ValueError(f"unknown destination {destination!r} (expected one of {DESTINATIONS})")
    allowed = ROUTE_DIRECTIONS[origin]
    if destination not in allowed:
        raise ValueError(
            f"origin {origin!r} cannot use destination {destination!r} (v1 allows {allowed})"
        )


def add_mapping(
    target_id: str, prop: str, control: str, component: str | None = None
) -> dict[str, Any]:
    """Create a mapping entry and append it to the mapper state (e16s01).

    e36s02: ``component`` selects the axis/channel of a multi-value property
    (None = scalar property, or the FIRST component for a vector property).
    """
    state.mapper_counter += 1
    mapping = _build_mapping(state.mapper_counter, target_id, prop, control, component=component)
    state.mapper_mappings.append(mapping)
    return mapping


def add_route(
    origin: str,
    destination: str,
    *,
    target_id: str | None = None,
    prop: str = "alpha",
    destination_spec: dict[str, Any] | None = None,
    cadence: int | None = None,
    steps: int = ROUTE_STEPS_CONTINUOUS,
) -> dict[str, Any]:
    """Create a Route (e40s01) and append it to the mapper state.

    ``add_mapping`` remains the factory for the Control -> Vimix shape (its
    control kind is meaningful there); this one covers State/Clock/Constant
    Origins writing a MIDI/OSC Destination. ``destination_spec`` is kept as an
    opaque persisted dict (validated by the UI/transport that consumes it).
    """
    _validate_route(origin, destination)
    state.mapper_counter += 1
    mapping = _build_mapping(
        state.mapper_counter,
        target_id,
        prop,
        MAPPER_CONTROL_ROUTE,
        origin=origin,
        destination=destination,
    )
    if isinstance(destination_spec, dict):
        mapping["destination_spec"] = dict(destination_spec)
    if origin == ORIGIN_STATE and cadence is not None:
        mapping["cadence"] = max(ROUTE_MIN_CADENCE_MS, int(cadence))
    mapping["steps"] = max(ROUTE_STEPS_CONTINUOUS, int(steps))
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

    A row that cannot become a valid mapping (unknown property, inconsistent
    Origin/Destination pair or control kind, non-dict input) returns None so
    the loader drops it. Valid rows heal every missing field to the model
    default, restore the FIRST present source (band > midi > leap — the model
    is exclusive, Control Origins only), seed the Origin window like the bind
    functions (or from the catalog for a State Origin), clamp the value into
    the (possibly reversed) Destination interval and drop unknown keys.

    e40s01: a row without ``origin``/``destination`` is a legacy Control ->
    Vimix mapping, so old project files load unchanged.
    """
    if not isinstance(raw, dict):
        return None
    prop = str(raw.get("property") or "")
    if prop not in catalog.PROPERTY_CATALOG:
        return None
    origin = raw.get("origin", ORIGIN_CONTROL)
    destination = raw.get("destination", DEST_VIMIX)
    if not isinstance(origin, str) or origin not in ORIGINS:
        return None
    if not isinstance(destination, str) or destination not in DESTINATIONS:
        return None
    if destination not in ROUTE_DIRECTIONS[origin]:
        return None  # e40s01: inconsistent Origin/Destination pair
    control = str(raw.get("control") or "")
    if origin == ORIGIN_CONTROL:
        if control not in MAPPER_CONTROLS or control == MAPPER_CONTROL_ROUTE:
            return None
    else:
        if control not in ("", MAPPER_CONTROL_ROUTE):
            return None
        control = MAPPER_CONTROL_ROUTE
    entry = catalog.PROPERTY_CATALOG[prop]
    target_id = str(raw.get("target_id") or "") or None
    if len(entry["components"]) > 1:
        component = raw.get("component")
        keys = {c["key"] for c in entry["components"]}
        if not (isinstance(component, str) and component in keys):
            return None  # e36s02: a vector row without a usable component is unusable
    else:
        component = raw.get("component")
        if component not in (None, "v"):
            return None  # e36s02: scalar rows carry no component key
        component = None
    spec = _component_spec(prop, component)
    mapping = _build_mapping(
        int(raw.get("id") or 0),
        target_id,
        prop,
        control,
        component=component,
        origin=origin,
        destination=destination,
    )
    if origin == ORIGIN_CONTROL:
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
    elif origin != ORIGIN_CONTROL:
        # State/Clock/Constant: keep the Origin window seeded by _build_mapping
        mapping["input_from"] = _to_float_or(raw.get("input_from"), mapping["input_from"])
        mapping["input_to"] = _to_float_or(raw.get("input_to"), mapping["input_to"])
    out_from, out_to = _default_output_range(destination, spec)
    mapping["output_from"] = _to_float_or(raw.get("output_from"), out_from)
    mapping["output_to"] = _to_float_or(raw.get("output_to"), out_to)
    dest_spec = raw.get("destination_spec")
    mapping["destination_spec"] = dict(dest_spec) if isinstance(dest_spec, dict) else {}
    origin_spec = raw.get("origin_spec")
    mapping["origin_spec"] = dict(origin_spec) if isinstance(origin_spec, dict) else {}
    if origin == ORIGIN_STATE:
        cadence = _to_float_or(raw.get("cadence"), ROUTE_DEFAULT_CADENCE_MS)
        mapping["cadence"] = max(ROUTE_MIN_CADENCE_MS, int(cadence))
    else:
        mapping["cadence"] = None
    steps = _to_float_or(raw.get("steps"), ROUTE_STEPS_CONTINUOUS)
    mapping["steps"] = max(ROUTE_STEPS_CONTINUOUS, int(steps))
    lo, hi = _output_bounds(mapping)
    neutral = float(spec["neutral"])  # e36s02: catalog no-effect neutral
    mapping["value"] = _clamp(_to_float_or(raw.get("value"), neutral), lo, hi)
    mapping["enabled"] = bool(raw.get("enabled", False))
    mapping["cue"] = sanitize_cue(raw.get("cue"))  # e35s01: heal the per-mapping cue
    return mapping


def _drop_dangling_cue_rows(mappings: list[dict[str, Any]]) -> None:
    """Drop mapper rows whose target mapping vanished after a restore (e35s01).

    A cue row of kind 'mapper' references ANOTHER mapping by id; that mapping
    may legitimately appear later in the saved file, so the pass runs over the
    full restored list — never per row while restoring.
    """
    known = {int(m["id"]) for m in mappings}
    for mapping in mappings:
        cue = mapping.get("cue")
        if not isinstance(cue, dict):
            continue
        cue["rows"] = [
            row
            for row in cue.get("rows", [])
            if not (
                row.get("kind") == "mapper"
                and int(row.get("payload", {}).get("mapping_id") or 0) not in known
            )
        ]


def restore_mappings(rows: Any) -> list[dict[str, Any]]:
    """Replace the live mapper with sanitized project rows (e28s01).

    Order is preserved (render order), invalid rows drop, the list is capped at
    MAPPER_MAX_MAPPINGS, and the id counter resumes above the max restored id
    so add_mapping never collides. Worker-safe: no dpg. A non-list (or None)
    empties the mapper and keeps the current counter. e35s01: after the whole
    restore a cue 'mapper' row referencing a mapping id that no longer exists
    is dropped (dangling references).
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
    _drop_dangling_cue_rows(restored)  # e35s01: post-restore pass
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
    """Drop Control->Vimix mappings whose source is gone; DISABLE other Routes.

    Returns the removed entries (the L-1 live-sources prune in
    ``update_vimix_sources_ui`` so a removed source takes its input mappings
    with it automatically). e40s01 orphan policy (ADR-route-model): an orphan
    State Route keeps its setup and is DISABLED instead of deleted, so a source
    that comes back resumes its LED; source-less Routes (Clock/Constant,
    target_id None) are never affected.
    """
    removed: list[dict[str, Any]] = []
    for mapping in state.mapper_mappings:
        if mapping["target_id"] in live_ids or mapping["target_id"] is None:
            continue
        if origin_of(mapping) == ORIGIN_CONTROL:
            removed.append(mapping)
        else:
            mapping["enabled"] = False
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


def row_slots(card_count: int, per_line: int) -> list[list[int]]:
    """Visual-line groups of one mapper row's CARDS (e34s01).

    Chunks the card indices into lines of ``per_line`` cards. The per-row '+'
    is NOT a card-sized slot any more (2026-09-05 resize bug: it used to wrap
    like a 150 px card): it is a small trailing button appended to the row's
    last card line when the pixel room fits it (see add_fits_last_line); this
    helper only places the cards. ``per_line`` is the window-derived card
    capacity (values <= 0 degrade to 1).
    """
    per_line = max(1, int(per_line))
    return [list(range(i, min(i + per_line, card_count))) for i in range(0, card_count, per_line)]


def add_fits_last_line(
    card_count: int, per_line: int, card_area: int, pitch: int, add_pitch: int
) -> bool:
    """Whether the row's '+' fits at the END of its last card line (e34s01).

    ``card_area`` is the window content width minus the row-lead offset (the
    room the cards share), ``pitch`` one card plus its spacing, ``add_pitch``
    the '+' button plus its spacing. A full last line (card_count a multiple
    of per_line) still leaves ``card_area % pitch`` of room — usually enough
    for the small '+' — so the '+' stays on the line with the last mapper and
    only moves to its own narrow line when that remainder is smaller than the
    button (very narrow windows). No cards -> the '+' does not fit anywhere.
    """
    if card_count <= 0:
        return False
    per_line = max(1, int(per_line))
    last_count = card_count % per_line
    if last_count == 0:
        last_count = per_line
    return card_area - last_count * pitch >= add_pitch


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
    no-effect neutral of its component (brightness/alpha/hue/gamma 0.0, speed
    1.0, posterize 0.0, transparency 1.0, color channels 1.0, ...). A
    button-like control (button, cue list — e34s04) returns to the OFF end
    (output_from) instead — the un-pressed state, consistent with
    toggle_mapping_value. A remapped output range (e23) clamps the neutral
    into the mapping's own interval, exactly like set_mapping_output
    re-clamps. Returns the effective stored value (0.0 for an unknown id);
    the e24 gate applies: a disabled mapping stores + moves but sends no OSC.
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    if button_like(mapping["control"]):  # e34s04: button + cue list reset to OFF
        neutral = mapping["output_from"]
    else:
        neutral = float(_mapping_component_spec(mapping)["neutral"])
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
    target_id = str(mapping["target_id"])
    prop = str(mapping["property"])
    entry = catalog.PROPERTY_CATALOG[prop]
    anchors = (
        catalog.anchor_get(state.source_anchors, target_id, prop)
        if entry["partial"] == catalog.PARTIAL_ANCHOR
        else None
    )
    args = catalog.compose_send_args(
        prop,
        component=mapping.get("component"),
        value=float(mapping["value"]),
        anchors=anchors,
    )
    if entry["partial"] == catalog.PARTIAL_ANCHOR and args and args[0] is not None:
        # remember the full vector we just sent so sibling component mappings
        # anchor their other channels on it (e36s02)
        catalog.anchor_update(state.source_anchors, target_id, prop, [float(a) for a in args])
    addr = f"/vimix/{target_id}/{prop}"
    if len(args) == 1 and args[0] is not None:
        osc_client.send_message(addr, float(args[0]))  # legacy scalar send shape
    else:
        osc_client.send_message(addr, list(args))
    append_log("OUT", f"{addr} {_format_args(args)}")


def _format_args(args: list[Any]) -> str:
    """Human log form: '[0.42]' scalar; '[N, 0.25]' nil-masked/anchor lists."""
    parts = []
    for a in args:
        parts.append("N" if a is None else f"{float(a):.2f}")
    return "[" + ", ".join(parts) + "]"


def send_mapping_value(mapping_id: int, value: float) -> float:
    """Store (clamped) + send a slider/knob value; returns the effective value."""
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    set_mapping_value(mapping_id, value)
    _send(mapping)
    return mapping["value"]


def send_button_mapping(mapping_id: int) -> float:
    """Press a button mapping; returns the stored value (0.0 when unknown).

    e36s02: a TRIGGER-family property (replay/reset/reload/flag) fires its
    message once without flipping the stored value; every other family keeps
    the toggle behaviour (flip output_from/OFF <-> output_to/ON).
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    if catalog.family_of(str(mapping["property"])) == catalog.FAMILY_TRIGGER:
        new_value = float(mapping["value"])
    else:
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


def _input_window(in_from: float, in_to: float) -> tuple[float, float]:
    """Sorted bounds of a mapping's input range (reversed ranges included)."""
    return min(in_from, in_to), max(in_from, in_to)


def has_input_window(mapping: dict[str, Any]) -> bool:
    """True when the mapping carries a usable input window (from != to)."""
    in_from, in_to = mapping.get("input_from"), mapping.get("input_to")
    return in_from is not None and in_to is not None and float(in_from) != float(in_to)


def input_in_window(mapping: dict[str, Any], raw: float) -> bool:
    """True when a raw source value falls inside the mapping's input window.

    Inclusive on both edges (a single-message range matches); a mapping without
    a usable window is never "in window" (callers treat it as undriven).
    """
    if not has_input_window(mapping):
        return False
    lo, hi = _input_window(float(mapping["input_from"]), float(mapping["input_to"]))
    return lo <= float(raw) <= hi


def _raw_unit(mapping: dict[str, Any], raw: float) -> float | None:
    """Unit 0..1 for a raw value, or None when the mapping must HOLD.

    A missing/degenerate input range yields unit 0.0 (output_from); a raw value
    outside a real window yields None (the e23/e27 zone-ownership hold).
    """
    if not has_input_window(mapping):
        return 0.0
    if not input_in_window(mapping, raw):
        return None
    in_from, in_to = float(mapping["input_from"]), float(mapping["input_to"])
    return _clamp((float(raw) - in_from) / (in_to - in_from), 0.0, 1.0)


def preview_mapping_value(mapping: dict[str, Any], raw: float) -> tuple[float, str]:
    """Pure diagnostic preview of what a raw value would do (e39s01).

    Mirrors apply_input_value WITHOUT sending: disabled -> MUTED (value
    unchanged), outside a real window -> HOLD (value unchanged), otherwise the
    value the mapping would take. Returns (effective_value, tag). Used by the
    MIDI Monitor; the dispatch path itself is untouched.
    """
    if not mapping.get("enabled", False):
        return float(mapping["value"]), MIDI_MONITOR_OUTCOME_MUTED
    unit = _raw_unit(mapping, raw)
    if unit is None:
        return float(mapping["value"]), MIDI_MONITOR_OUTCOME_HOLD
    value = float(mapping["output_from"]) + unit * (
        float(mapping["output_to"]) - float(mapping["output_from"])
    )
    return value, MIDI_MONITOR_OUTCOME_SENT


def apply_input_value(mapping_id: int, raw: float) -> float:
    """Drive a mapping from a raw source value (band level / MIDI value, e23s02).

    The raw value is remapped through the mapping's input range onto a clamped
    0..1 unit, then through the output range (apply_unit_value) and sent as
    OSC. Reversed ranges (from > to) sweep the other way inside the window.
    BUG-2026-09-07T160930 — ZONE OWNERSHIP: a raw value OUTSIDE the input
    window HOLDS the mapping (returns its current value, sends nothing)
    instead of pinning the nearest edge. Without the hold, two same-lever
    mappings writing one OSC address clamp-sent their edge on every message
    and overwrote each other — one slider looked dead. A degenerate range
    yields unit 0 (output_from). Returns the effective value (0.0 for an
    unknown id). Worker-safe, HIGH-1.
    """
    mapping = find_mapping(mapping_id)
    if mapping is None:
        return 0.0
    unit = _raw_unit(mapping, raw)
    if unit is None:
        return float(mapping["value"])  # outside my window: hold (zone ownership)
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


# e36s06: anchor seeding from the live vimix state feed.
# The anchored vectors (color/corner) are seeded from the state viOSC pushes
# (the same broadcast the monitor players poll) so sibling component mappings
# start from real values instead of the neutral white/identity vector.

_ANCHOR_SEED_PROPS: tuple[str, ...] = ("color", "corner")  # partial == anchor


def seed_anchors_from_live_state(sources: dict[str, Any]) -> int:
    """Seed missing (target, anchor-property) vectors from the live state.

    Only fills an empty slot — a vector written by a mapping send is never
    clobbered by the (older) feedback. Returns how many anchors were seeded.
    Targets are keyed by the source NAME (the address target_id), falling back
    to the grid index. Malformed/missing vectors are skipped.
    """
    seeded = 0
    store = state.source_anchors
    if not isinstance(sources, dict):
        return 0
    for idx, props in sources.items():
        if not isinstance(props, dict):
            continue
        name = props.get("name")
        target = str(name) if name else str(idx)
        existing = store.get(str(target)) or {}
        for prop in _ANCHOR_SEED_PROPS:
            if prop in existing:
                continue  # a mapping-sent vector is never clobbered by feedback
            raw = props.get(prop)
            if not isinstance(raw, (list, tuple)):
                continue
            try:
                values = [float(v) for v in raw]
            except (TypeError, ValueError):
                continue
            if len(values) == catalog.component_count(prop):
                store.setdefault(str(target), {})[prop] = values
                seeded += 1
    return seeded


def prune_anchors(live_ids: set[str]) -> int:
    """Drop anchor slots whose source no longer exists (mirrors prune_mappings)."""
    gone = [t for t in list(state.source_anchors) if t not in live_ids]
    for target in gone:
        del state.source_anchors[target]
    return len(gone)
