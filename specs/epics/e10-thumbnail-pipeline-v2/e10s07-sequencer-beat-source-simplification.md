# specs/epics/e10-thumbnail-pipeline-v2/e10s07-sequencer-beat-source-simplification.md

# e10s07 — Beat sources: only Band 1 is selectable in the sequencer

**type:** feat
**risk:** P2
**context:** sequencer

**Context:** The user finds three selectable audio bands redundant for the
sequencer beat and wants the source list simplified: only **Beat Band 1**
remains selectable; Band 2 and Band 3 checkboxes are removed from the
sequencer. Bands 2/3 stay available in the audio/spectrum window (they are
analysis bands there, not beat sources). Also lands with BUG-2026-08-27T213000
(sequencer deadlock on beat-source switch) so the smaller, robust source list
switches cleanly while playing.

## Requirements

#### MODIFIED: Sequencer beat-source list
**Before:** BPM Detection, Beat Band 1, Beat Band 2, Beat Band 3, MIDI Sync,
Manual BPM (6 checkboxes on two rows).
**After:** BPM Detection, Beat Band 1, MIDI Sync, Manual BPM (4 checkboxes).
The Band 2 / Band 3 constants, labels, LED tags and their event-driven
membership are removed; `refresh_band_value` fires the sequencer beat only for
band 1 (bands 2/3 remain spectrum-only).

#### UNCHANGED: Spectrum band analysis
The audio window keeps its three configurable bands (2/3 remain usable for
analysis and their LED flash); only the beat-source mapping is removed.

## Steps

1. RED: update the beat-source tests — `test_beat_is_event_driven` and
   `test_beat_source_ui_wired` assert the new 4-entry list (no band2/band3
   checkboxes/LEDs); add a test that a band-2/3 rise never fires the
   sequencer beat → verify: `.venv/bin/python -m pytest tests/ -q -k "beat"`
2. GREEN: remove Band 2/3 from `BEAT_SOURCE_LABELS`, `BEAT_LED_TAGS`,
   `beat_is_event_driven` and the sequencer checkbox loops; band 1 is the
   only band that can drive the beat → verify: same command
3. Full-suite verification (including the BUG-2026-08-27T213000 deadlock
   regression) → verify:
   `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Verification Script (Step-by-Step)

1. Open the sequencer: the beat-source row shows BPM Detection, Band 1,
   MIDI Sync, Manual BPM — no Band 2 / Band 3.
2. Select Band 1 with audio input: the sequencer advances on band-1 peaks.
3. Switch sources while playing (Band 1 → BPM → Manual → MIDI): the sequencer
   follows the new mode within ~100 ms and never blocks (BUG fix).
4. The audio window still shows three bands; enabling band 2/3 flashes their
   LEDs on peaks but never drives the sequencer.

## Out of scope

- Removing bands 2/3 from the audio/spectrum window.
- Changing the OSC contract, MIDI bindings or the beat cadence.

## Risks

- A stale persisted MIDI binding that references `band2_beat` would set an
  unknown `beat_source`; the deadlock fix's flash guard degrades it to the
  BPM-timed fallback instead of killing the tick thread.
