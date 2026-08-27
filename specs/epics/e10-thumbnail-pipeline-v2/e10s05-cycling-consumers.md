# specs/epics/e10-thumbnail-pipeline-v2/e10s05-cycling-consumers.md

# e10s05 — Thumb cycling in the sequencer clip slots and monitor players

**type:** feat
**risk:** P2
**context:** thumbnail-pipeline

**Context:** e10s04 cycles the Mediagrid tile through the source's stored
frames (750 ms cadence, `configure_item` texture_tag switch, gated on window
visibility) and explicitly left the other consumers out of scope, noting "the
shared per-source list makes it trivial later". The user now wants the same
cycle where a media is actually applied: the sequencer clip slot and the
monitor player thumbnails. The per-source cycle state already exists; this
story tags the two remaining consumer images and applies the same texture_tag
switch to them, keeping the cadence, the wall-clock anchor, and the idle
render throttle unchanged.

## Requirements

#### ADDED: Sequencer slot thumb participates in the source cycle
The clip-slot image button (`update_track_slot_ui`) gets a stable tag
(`seq_thumb_<row>`) and `tick_thumb_cycle` switches its `texture_tag` to the
same frame index the Mediagrid shows for the assigned source. Slots with 0-1
stored frames stay static (unchanged rule).

#### ADDED: Monitor player thumb participates in the source cycle
The monitor thumbnail image (`update_monitor_player_ui`) gets a stable tag
(`mon_thumb_<player_id>`) and switches its `texture_tag` with the same index.
The turntable disc, speed/alpha/seek bars and the `frame_sleep` interplay are
untouched.

#### MODIFIED: Cycle visibility gate covers all three consumers
**Before:** cycling runs only while the Mediagrid window is visible.
**After:** cycling advances while ANY consumer window is visible — Mediagrid,
sequencer, or any monitor player window. All windows closed → cycle pauses and
the wall-clock anchor does not fast-forward (existing `advance_thumb_cycle`
anchor semantics).

#### UNCHANGED: frame_sleep decision
The SPIKE-thumb-cycle decision stands: the 750 ms cycle is ~1.6 us per switch
and renders smoothly at the 30 fps idle throttle, so consumer cycling must NOT
force the full render rate (regression test keeps `frame_sleep` green).

## Steps

1. RED: tests asserting the slot image button is created with a stable
   `seq_thumb_<row>` tag, the monitor image with a stable `mon_thumb_<id>`
   tag, and `tick_thumb_cycle` switches all three consumers (grid tile, slot,
   monitor) of one source to the same frame index on the cadence →
   verify: `.venv/bin/python -m pytest tests/ -q -k cycle`
2. GREEN: tag the slot/monitor images; extend `tick_thumb_cycle` to configure
   every consumer of the source with the cycled index → verify:
   `.venv/bin/python -m pytest tests/ -q -k cycle`
3. RED: gate test — cycling advances when the Mediagrid is hidden but the
   sequencer (or a monitor) window is visible; pauses when all are hidden →
   verify: `.venv/bin/python -m pytest tests/ -q -k cycle`
4. GREEN: `_thumb_cycle_active()` predicate over the three window sets →
   verify: `.venv/bin/python -m pytest tests/ -q -k cycle`
5. Full-suite verification: cycling must not force full render rate
   (frame_sleep regression), all prior thumb tests green →
   verify: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Verification Script (Step-by-Step)

1. Start the stack with ≥2 videos with 3 thumbs each (BUG-2026-08-27T201742
   fixed first — the grid, slot and monitor must show all frames).
2. Assign a video to a sequencer row: the slot thumb cycles at ~750 ms in sync
   with the Mediagrid tile.
3. Open a Monitor Player on the same source: its thumb cycles too, in sync.
4. Hide the Mediagrid only: cycling continues (sequencer visible). Hide every
   consumer window: cycling pauses; reopening resumes without fast-forward.
5. Confirm no frame-rate drop with slots + monitors + grid all cycling (the
   ~us-per-switch cost is unchanged).

## Out of scope

- Cycling cadence changes, per-consumer cadence, or per-consumer offsets.
- Any OSC contract change.
- More than 3 stored frames per source.

## Risks

- Rebuilding the slot/monitor bodies (assign change, monitor re-render) must
  keep the stable tags so the cycle never targets a stale item — guards with
  `dpg.does_item_exist` in the same style as the grid tile.
- The visibility gate now includes the always-open sequencer window, so
  cycling is effectively continuous in normal use; the measured cost
  (~1.6 us/switch at 750 ms cadence) makes this negligible, and the
  `frame_sleep` regression guards the render rate.
