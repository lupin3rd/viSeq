# e05s01 — Beat source selector (6 modes) + sequencer dispatch; remove boot sync

**type:** feature
**risk:** P1
**context:** sequencer

**Context:** A combo right of the RESYNC button lets the user choose the timing source. The
default stays the BPM analysis. The sequencer tick loop branches: interval-driven modes
(analysis, manual) sleep 60/bpm as today; event-driven modes (band1/2/3, MIDI) wait on a
`sync_event_beat` instead. The boot-time `/vimix/current/sync` send is removed.

## Requirements

#### ENHANCED: Beat source selector with 6 modes
**Before:** sequencer always uses the analyzed BPM; boot sends `/vimix/current/sync`.
**After:** a combo (Rilevazione BPM / Battito Band 1..3 / MIDI Sync / BPM Manuale) right of
RESYNC; the sequencer advances on the fixed interval for analysis/manual, or on the beat
event for band/MIDI modes; nothing is sent at boot.

## Steps

1. Constants/state (`beat_source`, `sync_event_beat`, labels) + `beat_is_event_driven()`
   helper + sequencer_tick dispatch branch; remove the boot sync send and constant.
   → verify: `.venv/bin/python -m pytest tests/ -q -k beat`
2. Combo UI right of RESYNC + `on_beat_source` callback (shows the manual widgets only in
   manual mode); existing HIGH-2 tests still green (default = analysis).
   → verify: `.venv/bin/python -m pytest tests/ -q`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: the combo appears right of RESYNC; default "Rilevazione BPM" behaves as before;
   switching to "BPM Manuale" shows the number + TAP; boot log has no `/vimix/current/sync`.
