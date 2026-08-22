# e05s03 — MIDI clock sync listener (mido/rtmidi, 24 pulses per beat)

**type:** feature
**risk:** P1
**context:** midi

**Context:** In "MIDI Sync" mode the sequencer follows standard MIDI clock: 24 pulses (0xF8)
per quarter note. A worker opens the first available MIDI input (mido + python-rtmidi;
verified: the Midi Through port opens and loopback clock messages are received), counts
pulses, and fires the sequencer beat event once per 24 pulses while playing. No port or a
backend failure degrades gracefully: it logs once and idles.

## Requirements

#### ENHANCED: MIDI clock drives the sequencer
**Before:** no MIDI support.
**After:** with "MIDI Sync" selected and a MIDI clock source running (Traktor etc.), each
step advances on the 24th clock pulse; the log shows the opened port.

## Steps

1. Dependencies: add `mido` + `python-rtmidi` to requirements.txt; pure
   `midi_beats_from_pulses(pulses)` counter helper (24/beat).
   → verify: `.venv/bin/python -m pytest tests/ -q -k midi`
2. `midi_clock_loop` worker: open the first input port, count 'clock' messages, set
   `sync_event_beat` per beat in MIDI mode; graceful idle when no port/backend.
   → verify: `.venv/bin/python -m pytest tests/ -q`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: select "MIDI Sync"; with a MIDI clock source (or `mido` loopback test script)
   the sequencer steps advance per beat and the log shows the port name; without any clock,
   it stays idle without errors.
