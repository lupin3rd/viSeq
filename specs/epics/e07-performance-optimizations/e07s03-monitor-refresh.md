# e07s03 — P2: Monitor refresh — skip configure when arm static and values unchanged

**type:** refactor
**risk:** P2
**context:** perf/monitor

**Context:** `refresh_monitor_display` runs on the main thread every frame for every monitor
player and re-configures the disc arm, speed text, alpha bar and seek bar unconditionally —
4+ `configure_item` per player per frame, even when the video is paused and the values are
static (SPIKE-perf B: 24 dpg calls/frame @6 players). Cache the last written values per
player (dict fields) and only configure what actually changed; the arm only advances while
the video plays, so skip it otherwise.

## Requirements

#### MODIFIED: Monitor refresh configures only changed widgets
**Before:** every frame re-configured the disc arm, speed text, alpha fill and seek fill for
every player, regardless of change.
**After:** the arm is configured only while the video plays (it moves only then); speed,
alpha and seek are configured only when their value differs from the player's cached
`last_speed`/`last_alpha`/`last_seek` (missing cache = first refresh = configure).

## Steps

1. Add the per-player caches (`last_speed`, `last_alpha`, `last_seek`) and the change guards
   in `refresh_monitor_display`; arm configure gated on the playing state.
   → verify: `.venv/bin/python -m pytest tests/ -q -k monitor_refresh`
2. Regression tests: unchanged props → no configure_item on the second refresh; a seek
   change → only the seek fill is configured; a playing video → the arm is configured.
   → verify: `.venv/bin/python -m pytest tests/ -q -k monitor_refresh`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: monitor players still spin while the video plays and the bars track
   alpha/seek/speed live; with the video paused, the readouts freeze as before.

## Out of scope

- Changing the monitor's look or the props protocol.
- Throttling refresh to a fixed lower fps (the skip-unchanged guard already removes the
  steady-state cost; the remaining per-frame cost is only for actually-moving values).

## Risks

- A missed cache update would freeze a readout — the caches are written at the same place
  as the configure, and the first refresh always configures (None cache).
