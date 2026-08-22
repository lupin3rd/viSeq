# e05s01 — Beat source selector (6 checkboxes + LEDs on two lines); remove boot sync

**type:** feature
**risk:** P1
**context:** sequencer

**Context:** Six checkboxes (radio-style, exactly one active) on two lines right of the
RESYNC control, each with its own beat LED: Rilevazione BPM (with the BPM readout), Band
1/2/3, MIDI Sync, BPM Manuale. The BPM LED is removed from the audio analyzer window; the
beat decision happens on the sequencer. The sequencer tick loop branches: interval-driven
modes (analysis, manual) sleep 60/bpm; event-driven modes (band1/2/3, MIDI) wait on
`sync_event_beat`. The boot-time `/vimix/current/sync` send is removed.

## Requirements

#### ENHANCED: Beat source selector with 6 modes, checkbox + LED
**Before:** a combo; the BPM LED lived in the audio window; boot sent `/vimix/current/sync`.
**After:** two lines of radio-style checkboxes with per-mode LEDs on the sequencer; the BPM
readout sits next to Rilevazione BPM; nothing is sent at boot.

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
