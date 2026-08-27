---
bug_id: BUG-2026-08-27T213000
status: fixed
severity: high
scope: sequencer
title: Sequencer deadlocks after switching the beat source — the tick thread is stranded in an unbounded wait
---

# BUG-2026-08-27T213000: Sequencer blocks after switching the beat source

## Problem

- **What happens (actual):** While the sequencer is running, switching the
  beat source (e.g. from a band mode to BPM Detection / Manual / back) a few
  times eventually leaves the sequencer stuck: clicking PLAY does nothing —
  no steps advance, no playhead moves, and STOP/PLAY cannot recover it.
- **What should happen (expected):** A beat-source switch or STOP must always
  break through the sequencer's wait; the tick thread must never be stranded
  in a mode that no longer fires.
- **How to reproduce:** Start the sequencer in Beat Band 1 (audio input with a
  rising band). While it plays, switch to BPM Detection. The tick thread is
  already blocked in `sync_event_beat.wait()`; BPM mode never sets that event,
  so the thread waits forever — PLAY/STOP toggles the flag but the thread
  never returns to the top of its loop.

`Security impact: NONE` — local UI/threading only.

## Root Cause Analysis

`sequencer_tick()` uses an **unbounded** `sync_event_beat.wait()` in the
event-driven branch (band/MIDI modes). The wait is entered once and only
returns when a beat event fires. Switching `beat_source` while the thread is
inside that wait (the exact "switch a few times" flow the user hit) leaves no
producer for the event (BPM/Manual modes never set it) → the thread blocks
forever. STOP sets `is_playing=False` but the thread never re-reads it; PLAY
sets `sync_event_seq` but the thread is not in the seq wait. The only
recovery is restarting the app.

- **Modules involved:** `sequencer_tick()` (viseq.py) — the band/MIDI wait.
- **Why it fails:** the wait has no timeout, so the loop cannot re-evaluate
  `is_playing` or `beat_source`; the mode switch is invisible to the blocked
  thread.
- **Contributing factors:** a secondary fragility — `flash_led(BEAT_LED_TAGS[beat_source])`
  raises KeyError (killing the tick thread) if `beat_source` ever holds an
  unknown value (stale MIDI binding), producing the same "play does nothing"
  symptom.
- **Risk level:** High — a common interaction (switching sources while
  playing) permanently kills the sequencer for the session.

## TDD Fix Plan (viseq repo)

1. **RED**: structural test asserting the band/MIDI wait in `sequencer_tick`
   is polled with a timeout (regex on the source, same pattern as
   `test_sequencer_waits_once_per_step`) → verify:
   `.venv/bin/python -m pytest tests/ -q -k "wait_is_polled or waits_once"`
2. **GREEN**: replace the unbounded wait with a bounded poll
   (`sync_event_beat.wait(0.1)`, advance only when it returned True, otherwise
   re-loop so mode/stop are re-evaluated) → verify: same command
3. **RED**: test asserting a beat-source switch wakes the tick loop (a pending
   beat event is not required for the loop to notice the mode change — polled
   wait returns False and the loop continues) → verify:
   `.venv/bin/python -m pytest tests/ -q -k "beat_source"`
4. **GREEN**: guard `flash_led` against unknown `beat_source`
   (`BEAT_LED_TAGS.get(beat_source)`) so a stale binding can never kill the
   tick thread → verify:
   `.venv/bin/python -m pytest tests/ -q -k "beat_source"`
5. Full-suite verification → verify:
   `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Acceptance Criteria

- [ ] Switching the beat source while the sequencer runs never strands the
      tick thread; the new mode takes effect within ~100 ms.
- [ ] STOP always stops the sequencer within ~100 ms of the click.
- [ ] An unknown `beat_source` value degrades gracefully (flash skipped,
      tick thread alive) instead of killing the loop.
- [ ] Beat cadence is unchanged: a real beat event still advances immediately
      (the poll only bounds idle waits).
- [ ] All new tests pass; existing tests still pass.

## Resolution

Fixed on 2026-08-27 (TDD, all gates green: 203 tests, ruff, mypy).

- `sequencer_tick()` now polls the band/MIDI wait: `sync_event_beat.wait(0.1)`
  and `continue` on idle, so a beat-source switch or STOP is re-evaluated
  within ~100 ms while a real beat still advances immediately.
- `flash_led(BEAT_LED_TAGS.get(beat_source))` (also in
  `visual_metronome_loop`) degrades an unknown beat-source value instead of
  raising KeyError and killing the tick thread.
- Landing together with e10s07: bands 2/3 are no longer selectable beat
  sources, so the small remaining list switches cleanly.
