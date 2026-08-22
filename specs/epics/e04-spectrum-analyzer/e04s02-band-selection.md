# e04s02 — Three selectable bands (band1/band2/band3), individually enableable

**type:** feature
**risk:** P2
**context:** audio

**Context:** The single selectable band becomes three independent bands. Each band has its
own enable checkbox (disabled by default: not computed, overlay hidden, variable stays 0),
its own **horizontal** range (Start/End sliders, 0..1 of the spectrum) and its own
**vertical** level window (Min/Max sliders, 0..1 of the bar height). The band value is the
mean "fill" of the selection rectangle: each bar inside the horizontal range is mapped so
0 = at/below Min and 1 = at/above Max, then averaged. Enabled bands' values are displayed
and stored in the module variables `band1`, `band2`, `band3` (0..1) for future features.
The spectrum bars are taller (drawlist 330x80) and the spectrum engine stays at 16 bars
(benchmarked lighter). After the automatic OSC connect at boot, the app sends
`/vimix/current/sync` so Vimix re-emits its current source state. All updates run in the
main-thread queued task (HIGH-1).

## Requirements

#### ENHANCED: Three individually enableable bands with a 2D selection rectangle
**Before:** one band (`audio_band_value`, `band_start`/`band_end` sliders).
**After:** bands 1-3 with `band{1,2,3}_enabled` checkboxes (default off), per-band
`band{N}_start`/`band{N}_end` (frequency) and `band{N}_min`/`band{N}_max` (level) sliders,
`band{N}_rect` 2D overlays, `band{N}_value_text` displays, and the module variables
`band1`/`band2`/`band3` (0..1) updated only while enabled.

#### ENHANCED: Vimix current-source sync after boot autoconnect
**Before:** no sync message after the automatic connection.
**After:** `autostart_osc` sends `/vimix/current/sync` (payload `[]`) via the viOSC client
once the automatic client connection succeeds.

## Steps

1. Constants/state: `NUM_BANDS = 3`, `SPECTRUM_BARS = 16`, `SPEC_DRAWLIST_H = 80`, per-band
   rect colors, `bands_enabled = {1: False, 2: False, 3: False}`, `band1/band2/band3 = 0.0`;
   `band_value_from_bars(bars, start, end, min_level=0.0, max_level=1.0)` gains the level
   window (defaults preserve the old behavior); `VIMIX_CURRENT_SYNC` constant.
   → verify: `.venv/bin/python -m pytest tests/ -q -k bands`
2. `refresh_band_value` reads the level sliders and draws the 2D rectangle; band rows get
   the Min/Max sliders; taller drawlist; `autostart_osc` sends the sync after connect.
   → verify: `.venv/bin/python -m pytest tests/ -q`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: enable Band 1 → its rectangle and value appear and track the spectrum; moving
   Min/Max changes when the value saturates; `band1` is readable as a variable; after boot,
   the OSC log shows `/vimix/current/sync` and Vimix reacts to it.
