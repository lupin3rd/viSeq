# specs/epics/e10-thumbnail-pipeline-v2/e10s08-sequencer-live-bpm-and-compact-sources.md

# e10s08 — Sequencer: stale BPM never drives the clock + compact single-row beat sources

**type:** feat
**risk:** P2
**context:** sequencer

**Context:** Two follow-ups to e10s07. (1) Switching from Manual BPM to BPM
Detection left the sequencer running at the manual tempo (e.g. 120) even
though no detection was active — `current_bpm` was a stale leftover. The
sequencer must advance only on a *live* tempo: a recent detection in Analysis
mode, or the entered value in Manual mode. (2) The beat-source UI took two
rows; it is collapsed into the transport row with abbreviated labels.

## Requirements

#### ADDED: Live-tempo gating for the timed clock
`bpm_last_detected` records every successful BPM detection; a timed mode
(Analysis/Manual) may advance only when `_timed_bpm_live()` is true — Manual
always (the entered value is real), Analysis only while beat tracking is on
and a detection arrived within `BPM_DETECTION_STALE_SECONDS` (2 s, 2x the
1 s analysis cadence). With no live tempo the sequencer idles (re-checks every
50 ms) instead of stepping on a stale BPM. The visual metronome follows the
same rule.

#### MODIFIED: Beat-source row is a single compact line
**Before:** transport row + a second aligned row (312 px spacer) holding
MIDI/Manual with full labels.
**After:** one row: PLAY/‹/RESYNC/› + `BPM Det` + Band 1 + `MIDI` + `Manual`
+ (hidden until manual) the BPM input, TAP and live readout. Labels are
abbreviated; `manual_bpm_text` is hidden outside manual mode so a stale value
never lingers.

## Steps

1. RED: `_timed_bpm_live()` tests — manual always live; analysis live only
   with tracking on and a detection within the stale window; a structural
   test that the analyzer stamps `bpm_last_detected` →
   verify: `.venv/bin/python -m pytest tests/ -q -k "timed_bpm or essentia_loop_marks"`
2. GREEN: `bpm_last_detected` + `_timed_bpm_live()` + gating in
   `sequencer_tick` and `visual_metronome_loop` → verify: same command
3. RED: UI tests — abbreviated labels; the beat sources live on one row (the
   alignment-spacer test is removed) → verify:
   `.venv/bin/python -m pytest tests/ -q -k "beat_source_labels or beat_source_ui_wired"`
4. GREEN: single-row layout + label abbreviations + `manual_bpm_text` show
   toggle in `midi_action_beat_source` → verify: same command
5. Full-suite verification (HIGH-2 fade tests now run in Manual mode — a live
   timed tempo) → verify:
   `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Verification Script (Step-by-Step)

1. Manual BPM 120 → PLAY: sequencer runs at 120.
2. Switch to BPM Det without enabling beat tracking (or with silent input):
   the sequencer stops advancing within ~2 s (no stale 120).
3. Enable beat tracking with audio: the sequencer resumes at the detected BPM.
4. The transport row holds all four sources; Manual shows the input + TAP;
   switching away hides them.

## Out of scope

- Changing the band/event-driven behavior (e10s07).
- The audio-window band configs.

## Risks

- The fresh-detection window must not stall a real session: 2 s covers the 1 s
  analysis cadence with margin; a silent input (no detection) intentionally
  stops the sequencer per the user's request.
