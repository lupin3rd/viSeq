# specs/epics/e10-thumbnail-pipeline-v2/e10s09-spectrum-analysis-rework.md

# e10s09 — Perceptual spectrum analysis: log bars, level-independent AGC, peak-aware bands

**type:** feat
**risk:** P1
**context:** audio-analysis

**Context:** The user reported that the audio analysis bands did not respond
well to input audio. Investigation (synthetic signals through the real
pipeline) found three defects in the spectrum path:

1. **Linear bar binning** — the 16 bars split 0-22 kHz linearly, so musical
   energy (mostly < 7 kHz) piled into the first bars and the high bars stayed
   dead; the default band ranges (equal thirds of the linear bars → 0-7.3 /
   7.3-14.6 / 14.6-22 kHz) made bands 2/3 almost silent for music (measured:
   a music mix gave band1=0.31, band2=0.09, band3=0.00).
2. **Mean aggregation dilutes peaks** — a full-scale tone lit one bar, and the
   band value (mean over the band's bars) reached only ~0.19; the beat edge
   (`value >= 1.0`) was effectively unreachable (a wall of four full-scale
   tones reached only 0.59).
3. **Input-level dependence** — the 1.0 threshold meant 0 dBFS, so a quiet
   track could never drive the bands or the beat.

## Requirements

#### MODIFIED: Perceptual (log) bar mapping
`compute_spectrum_bars` bins the FFT over log-spaced frequency edges
(`SPECTRUM_F_MIN` 40 Hz → `SPECTRUM_F_MAX` 20 kHz) instead of a linear split.
Music energy spreads across the bars; the default equal-third band ranges now
mean ~40-320 Hz (bass) / 320 Hz-2.5 kHz (mids) / 2.5-20 kHz (highs).

#### ADDED: Level-independent AGC
`apply_spectrum_agc(bars, peak_hold)` normalizes each frame against a
fast-attack / slow-release spectral peak (`SPECTRUM_PEAK_TARGET` 0.9,
`SPECTRUM_PEAK_DECAY` 0.995/frame, `SPECTRUM_PEAK_FLOOR` 0.06 silence guard)
so loud and quiet input reach the same normalized response and the band/beat
thresholds are relative, not absolute. Wired into `spectrum_analyzer_loop`.

#### MODIFIED: Peak-aware band aggregation
`band_value_from_bars` gains an `agg` parameter (`mean` default — unchanged
contract; `peak`; `blend` = `BAND_AGG_WEIGHT`*peak + (1-weight)*mean).
`refresh_band_value` uses `blend`, so a band's value reflects its loudest bar;
the beat edge uses the named `BAND_BEAT_THRESHOLD = 0.6` (measured: kicks land
at 0.6-0.9 after AGC+blend; sustained content is ignored by the edge).

## Steps

1. RED: log-bar tests (5 kHz tone lands on its log bar; edges strictly
   increasing 40..20000) → verify: `.venv/bin/python -m pytest tests/ -q -k "spectrum or bar_freq"`
2. GREEN: `_bar_freq_edges` + log mapping in `compute_spectrum_bars` →
   verify: same command
3. RED: AGC tests — loud/quiet reach the same normalized peak, silence stays
   zero, the hold releases slowly → verify:
   `.venv/bin/python -m pytest tests/ -q -k "agc or band_value_agg or beat_threshold"`
4. GREEN: `apply_spectrum_agc` + wiring in `spectrum_analyzer_loop`; `agg`
   support in `band_value_from_bars`; `blend` + `BAND_BEAT_THRESHOLD` in
   `refresh_band_value` → verify: same command
5. Full-suite verification → verify:
   `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Verification Script (Step-by-Step)

1. Play a track with bass, mids and cymbals: all three bands and most bars
   respond (a music mix measured 0.84/0.63/0.50 vs 0.31/0.09/0.00 before).
2. Lower/raise the input level: the bars and bands track the content, not the
   volume (AGC).
3. Select Beat Band 1 as the beat source with a kick-driven track: the
   sequencer advances on every kick (10/10 on realistic synthetic kicks with
   random phase and broadband attack clicks).
4. Silence keeps the bars at zero (no noise amplification).

## Out of scope

- Changing the FFT size or frame rate (2048/30 fps stays).
- Adding more analysis bands (3 remains the UI contract).
- The BPM detector (essentia) path.

## Risks

- AGC normalizes relative to the recent peak: a suddenly much louder passage
  briefly dims the rest (by design, ~1.3 dB/s release bounds it).
- The beat threshold is tuned to measured kicks; sustained loud bass never
  re-triggers (edge semantics), which is the intended beat definition.
