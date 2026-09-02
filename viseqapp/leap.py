"""Leap Motion engine for viseq (e26): curated signal catalog + tracking-frame
normalization + config mirrors. WORKER-SAFE: no dpg import (HIGH-1).

The external ``leap`` package (leapc-python-api) is NEVER imported at module
level — the suite/CI runs without it and the worker glue in the composition
root imports it lazily. This module only holds static catalog data, pure
value logic and config/state mirrors.

Snapshot model: one flat dict of floats keyed ``"<hand>.<field>"``
(e.g. ``"left.palm_y"``) written by the worker under ``state.leap_lock``.
``LEAP_FIELDS`` is the single metadata table: label/suffix/decimals for the
live monitor (e26s02) plus ``bindable`` and the default input range for the
mapper catalog (e26s03).

DEVIATION (live-verified 2026-09-02, Gemini 5.17.1.0 + LMC fw 1.7.0): the
service never populates LEAP_PALM.stabilized_position (it stays 0.0 even with
a hand held still for 20 s while position/velocity read real values), so the
``palm_*`` keys come from the RAW palm position (jitter accepted; rate-capped
driving + remap ranges keep mappings usable).
"""

from typing import Any

from viseqapp import state
from viseqapp.config import load_config, save_config

# The two tracked hands (HandType.Left/Right of the LeapC API).
LEAP_HANDS: tuple[str, ...] = ("left", "right")

# Per-field metadata, one entry per normalized snapshot field. bindable=True
# fields form the curated mapper catalog (18 per hand) and carry the default
# input range seeded on a mapping bind; monitor-only fields (raw position,
# distances, angles, timings) are shown by the live monitor but never bound.
LEAP_FIELDS: dict[str, dict[str, Any]] = {
    "palm_x": {
        "label": "Palm X",
        "suffix": "mm",
        "decimals": 1,
        "bindable": True,
        "input_from": -150.0,
        "input_to": 150.0,
    },
    "palm_y": {
        "label": "Palm Y",
        "suffix": "mm",
        "decimals": 1,
        "bindable": True,
        "input_from": -100.0,
        "input_to": 300.0,
    },
    "palm_z": {
        "label": "Palm Z",
        "suffix": "mm",
        "decimals": 1,
        "bindable": True,
        "input_from": 0.0,
        "input_to": 350.0,
    },
    "vel_x": {
        "label": "Velocity X",
        "suffix": "mm/s",
        "decimals": 0,
        "bindable": True,
        "input_from": -1500.0,
        "input_to": 1500.0,
    },
    "vel_y": {
        "label": "Velocity Y",
        "suffix": "mm/s",
        "decimals": 0,
        "bindable": True,
        "input_from": -1500.0,
        "input_to": 1500.0,
    },
    "vel_z": {
        "label": "Velocity Z",
        "suffix": "mm/s",
        "decimals": 0,
        "bindable": True,
        "input_from": -1500.0,
        "input_to": 1500.0,
    },
    "nrm_x": {
        "label": "Normal X",
        "suffix": "",
        "decimals": 2,
        "bindable": True,
        "input_from": -1.0,
        "input_to": 1.0,
    },
    "nrm_y": {
        "label": "Normal Y",
        "suffix": "",
        "decimals": 2,
        "bindable": True,
        "input_from": -1.0,
        "input_to": 1.0,
    },
    "nrm_z": {
        "label": "Normal Z",
        "suffix": "",
        "decimals": 2,
        "bindable": True,
        "input_from": -1.0,
        "input_to": 1.0,
    },
    "pinch": {
        "label": "Pinch",
        "suffix": "",
        "decimals": 2,
        "bindable": True,
        "input_from": 0.0,
        "input_to": 1.0,
    },
    "pinch_dist": {"label": "Pinch dist.", "suffix": "mm", "decimals": 1, "bindable": False},
    "grab": {
        "label": "Grab",
        "suffix": "",
        "decimals": 2,
        "bindable": True,
        "input_from": 0.0,
        "input_to": 1.0,
    },
    "grab_angle": {"label": "Grab angle", "suffix": "rad", "decimals": 2, "bindable": False},
    "conf": {
        "label": "Confidence",
        "suffix": "",
        "decimals": 2,
        "bindable": True,
        "input_from": 0.0,
        "input_to": 1.0,
    },
    "visible": {"label": "Visible", "suffix": "s", "decimals": 2, "bindable": False},
    "width": {"label": "Width", "suffix": "mm", "decimals": 1, "bindable": False},
    "ext_thumb": {
        "label": "Thumb",
        "suffix": "",
        "decimals": 0,
        "bindable": True,
        "input_from": 0.0,
        "input_to": 1.0,
    },
    "ext_index": {
        "label": "Index",
        "suffix": "",
        "decimals": 0,
        "bindable": True,
        "input_from": 0.0,
        "input_to": 1.0,
    },
    "ext_middle": {
        "label": "Middle",
        "suffix": "",
        "decimals": 0,
        "bindable": True,
        "input_from": 0.0,
        "input_to": 1.0,
    },
    "ext_ring": {
        "label": "Ring",
        "suffix": "",
        "decimals": 0,
        "bindable": True,
        "input_from": 0.0,
        "input_to": 1.0,
    },
    "ext_pinky": {
        "label": "Pinky",
        "suffix": "",
        "decimals": 0,
        "bindable": True,
        "input_from": 0.0,
        "input_to": 1.0,
    },
    "present": {
        "label": "Hand present",
        "suffix": "",
        "decimals": 0,
        "bindable": True,
        "input_from": 0.0,
        "input_to": 1.0,
    },
}

# Digit order of hand.digits (thumb..pinky) maps onto the ext_* field names.
_FINGER_FIELDS: tuple[str, ...] = ("thumb", "index", "middle", "ring", "pinky")

# e26s02: placeholder the live monitor shows while a hand is absent (no key in
# the snapshot) or the engine is disabled. ASCII hyphen: U+2014 em dash renders
# as a fallback glyph in ProggyClean (e13s01 convention).
LEAP_MONITOR_PLACEHOLDER: str = "-"


def format_value(field: str, value: float) -> str:
    """Render one snapshot value for the live monitor (e26s02).

    Decimals and the unit suffix come from the field metadata: 87.1 mm,
    0.43, 1.20 rad, 2.50 s, 1.
    """
    meta = leap_field(field)
    text = f"{float(value):.{int(meta['decimals'])}f}"
    suffix = str(meta.get("suffix") or "")
    return f"{text} {suffix}" if suffix else text


def leap_status_label(enabled: bool, status: str) -> str:
    """Status line text for the Leap Motion window (e26s02)."""
    if not enabled:
        return "Disabled"
    labels = {
        "missing": "Leap library/service not available",
        "disconnected": "Disconnected - retrying...",
        "connected": "Connected - waiting for a hand...",
        "tracking": "Tracking...",
    }
    return labels.get(status, status)


def leap_field(field: str) -> dict[str, Any]:
    """The metadata entry for a snapshot field (KeyError = catalog bug)."""
    return LEAP_FIELDS[field]


def bindable_signals() -> tuple[str, ...]:
    """The curated mapper catalog: field keys bindable as a hand signal (e26s03)."""
    return tuple(k for k, meta in LEAP_FIELDS.items() if meta.get("bindable"))


def signal_default_range(field: str) -> tuple[float, float] | None:
    """The default input range seeded when a mapping binds this signal; None for
    monitor-only fields or unknown keys."""
    meta = LEAP_FIELDS.get(field)
    if meta is None or not meta.get("bindable"):
        return None
    return (float(meta["input_from"]), float(meta["input_to"]))


def binding_key(hand: str, field: str) -> str:
    """The full mapper binding key for a hand signal ('left.pinch')."""
    return f"{hand}.{field}"


def binding_parts(key: str) -> tuple[str, str] | None:
    """Split a binding key into (hand, field); None for anything malformed."""
    if key.count(".") == 1:
        hand, field = key.split(".")
        if hand in LEAP_HANDS and field in LEAP_FIELDS:
            return hand, field
    return None


def binding_label(key: str) -> str:
    """Human label for a binding key ('Left · Pinch'); raw key when malformed."""
    parts = binding_parts(key)
    if parts is None:
        return key
    hand, field = parts
    return f"{hand.capitalize()} · {LEAP_FIELDS[field]['label']}"


def _hand_side(hand: Any) -> str:
    """'left'/'right' from a LeapC hand (str(hand.type) reads 'HandType.Left')."""
    return "left" if "left" in str(hand.type).lower() else "right"


def normalize_tracking_event(event: Any) -> dict[str, float]:
    """One flat float snapshot from a LeapC TrackingEvent.

    Keys are ``<hand>.<field>`` for EVERY present hand plus
    ``<hand>.present = 1.0``. The raw palm position feeds the palm_* keys
    (stabilized_position is never populated by the Gemini 5.17.1.0 service on
    the original controller — module docstring). A frame with no hands yields
    an empty dict; absent hands produce no keys.
    """
    out: dict[str, float] = {}
    for hand in event.hands or []:
        side = _hand_side(hand)
        palm = hand.palm
        pos, vel, nrm = palm.position, palm.velocity, palm.normal
        out.update(
            {
                f"{side}.present": 1.0,
                f"{side}.palm_x": float(pos.x),
                f"{side}.palm_y": float(pos.y),
                f"{side}.palm_z": float(pos.z),
                f"{side}.vel_x": float(vel.x),
                f"{side}.vel_y": float(vel.y),
                f"{side}.vel_z": float(vel.z),
                f"{side}.nrm_x": float(nrm.x),
                f"{side}.nrm_y": float(nrm.y),
                f"{side}.nrm_z": float(nrm.z),
                f"{side}.pinch": float(hand.pinch_strength),
                f"{side}.pinch_dist": float(hand.pinch_distance),
                f"{side}.grab": float(hand.grab_strength),
                f"{side}.grab_angle": float(hand.grab_angle),
                f"{side}.conf": float(hand.confidence),
                f"{side}.visible": float(hand.visible_time),
                f"{side}.width": float(palm.width),
            }
        )
        for idx, finger in enumerate(_FINGER_FIELDS):
            extended = bool(hand.digits[idx].is_extended)
            out[f"{side}.ext_{finger}"] = 1.0 if extended else 0.0
    return out


def leap_init_from_config(cfg: dict[str, Any]) -> None:
    """Load the Leap engine mirror from the config (boot, e26s01)."""
    leap_cfg = cfg.get("leap") or {}
    state.leap_enabled = bool(leap_cfg.get("enabled", False))


def set_leap_enabled(enabled: bool) -> None:
    """Enable/disable the Leap engine and persist the flag (main thread).

    The worker loop watches state.leap_enabled and opens/closes the LeapC
    connection on its own cadence, so no direct connect/disconnect happens
    here (mirrors the midi engine's set_midi_enabled contract).
    """
    state.leap_enabled = bool(enabled)
    cfg = load_config()
    cfg["leap"]["enabled"] = bool(enabled)
    save_config(cfg)
