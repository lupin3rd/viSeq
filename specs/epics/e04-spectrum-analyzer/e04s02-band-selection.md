# e04s02 — Band selection: Start/End sliders + rectangle overlay + audio_band_value variable

**type:** feature
**risk:** P2
**context:** audio

**Context:** The user wants to select a part of the spectrum and use its level (0..1) as a
program variable. Two Start/End drag sliders (0..1 of the spectrum) define the band; a
translucent rectangle overlays the selection on the drawlist; the band's mean bar level is
computed by the pure `band_value_from_bars(bars, start, end)` helper, shown as text and
continuously stored in the module variable `audio_band_value` for future features. The whole
update runs in the main-thread queued task (HIGH-1); `audio_band_value` is written on the
main thread and read by future main-thread features.

## Requirements

#### ENHANCED: Selectable band value (0..1) as a program variable
**Before:** no band selection; no value variable.
**After:** Start/End sliders (0..1), a translucent selection rectangle on the spectrum, a
"Band value: X.XX" text, and the module variable `audio_band_value` updated continuously.

## Steps

1. Pure `band_value_from_bars(bars, start, end) -> float` (mean of selected bars, clamped;
   degenerate/inverted ranges yield at least one bar) + unit tests (full range, partial,
   inverted, empty).
   → verify: `.venv/bin/python -m pytest tests/ -q -k band_value`
2. Sliders + overlay rectangle + value text + `audio_band_value` wiring inside the queued
   spectrum task (`update_spectrum_ui`), plus an `on_band_change` callback to refresh
   immediately when the sliders move.
   → verify: `.venv/bin/python -m pytest tests/ -q`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: with the spectrum live, drag Start/End — the rectangle and the band value move;
   `audio_band_value` tracks the selected band level 0..1; a pure tone in the band yields ~1.
