"""Audio analysis for viseq (REFACTOR_LATEST.md commit 8/13).

The ring-buffer callback, the spectrum/band math (including the pure band-window
geometry of the mouse-editable rectangles, e50s01) and the essentia extractors.
WORKER-SAFE: no dpg import (HIGH-1) — UI redraws are queued by the thin loops in
the composition root via ui_task.
"""

import math
from typing import Any

import essentia.standard as es
import numpy as np

from viseqapp import state
from viseqapp.constants import (
    BAND_AGG_WEIGHT,
    BAND_EDGE_BODY,
    BAND_EDGE_BOTTOM,
    BAND_EDGE_LEFT,
    BAND_EDGE_RIGHT,
    BAND_EDGE_TOP,
    BAND_RECT_HIT_TOLERANCE_PX,
    BAND_RECT_MIN_SPAN,
    SPECTRUM_BARS,
    SPECTRUM_DB_FLOOR,
    SPECTRUM_F_MAX,
    SPECTRUM_F_MIN,
    SPECTRUM_FFT_SIZE,
    SPECTRUM_PEAK_DECAY,
    SPECTRUM_PEAK_FLOOR,
    SPECTRUM_PEAK_TARGET,
)

rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
lowpass_filter = es.LowPass(cutoffFrequency=250.0)

rhythm_extractor = es.RhythmExtractor2013(method="multifeature")


lowpass_filter = es.LowPass(cutoffFrequency=250.0)


def audio_callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
    if status:
        print(status)
    samples = indata[:, 0].astype(np.float32)
    # L-2 ring-buffer write: in-place, modulo indexing, no full-buffer reallocation
    # (np.roll allocated a fresh ~1 MB array ~43x/s on every callback).
    n = len(samples)
    if n >= len(state.audio_buffer):  # defensive: block larger than the buffer
        state.audio_buffer[:] = samples[-len(state.audio_buffer) :]
        state.audio_buffer_head = 0
    else:
        end = state.audio_buffer_head + n
        if end <= len(state.audio_buffer):
            state.audio_buffer[state.audio_buffer_head : end] = samples
        else:
            split = len(state.audio_buffer) - state.audio_buffer_head
            state.audio_buffer[state.audio_buffer_head :] = samples[:split]
            state.audio_buffer[: n - split] = samples[split:]
        state.audio_buffer_head = end % len(state.audio_buffer)


def get_audio_snapshot() -> np.ndarray:
    """Chronological copy of the last len(audio_buffer) samples (newest at tail).

    Linearizes the ring buffer for the BPM thread. Called once per second (not per
    audio callback), so this allocation is acceptable.
    """
    head = state.audio_buffer_head
    if head == 0:
        return state.audio_buffer.copy()
    return np.concatenate((state.audio_buffer[head:], state.audio_buffer[:head]))


def _bar_freq_edges(n_bars: int, sr: float) -> np.ndarray:
    """Log-spaced frequency edges for n_bars perceptual bars (Hz, e10s09).

    Equal log steps spread musical energy across the bars — linear binning
    piled almost everything into the low bars and left the high ones dead.
    """
    ratio = (SPECTRUM_F_MAX / SPECTRUM_F_MIN) ** (1.0 / n_bars)
    edges = SPECTRUM_F_MIN * ratio ** np.arange(n_bars + 1)
    edges[-1] = SPECTRUM_F_MAX  # snap: the pow chain drifts by float epsilon
    return edges


def compute_spectrum_bars(
    samples: np.ndarray, n_bars: int = SPECTRUM_BARS, sr: float = state.samplerate
) -> np.ndarray:
    """Magnitude spectrum of the latest samples, binned into n_bars levels (0..1).

    Hann-windowed rfft, dB scale with a -SPECTRUM_DB_FLOOR floor; the bars are
    log-spaced over SPECTRUM_F_MIN..SPECTRUM_F_MAX (e10s09) so music energy is
    spread perceptually; a full-scale sine reaches ~1.0, silence ~0.0.
    """
    if samples.size < SPECTRUM_FFT_SIZE:
        samples = np.pad(samples, (0, SPECTRUM_FFT_SIZE - samples.size))
    frame = samples[-SPECTRUM_FFT_SIZE:] * np.hanning(SPECTRUM_FFT_SIZE)
    mag = np.abs(np.fft.rfft(frame))[1:]  # drop DC; bin k = k*sr/FFT_SIZE
    bin_edges = np.floor(_bar_freq_edges(n_bars, sr) / (sr / SPECTRUM_FFT_SIZE)).astype(int)
    levels = np.zeros(n_bars, dtype=np.float32)
    for i in range(n_bars):
        lo = bin_edges[i]
        hi = min(bin_edges[i + 1], len(mag))
        if hi > lo:
            levels[i] = float(np.max(mag[lo:hi]))
    # max per bar: averaging in dB would drown a narrow peak among quiet bins
    db = 20.0 * np.log10(levels / (SPECTRUM_FFT_SIZE / 4.0) + 1e-12)
    return np.clip((db + SPECTRUM_DB_FLOOR) / SPECTRUM_DB_FLOOR, 0.0, 1.0).astype(np.float32)


def apply_spectrum_agc(bars: np.ndarray, peak_hold: float) -> tuple[np.ndarray, float]:
    """Normalize bars against a slow-decaying spectral peak (level-independent, e10s09).

    A loud transient raises the hold instantly; the hold decays each frame so the
    display and bands track the recent loudest content instead of requiring a
    fixed full-scale input. Silence (peak below the floor) keeps a flat gain.
    """
    current = float(np.max(bars)) if bars.size else 0.0
    peak_hold = current if current > peak_hold else max(current, peak_hold * SPECTRUM_PEAK_DECAY)
    gain = SPECTRUM_PEAK_TARGET / max(peak_hold, SPECTRUM_PEAK_FLOOR)
    return np.clip(bars * gain, 0.0, 1.0).astype(np.float32), peak_hold


def band_value_from_bars(
    bars: np.ndarray,
    start: float,
    end: float,
    min_level: float = 0.0,
    max_level: float = 1.0,
    agg: str = "mean",
) -> float:
    """Fill (0..1) of the selection rectangle over the bars.

    The horizontal window [start, end) picks the bars; the vertical window
    [min_level, max_level] maps each bar's level so 0 = at/below min and
    1 = at/above max. An inverted/empty level window falls back to the plain
    bar mean (backward compatible with the frequency-only usage).

    agg selects the aggregation over the mapped bars (e10s09): "mean" (default,
    steady fill), "peak" (loudest bar — transient detection) or "blend"
    (peak-dominant, used by the live band values and the beat edge).
    """
    if bars.size == 0:
        return 0.0
    n = bars.size
    lo = round(start * n)
    hi = round(end * n)
    if hi <= lo:  # inverted/degenerate horizontal selection -> at least one bar
        hi = lo + 1
    lo = max(0, min(lo, n - 1))
    hi = max(lo + 1, min(hi, n))
    selected = bars[lo:hi]
    if max_level <= min_level:
        return float(np.mean(selected))
    mapped = np.clip((selected - min_level) / (max_level - min_level), 0.0, 1.0)
    if agg == "peak":
        return float(np.max(mapped))
    if agg == "blend":
        return float(BAND_AGG_WEIGHT * np.max(mapped) + (1.0 - BAND_AGG_WEIGHT) * np.mean(mapped))
    return float(np.mean(mapped))


# --- e50s01: band-window geometry (pure, dpg-free) ---
# The band rectangle is the pixel image of (start, end, min_level, max_level):
# x = value * width, y = (1 - level) * height — the mapping the composition
# root's refresh_band_value draws with. These helpers own the pointer maths
# (edge selection, overlap tie-break, clamped drag); the UI seam stays thin.

BandWindow = tuple[float, float, float, float]  # (start, end, min_level, max_level)
BandRectPx = tuple[float, float, float, float]  # (x0, y0, x1, y1)


def band_rect_px(window: BandWindow, width: float, height: float) -> BandRectPx:
    """The pixel rectangle of a band window inside a drawlist (e50s01)."""
    start, end, min_level, max_level = window
    return (
        start * width,
        (1.0 - max_level) * height,
        end * width,
        (1.0 - min_level) * height,
    )


def _point_segment_distance(
    px: float, py: float, x0: float, y0: float, x1: float, y1: float
) -> float:
    """Euclidean distance from a point to a segment (pure; endpoints included)."""
    dx, dy = x1 - x0, y1 - y0
    span = dx * dx + dy * dy
    if span == 0.0:
        return math.hypot(px - x0, py - y0)
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / span))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def band_rect_local_pointer(
    client_pointer: tuple[float, float], rect_min: tuple[float, float]
) -> tuple[float, float]:
    """Drawlist-local pointer from the client mouse and the drawlist origin (e50s01).

    `get_mouse_pos(local=False)` is DPG's stable per-frame client mouse and
    `get_item_rect_min` reports the item in that same space, so any padding
    cancels in the subtraction. `get_drawing_mouse_pos()` was rejected: its
    origin is undocumented (the drawing API docs do not state it) and the
    composition root already documents the client space as the reliable one.
    """
    return (
        float(client_pointer[0]) - float(rect_min[0]),
        float(client_pointer[1]) - float(rect_min[1]),
    )


def band_rect_contains(rect: BandRectPx, point: tuple[float, float]) -> bool:
    """Is the point inside the rectangle (borders included)? (e50s02)

    Shared by the body hit-test and by the hover gate that replaces the item
    scoping DPG 2.x does not allow on mouse handlers (they are global-only).
    """
    x0, y0, x1, y1 = rect
    return x0 <= float(point[0]) <= x1 and y0 <= float(point[1]) <= y1


def band_rect_edge_segment(
    rect: BandRectPx, edge: str
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """The drawn line of one edge, for the grab highlight (e50s01).

    Returns the segment's two endpoints, or None for `BAND_EDGE_BODY` (a body
    drag moves no single edge, so there is no line to highlight).
    """
    x0, y0, x1, y1 = rect
    if edge == BAND_EDGE_LEFT:
        return ((x0, y0), (x0, y1))
    if edge == BAND_EDGE_RIGHT:
        return ((x1, y0), (x1, y1))
    if edge == BAND_EDGE_TOP:
        return ((x0, y0), (x1, y0))
    if edge == BAND_EDGE_BOTTOM:
        return ((x0, y1), (x1, y1))
    return None


def band_rect_hit_test(
    pointer: tuple[float, float],
    rects: list[tuple[int, BandRectPx]],
    tolerance: float = BAND_RECT_HIT_TOLERANCE_PX,
) -> tuple[int, str] | None:
    """Resolve what the pointer grabbed: (band_id, edge) or None (e50s01).

    `rects` is ordered in HIT PRIORITY (highest band id first): the nearest edge
    within `tolerance` wins and an equal distance keeps the FIRST entry, which is
    the user's tie-break — the higher band is the one drawn on top. A pointer
    farther than the tolerance from every edge resolves to None (the body move
    arrives with e50s02).
    """
    px, py = float(pointer[0]), float(pointer[1])
    best: tuple[float, int, str] | None = None
    for band_id, (x0, y0, x1, y1) in rects:
        for edge, segment in (
            (BAND_EDGE_LEFT, (x0, y0, x0, y1)),
            (BAND_EDGE_RIGHT, (x1, y0, x1, y1)),
            (BAND_EDGE_TOP, (x0, y0, x1, y0)),
            (BAND_EDGE_BOTTOM, (x0, y1, x1, y1)),
        ):
            distance = _point_segment_distance(px, py, *segment)
            if distance <= tolerance and (best is None or distance < best[0]):
                best = (distance, band_id, edge)
    if best is not None:
        return (best[1], best[2])
    # An edge always beats a body hit (a precise grab is deliberate); only with
    # no edge in reach does the first rectangle containing the pointer move as a
    # whole (e50s02).
    for band_id, (x0, y0, x1, y1) in rects:
        if band_rect_contains((x0, y0, x1, y1), (px, py)):
            return (band_id, BAND_EDGE_BODY)
    return None


def band_rect_drag(
    window: BandWindow,
    edge: str,
    delta_x: float,
    delta_y: float,
    width: float,
    height: float,
    min_span: float = BAND_RECT_MIN_SPAN,
) -> BandWindow:
    """Apply a pixel drag to a band window (e50s02: all four edges + the body).

    A drag returns the new (start, end, min_level, max_level). `left`/`right`
    move their frequency bound, `top`/`bottom` their level bound, each clamped to
    0..1 and to `min_span` away from its partner so the window can never invert.
    `body` shifts all four values by the same pixel delta with the shift clamped
    so the window keeps its span and stays inside 0..1. The level axis is
    inverted (y = (1 - level) * height), so a downward delta DECREASES a level. A
    zero-size drawlist is a no-op (defensive).
    """
    if width <= 0.0 or height <= 0.0:
        return window
    start, end, min_level, max_level = window
    if edge == BAND_EDGE_LEFT:
        start = max(0.0, min(start + delta_x / width, end - min_span))
    elif edge == BAND_EDGE_RIGHT:
        end = max(start + min_span, min(end + delta_x / width, 1.0))
    elif edge == BAND_EDGE_TOP:
        max_level = min(1.0, max(max_level - delta_y / height, min_level + min_span))
    elif edge == BAND_EDGE_BOTTOM:
        min_level = max(0.0, min(min_level - delta_y / height, max_level - min_span))
    elif edge == BAND_EDGE_BODY:
        shift_x = max(-start, min(delta_x / width, 1.0 - end))
        shift_level = max(-min_level, min(-delta_y / height, 1.0 - max_level))
        start, end = start + shift_x, end + shift_x
        min_level, max_level = min_level + shift_level, max_level + shift_level
    else:
        return window
    return (start, end, min_level, max_level)


def _set_band_variable(band_id: int, value: float) -> None:
    """Store a band level into its module variable (band1/band2/band3)."""
    if band_id == 1:
        state.band1 = value
    elif band_id == 2:
        state.band2 = value
    else:
        state.band3 = value
