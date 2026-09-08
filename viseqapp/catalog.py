"""Component-aware OSC property catalog for viseq (e36s01).

Pure data + pure helpers — no dpg, no state, no I/O (HIGH-1). This module is
the single source of truth for the vimix source-attribute vocabulary (the two
OSC wiki sections "Attributes to control a source" and "…to control different
types of sources", strings parked by user decision). It replaces what the flat
single-float ``mapper.MAPPER_PROPERTIES`` table cannot express: families,
per-component ranges, the partial-set strategy each property supports (nil
mask vs anchored vector — verified on the live rig, SPIKE-osc-source-attrs),
the optional-ms animation flag and enum options.

Every entry: {family, components:[{key,label,min,max,neutral}], partial,
ms, options}. Legacy property ids and label/range pairs for the 17 shared
attributes stay compatible with MAPPER_PROPERTIES (posterize min is the one
deliberate difference: the catalog can express 0 = disabled).
"""

import math
from typing import Any

FAMILY_SET_SCALAR = "set_scalar"
FAMILY_SET_VEC = "set_vec"
FAMILY_RATE = "rate"
FAMILY_TOGGLE = "toggle"
FAMILY_TRIGGER = "trigger"
FAMILY_ENUM = "enum"
FAMILIES: tuple[str, ...] = (
    FAMILY_SET_SCALAR,
    FAMILY_SET_VEC,
    FAMILY_RATE,
    FAMILY_TOGGLE,
    FAMILY_TRIGGER,
    FAMILY_ENUM,
)

PARTIAL_NIL = "nil"
PARTIAL_ANCHOR = "anchor"
PARTIAL_NONE = "none"


def _scalar(
    label: str,
    lo: float,
    hi: float,
    neutral: float,
    *,
    family: str = FAMILY_SET_SCALAR,
    ms: bool = False,
    partial: str = PARTIAL_NONE,
    options: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    return {
        "family": family,
        "label": label,
        "components": [{"key": "v", "label": label, "min": lo, "max": hi, "neutral": neutral}],
        "partial": partial,
        "ms": ms,
        "options": list(options) if options else None,
    }


def _component(key: str, label: str, lo: float, hi: float, neutral: float) -> dict[str, Any]:
    return {"key": key, "label": label, "min": lo, "max": hi, "neutral": neutral}


def _vector(
    label: str,
    components: list[dict[str, Any]],
    *,
    partial: str,
    ms: bool = False,
    family: str = FAMILY_SET_VEC,
) -> dict[str, Any]:
    return {
        "family": family,
        "label": label,
        "components": components,
        "partial": partial,
        "ms": ms,
        "options": None,
    }


PI = math.pi
_FULL = (-5.0, 5.0)  # geometry position span (vimix panel slider range)

PROPERTY_CATALOG: dict[str, dict[str, Any]] = {
    # --- set scalar (instant absolute value, optional ms animation) ---
    "alpha": _scalar("Alpha", -1.0, 1.0, 0.0, ms=True),
    "transparency": _scalar("Transparency", 0.0, 2.0, 1.0, ms=True),
    "depth": _scalar("Depth", 0.0, 12.0, 6.0, ms=True),
    "angle": _scalar("Angle", -PI, PI, 0.0, ms=True),
    "brightness": _scalar("Brightness", -1.0, 1.0, 0.0, ms=True),
    "contrast": _scalar("Contrast", -1.0, 1.0, 0.0, ms=True),
    "saturation": _scalar("Saturation", -1.0, 1.0, 0.0, ms=True),
    "hue": _scalar("Hue", 0.0, 1.0, 0.0, ms=True),
    "gamma": _scalar("Gamma", -1.0, 1.0, 0.0, ms=True),
    "threshold": _scalar("Threshold", 0.0, 1.0, 0.0, ms=True),
    "posterize": _scalar("Posterize", 0.0, 256.0, 0.0, ms=True),  # 0 = disabled
    "seek": _scalar("Seek", 0.0, 1.0, 0.0),  # fraction; no animation
    "speed": _scalar("Speed", 0.1, 10.0, 1.0, ms=True),
    # --- set vector (whole or per-component, optional ms) ---
    "position": _vector(
        "Position",
        [_component("x", "X", *_FULL, 0.0), _component("y", "Y", *_FULL, 0.0)],
        partial=PARTIAL_NIL,
        ms=True,
    ),
    "size": _vector(
        "Size",
        [_component("w", "W", -10.0, 10.0, 1.0), _component("h", "H", -10.0, 10.0, 1.0)],
        partial=PARTIAL_NIL,
        ms=True,
    ),
    "color": _vector(
        "Color",
        [
            _component("r", "R", 0.0, 1.0, 1.0),
            _component("g", "G", 0.0, 1.0, 1.0),
            _component("b", "B", 0.0, 1.0, 1.0),
        ],
        partial=PARTIAL_ANCHOR,  # spike: color rejects nil, needs anchors
        ms=True,
    ),
    "corner": _vector(
        "Corner",
        [
            # order A (lower-left) B (upper-left) C (lower-right) D (upper-right)
            _component("ax", "A.x", -1.0, 1.0, -1.0),
            _component("ay", "A.y", -1.0, 1.0, -1.0),
            _component("bx", "B.x", -1.0, 1.0, -1.0),
            _component("by", "B.y", -1.0, 1.0, 1.0),
            _component("cx", "C.x", -1.0, 1.0, 1.0),
            _component("cy", "C.y", -1.0, 1.0, -1.0),
            _component("dx", "D.x", -1.0, 1.0, 1.0),
            _component("dy", "D.y", -1.0, 1.0, 1.0),
        ],
        partial=PARTIAL_ANCHOR,  # spike: corner nil zeroes -> anchors only
        ms=True,
    ),
    # --- rate (bounded bursts with ms; never sustained without it — spike) ---
    "loom": _scalar("Loom", -2.0, 2.0, 0.0, family=FAMILY_RATE, ms=True),
    "turn": _scalar("Turn", -2.0 * PI, 2.0 * PI, 0.0, family=FAMILY_RATE, ms=True),
    "ffwd": _scalar("Fast forward", 0.0, 10000.0, 0.0, family=FAMILY_RATE, ms=True),
    "grab": _vector(
        "Grab",
        [_component("x", "X", *_FULL, 0.0), _component("y", "Y", *_FULL, 0.0)],
        family=FAMILY_RATE,
        partial=PARTIAL_NIL,
        ms=True,
    ),
    "resize": _vector(
        "Resize",
        [
            _component("x", "X", -10.0, 10.0, 0.0),
            _component("y", "Y", -10.0, 10.0, 0.0),
        ],
        family=FAMILY_RATE,
        partial=PARTIAL_NIL,
        ms=True,
    ),
    # --- toggle (bool float >0.5) ---
    "play": _scalar("Play", 0.0, 1.0, 0.0, family=FAMILY_TOGGLE),
    "pause": _scalar("Pause", 0.0, 1.0, 0.0, family=FAMILY_TOGGLE),
    "lock": _scalar("Lock", 0.0, 1.0, 0.0, family=FAMILY_TOGGLE),
    "correction": _scalar("Correction", 0.0, 1.0, 0.0, family=FAMILY_TOGGLE),
    # --- trigger (no-argument fire; flag has an optional target value) ---
    "replay": _scalar("Replay", 0.0, 1.0, 0.0, family=FAMILY_TRIGGER),
    "reset": _scalar("Reset geometry", 0.0, 1.0, 0.0, family=FAMILY_TRIGGER),
    "reload": _scalar("Reload", 0.0, 1.0, 0.0, family=FAMILY_TRIGGER),
    "flag": _scalar("Flag", -1.0, 100.0, -1.0, family=FAMILY_TRIGGER),
    # --- enum (integer-index forms; blending accepts f index, no string) ---
    "blending": _scalar(
        "Blending",
        0.0,
        7.0,
        0.0,
        family=FAMILY_ENUM,
        options=(
            "Normal",
            "Screen",
            "Subtract",
            "Multiply",
            "Hard light",
            "Soft light",
            "Soft subtract",
            "Lighten only",
        ),
    ),
    "invert": _scalar(
        "Invert",
        0.0,
        2.0,
        0.0,
        family=FAMILY_ENUM,
        options=("None", "RGB", "Luminance"),
    ),
}


def family_of(prop: str) -> str:
    """The family of a catalog property (KeyError = unknown, like _spec_of)."""
    return PROPERTY_CATALOG[prop]["family"]


def is_set_scalar(prop: str) -> bool:
    return family_of(prop) == FAMILY_SET_SCALAR


def is_set_vec(prop: str) -> bool:
    return family_of(prop) == FAMILY_SET_VEC


def is_rate(prop: str) -> bool:
    return family_of(prop) == FAMILY_RATE


def is_toggle(prop: str) -> bool:
    return family_of(prop) == FAMILY_TOGGLE


def is_trigger(prop: str) -> bool:
    return family_of(prop) == FAMILY_TRIGGER


def is_enum(prop: str) -> bool:
    return family_of(prop) == FAMILY_ENUM


def component_index(prop: str, key: str) -> int:
    """0-based index of a component key within the property."""
    for i, c in enumerate(PROPERTY_CATALOG[prop]["components"]):
        if c["key"] == key:
            return i
    raise KeyError(f"component {key!r} of {prop!r}")


def component_count(prop: str) -> int:
    return len(PROPERTY_CATALOG[prop]["components"])


def default_components(prop: str) -> list[float]:
    """The neutral full vector of a property (anchors fall back on this)."""
    return [float(c["neutral"]) for c in PROPERTY_CATALOG[prop]["components"]]


# --- anchor store helpers (pure: the store dict is passed in) ---------------
# state.source_anchors is the app-wide store (target_id -> property -> vector).


def anchor_get(store: dict[str, dict[str, list[float]]], target_id: str, prop: str) -> list[float]:
    """The stored full vector for (target, property), or the catalog neutral."""
    target_store = store.get(str(target_id)) or {}
    values = target_store.get(prop)
    if values is None:
        return default_components(prop)
    return list(values)


def anchor_update(
    store: dict[str, dict[str, list[float]]], target_id: str, prop: str, values: list[float]
) -> None:
    """Store a full vector for (target, property); wrong length is a bug."""
    expected = component_count(prop)
    if len(values) != expected:
        raise ValueError(f"anchor for {prop!r} needs {expected} values, got {len(values)}")
    store.setdefault(str(target_id), {})[prop] = [float(v) for v in values]


def with_component(
    store: dict[str, dict[str, list[float]]], target_id: str, prop: str, key: str, value: float
) -> list[float]:
    """The stored vector of (target, prop) with one component replaced."""
    merged = anchor_get(store, target_id, prop)
    merged[component_index(prop, key)] = float(value)
    return merged


# --- OSC argument composer ---------------------------------------------------


def compose_send_args(
    prop: str,
    *,
    component: str | None = None,
    value: float | None = None,
    values: list[float] | None = None,
    anchors: list[float] | None = None,
    ms: float | None = None,
) -> list[Any]:
    """The exact OSC argument list for one /vimix/<target>/<prop> message.

    Per family: set_scalar/enum/toggle -> [value]; trigger -> [] (flag: [value]
    when given); set_vec / vector-rate whole -> values (or the neutral vector);
    a single component of a nil-capable vector -> None for the other slots
    (vimix keeps those axes); a single component of an anchored vector (color,
    corner) -> the full vector from ``anchors`` with that component replaced.
    ``ms`` appends the native-animation duration when the catalog allows it
    (never on toggle/trigger/enum/seek). Legacy scalar sends (component None,
    no ms) stay exactly [value] — byte-identical to today's float send.
    """
    entry = PROPERTY_CATALOG[prop]
    family = entry["family"]
    is_vector = family == FAMILY_SET_VEC or (family == FAMILY_RATE and component_count(prop) > 1)

    if is_vector:
        if component is not None:
            idx = component_index(prop, component)
            if entry["partial"] == PARTIAL_NIL:
                args: list[Any] = [None] * component_count(prop)
                args[idx] = value
            else:  # PARTIAL_ANCHOR (and whole-fallback): resend full vector
                anchored: list[Any] = (
                    list(anchors) if anchors is not None else default_components(prop)
                )
                anchored[idx] = value
                args = anchored
        else:
            args = list(values) if values is not None else default_components(prop)
    elif family == FAMILY_TRIGGER:
        args = [value] if prop == "flag" and value is not None else []
    else:  # set_scalar, rate scalar, toggle, enum
        neutral = entry["components"][0]["neutral"]
        scalar = value
        if scalar is None and values:
            scalar = values[0]
        if family == FAMILY_ENUM and scalar is not None:
            scalar = float(round(scalar))  # index semantics: integer step at the boundary
        args = [scalar if scalar is not None else neutral]

    if (
        ms is not None
        and entry["ms"]
        and family
        in (
            FAMILY_SET_SCALAR,
            FAMILY_SET_VEC,
            FAMILY_RATE,
        )
    ):
        args = [*args, ms]
    return args
