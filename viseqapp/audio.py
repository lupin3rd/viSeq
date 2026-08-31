"""Audio analysis for viseq (REFACTOR_LATEST.md commit 8/13).

The ring-buffer callback, the spectrum/band math and the essentia
extractors. WORKER-SAFE: no dpg import (HIGH-1) — UI redraws are queued
by the thin loops in the composition root via ui_task.
"""

from typing import Any

import essentia.standard as es
import numpy as np

from viseqapp import state
from viseqapp.constants import (
    BAND_AGG_WEIGHT,
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


def _set_band_variable(band_id: int, value: float) -> None:
    """Store a band level into its module variable (band1/band2/band3)."""
    if band_id == 1:
        state.band1 = value
    elif band_id == 2:
        state.band2 = value
    else:
        state.band3 = value
