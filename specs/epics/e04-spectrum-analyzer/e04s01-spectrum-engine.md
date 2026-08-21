# e04s01 — Spectrum engine: FFT worker + 16 normalized bars in a drawlist

**type:** feature
**risk:** P1
**context:** audio

**Context:** When level analysis is on, the audio window should show a live vertical-bar
spectrum. A dedicated worker thread computes the FFT on the ring-buffer snapshot at ~30 FPS
and enqueues a main-thread redraw of 16 bars (drawlist rectangles). The math is pure and
unit-testable: `compute_spectrum_bars(samples) -> np.ndarray` (Hann window, rfft, dB scale
with -60 dBFS floor, 16 equal bins) and the bars are drawn via `ui_task` (HIGH-1).

## Requirements

#### ENHANCED: Live spectrum bars when level analysis is on
**Before:** only a VU progress bar.
**After:** a 330x40 drawlist with 16 vertical bars (green), each 0..1 level from the FFT;
the bars update ~30x/s while `is_audio_analyzing`, and the VU progress bar is removed
(its enqueue/set_value paths stay guarded).

## Steps

1. Pure `compute_spectrum_bars(samples) -> np.ndarray` (16 bins, 0..1, dB floor) + unit
   tests (pure tone peaks in the right bin; silence -> ~0; boundaries).
   → verify: `.venv/bin/python -m pytest tests/ -q -k spectrum`
2. `spectrum_analyzer_loop` worker (30 FPS, reads `get_audio_snapshot`, enqueues the bar
   redraw via `ui_task`) + the drawlist with tagged bars + `update_spectrum_bars(bars)`
   redraw function; replace the VU progress bar in the audio window.
   → verify: `.venv/bin/python -m pytest tests/ -q`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: select the audio source, enable level analysis → 32 green bars react to the
   music; silence → bars near zero.
