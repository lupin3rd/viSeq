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

from itertools import pairwise
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

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


# e26s03: per-mapping drive caps. LEAP_DRIVE_INTERVAL = ~30 pushes/second per
# mapping; a raw change smaller than the mapping's input span / 1000 is noise
# (a still hand sends nothing).
LEAP_DRIVE_INTERVAL: float = 1.0 / 30.0
LEAP_DRIVE_EPSILON_DIVISOR: float = 1000.0


def drive_ready(
    now: float,
    last_push: float,
    interval: float,
    last_raw: float | None,
    raw: float,
    input_from: float,
    input_to: float,
) -> bool:
    """Should this mapping push now? (e26s03, pure gate)

    Rate cap: a push inside ``interval`` of the last one is skipped. Change
    epsilon: a raw delta below input_span/1000 is skipped. The first drive
    after a hand appears (no last_raw) always pushes; a degenerate input
    range (span 0) is interval-gated only.
    """
    if now - last_push < interval:
        return False
    if last_raw is None:
        return True
    span = abs(input_to - input_from)
    below_epsilon = span > 0.0 and abs(raw - last_raw) < span / LEAP_DRIVE_EPSILON_DIVISOR
    return not below_epsilon


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
    """Load the Leap engine mirrors from the config (boot, e26s01/e26s04)."""
    leap_cfg = cfg.get("leap") or {}
    state.leap_enabled = bool(leap_cfg.get("enabled", False))
    state.leap_visualizer = bool(leap_cfg.get("visualizer", False))


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


def set_leap_visualizer(enabled: bool) -> None:
    """Enable/disable the embedded visualizer and persist the flag (main thread).

    The worker loop watches state.leap_visualizer and sets/clears the LeapC
    Images policy on its own cadence, so no direct connection work happens
    here (mirrors set_leap_enabled). Off = the device never streams IR.
    """
    state.leap_visualizer = bool(enabled)
    cfg = load_config()
    cfg["leap"]["visualizer"] = bool(enabled)
    save_config(cfg)


# ---------- e26s04: embedded visualizer (pure, dpg-free) ----------
# One RGBA frame = IR camera panel | skeleton panel, composed with numpy+Pillow
# (both already runtime deps — no opencv; user decision 2026-09-03). The worker
# in the composition root feeds grayscale IR copies + plain geometry dicts, so
# this module never imports the external leap package. Output is the DPG
# texture format proven by the thumbnail pipeline (RGBA float32 0..1).

# Panel geometry fits the COMPACT 560-wide Leap Motion window (the window never
# widens, user decision): IR panel 400x150 (native 640x240 downscaled 0.625),
# skeleton panel 120x150 isotropic (0.3 px/mm over the example mm ranges).
LEAP_VIZ_IR_W: int = 400
LEAP_VIZ_IR_H: int = 150
LEAP_VIZ_SKEL_W: int = 120
LEAP_VIZ_SKEL_H: int = 150
LEAP_VIZ_GUTTER: int = 8
LEAP_VIZ_W: int = LEAP_VIZ_IR_W + LEAP_VIZ_GUTTER + LEAP_VIZ_SKEL_W
LEAP_VIZ_H: int = LEAP_VIZ_IR_H
LEAP_VIZ_FPS: float = 30.0
# Skeleton projection box in hand mm (the reference example's ranges).
LEAP_VIZ_X_RANGE: tuple[float, float] = (-200.0, 200.0)
LEAP_VIZ_Y_RANGE: tuple[float, float] = (0.0, 500.0)
# Per-side skeleton color (left orange / right cyan, as in the example).
LEAP_VIZ_HAND_COLORS: dict[str, tuple[int, int, int]] = {
    "left": (255, 120, 0),
    "right": (0, 200, 255),
}
LEAP_VIZ_WAIT_COLOR: tuple[int, int, int] = (255, 0, 0)

_VIZ_FONT: Any = ImageFont.load_default()


def viz_hand_geometry(hand: Any) -> dict[str, Any]:
    """Compact per-hand geometry for the visualizer (e26s04).

    Duck-typed on a LeapC hand: palm (x, y) mm, pinch/grab strengths and, per
    digit, the five bone-endpoint joints (x, y) mm. Called synchronously on
    the poll thread while the hand data is still valid (same UAF discipline as
    the tracking snapshot).
    """
    palm = hand.palm.position
    digits = []
    for digit in hand.digits:
        pts = [digit.bones[0].prev_joint]
        pts += [bone.next_joint for bone in digit.bones]
        digits.append([(float(p.x), float(p.y)) for p in pts])
    return {
        "side": _hand_side(hand),
        "palm": (float(palm.x), float(palm.y)),
        "pinch": float(hand.pinch_strength),
        "grab": float(hand.grab_strength),
        "digits": digits,
    }


def viz_render_due(now: float, last_render: float | None) -> bool:
    """Rate gate for the visualizer composite: at most LEAP_VIZ_FPS frames."""
    if last_render is None:
        return True
    return now - last_render >= 1.0 / LEAP_VIZ_FPS


def compose_viz_frame(ir: Any, hands: list[dict[str, Any]] | None) -> np.ndarray:
    """One RGBA float32 (LEAP_VIZ_H, LEAP_VIZ_W, 4) texture frame (e26s04).

    Left half: the IR camera panel (grayscale uint8 array -> RGB, downscaled);
    right half: the hand skeleton (geometry dicts from viz_hand_geometry).
    Missing halves draw a dark placeholder with an ASCII waiting label. All
    drawing happens here on the worker side; the main thread only uploads the
    finished frame (HIGH-1).
    """
    canvas = Image.new("RGBA", (LEAP_VIZ_W, LEAP_VIZ_H), (0, 0, 0, 255))
    _viz_draw_ir_panel(canvas, ir)
    _viz_draw_skeleton_panel(canvas, hands)
    return np.asarray(canvas, dtype=np.float32) / 255.0


def _viz_draw_ir_panel(canvas: Any, ir: Any) -> None:
    """Paste the downscaled IR frame into the left panel; label when absent."""
    box = (0, 0, LEAP_VIZ_IR_W, LEAP_VIZ_IR_H)
    if ir is None:
        _viz_placeholder(canvas, box, "Waiting for camera...")
        return
    arr = np.ascontiguousarray(ir, dtype=np.uint8)
    mode = "L" if arr.ndim == 2 else "RGB"
    img = Image.fromarray(arr, mode=mode).convert("RGB")
    img = img.resize((LEAP_VIZ_IR_W, LEAP_VIZ_IR_H), Image.Resampling.BILINEAR)
    canvas.paste(img, box)


def _viz_draw_skeleton_panel(canvas: Any, hands: list[dict[str, Any]] | None) -> None:
    """Draw each hand's skeleton on its own panel, pasted right of the IR."""
    panel = Image.new("RGBA", (LEAP_VIZ_SKEL_W, LEAP_VIZ_SKEL_H), (0, 0, 0, 255))
    if not hands:
        _viz_placeholder(
            panel,
            (0, 0, LEAP_VIZ_SKEL_W, LEAP_VIZ_SKEL_H),
            "Waiting for device...",
        )
    else:
        draw = ImageDraw.Draw(panel)
        for geom in hands:
            _viz_draw_hand(draw, geom)
    canvas.paste(panel, (LEAP_VIZ_IR_W + LEAP_VIZ_GUTTER, 0))


def _viz_draw_hand(draw: Any, geom: dict[str, Any]) -> None:
    """One hand on the skeleton panel: palm dot, bone lines, joint dots, label."""
    color = LEAP_VIZ_HAND_COLORS.get(geom["side"], (255, 255, 255))
    px, py = _viz_mm_to_px(geom["palm"][0], geom["palm"][1])
    draw.ellipse((px - 6, py - 6, px + 6, py + 6), fill=color)
    for digit in geom["digits"]:
        pts = [_viz_mm_to_px(x, y) for x, y in digit]
        for p1, p2 in pairwise(pts):
            draw.line((p1, p2), fill=color, width=2)
        for p in pts:
            draw.ellipse((p[0] - 2, p[1] - 2, p[0] + 2, p[1] + 2), fill=(255, 255, 255))
    label = f"{geom['side'][0].upper()} {geom['pinch']:.2f} {geom['grab']:.2f}"
    draw.text((2, max(0, py - 24)), label, fill=color, font=_VIZ_FONT)


def _viz_mm_to_px(x: float, y: float) -> tuple[int, int]:
    """Skeleton-box pixel for a hand mm point (y up, X/Y_RANGE projection)."""
    x0, x1 = LEAP_VIZ_X_RANGE
    y0, y1 = LEAP_VIZ_Y_RANGE
    px = int((x - x0) / (x1 - x0) * LEAP_VIZ_SKEL_W)
    py = int(LEAP_VIZ_SKEL_H - (y - y0) / (y1 - y0) * LEAP_VIZ_SKEL_H)
    return px, py


def _viz_placeholder(img: Any, box: tuple[int, int, int, int], label: str) -> None:
    """Centered dark-region waiting label (ASCII, default PIL font)."""
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    text = draw.textbbox((0, 0), label, font=_VIZ_FONT)
    tw = text[2] - text[0]
    th = text[3] - text[1]
    draw.text(
        (x0 + (x1 - x0 - tw) // 2, y0 + (y1 - y0 - th) // 2),
        label,
        fill=LEAP_VIZ_WAIT_COLOR,
        font=_VIZ_FONT,
    )


# ---------- e26s05: stall watchdog policy (pure, dpg-free) ----------
# Live-observed (2026-09-03): the LMC/Gemini service can wedge SILENTLY — the
# tracking evaluator freezes while the process + USB stay alive and the app's
# connection stays open, so no tracking events arrive and nothing flips the
# status. The service streams EMPTY tracking frames at ~115 Hz even with no
# hand in view, so a silence longer than LEAP_STALL_TIMEOUT is a genuine wedge,
# not an idle hand.
LEAP_STALL_TIMEOUT: float = 3.0  # s of tracking silence that means a stall
LEAP_STALL_RETRY: float = 2.0  # fast reconnect for the first attempts
LEAP_STALL_ESCALATED_RETRY: float = 30.0  # slow cadence once escalated
LEAP_STALL_ESCALATION_COUNT: int = 3  # attempts before escalation


def stall_detected(now: float, last_frame: float, timeout: float = LEAP_STALL_TIMEOUT) -> bool:
    """Has the tracking stream been silent for more than the timeout? (e26s05)

    The keep-alive refreshes last_frame on every tracking event; a wedge stops
    the stream, so the elapsed silence grows past the timeout.
    """
    return now - last_frame > timeout


def stall_escalated(count: int) -> bool:
    """Watchdog reconnects reached the escalation count (e26s05)."""
    return count >= LEAP_STALL_ESCALATION_COUNT


def stall_retry_wait(count: int) -> float:
    """Backoff after a stall-forced reconnect: fast, then slow (e26s05).

    A wedged service cannot be revived by hot-looping it — after the escalation
    count the app waits LEAP_STALL_ESCALATED_RETRY between attempts and logs
    the actionable remedy (restart the service / replug the device).
    """
    return LEAP_STALL_ESCALATED_RETRY if stall_escalated(count) else LEAP_STALL_RETRY
