# e05s02 — Manual BPM (numeric + TAP) and band-1.0 rising-edge beat triggers

**type:** feature
**risk:** P2
**context:** sequencer

**Context:** Manual mode sets `current_bpm` from a numeric input and/or a TAP button
(interval-based, averaged over the last taps). Band modes fire the beat event on the
rising edge of the selected band reaching 1.0 (tracked in the main-thread spectrum task).

## Requirements

#### ENHANCED: Manual BPM + TAP
**Before:** no manual timing.
**After:** in "BPM Manuale" mode, a numeric input and a TAP button set `current_bpm`; the
sequencer uses the interval as usual.

#### ENHANCED: Band-1.0 beat triggers
**Before:** bands only produce values.
**After:** when the selected band mode is active and the band's value rises to >= 1.0
(prev < 1.0), the sequencer beat event fires; other bands do not trigger.

## Steps

1. `tap_bpm` (interval averaging) + `on_manual_bpm` + manual widgets wiring.
   → verify: `.venv/bin/python -m pytest tests/ -q -k tap`
2. Rising-edge detection in `refresh_band_value` (`band_prev_values`) that sets
   `sync_event_beat` only for the active band source.
   → verify: `.venv/bin/python -m pytest tests/ -q -k band_beat`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: "BPM Manuale" + TAP at ~120bpm → BPM text shows ~120 and the sequencer follows;
   a band mode with the level Max lowered fires steps when the band hits the top.
