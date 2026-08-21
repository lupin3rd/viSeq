# e04s02 — Three selectable bands (band1/band2/band3), individually enableable

**type:** feature
**risk:** P2
**context:** audio

**Context:** The single selectable band becomes three independent bands. Each band has its
own enable checkbox (disabled by default: not computed, overlay hidden, variable stays 0),
its own Start/End range sliders and a translucent rectangle on the spectrum. The enabled
bands' mean bar levels are displayed and continuously stored in the module variables
`band1`, `band2`, `band3` (0..1) for future features. The spectrum engine itself drops from
32 to 16 bars (benchmarked lighter: ~26% less compute per frame + half the draw calls; the
FFT dominates either way). All updates run in the main-thread queued task (HIGH-1).

## Requirements

#### ENHANCED: Three individually enableable band variables
**Before:** one band (`audio_band_value`, `band_start`/`band_end` sliders).
**After:** bands 1-3 with `band{1,2,3}_enabled` checkboxes (default off), per-band
`band{N}_start`/`band{N}_end` sliders, translucent `band{N}_rect` overlays, `band{N}_value_text`
displays, and the module variables `band1`/`band2`/`band3` (0..1) updated only while enabled.

## Steps

1. Constants/state: `NUM_BANDS = 3`, `SPECTRUM_BARS = 16`, per-band rect colors,
   `bands_enabled = {1: False, 2: False, 3: False}`, `band1/band2/band3 = 0.0`; pure
   `band_value_from_bars` unchanged.
   → verify: `.venv/bin/python -m pytest tests/ -q -k bands`
2. `refresh_bands(bars)` (enabled only), `on_band_enable`, `on_band_change(band_id)`;
   three band rows in the audio window (checkbox + Start/End + value), three hidden
   overlays; remove the single-band UI.
   → verify: `.venv/bin/python -m pytest tests/ -q`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: enable Band 1 → its rectangle and value appear and track the spectrum; the
   other bands stay "—" and cost nothing; `band1` is readable as a variable.
