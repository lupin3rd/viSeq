# e10s04 — Mediagrid thumb cycling (750 ms, configure_item, gated) + failed-state retry UX + measured spike

**type:** feat
**risk:** P1
**context:** thumbnail-pipeline

**Context:** The user wants the Mediagrid tile to cycle through the source's
frames so a video is identifiable from three frames. A previous 3-thumb animation
was slow; this implementation switches the tile image's texture reference between
pre-loaded static textures via `dpg.configure_item(img_tag, texture_tag=...)` on a
750 ms wall-clock timer — no per-cycle decode, no texture create/delete churn, no
repeated OSC requests. A measured spike validates the cost before the feature is
committed, and the same story adds the failed-state UX (a tile that has gone
unanswered for N request cycles shows "thumbnail failed" with a right-click retry
instead of "Loading thumbnail..." forever).

## Requirements

#### ADDED: Spike — measured cycling cost (SPIKE-perf pattern)
A spike report (`specs/archive/spikes/SPIKE-thumb-cycle.md`) measures the
frame-time delta of cycling 20-50 sources × 3 textures at 750 ms and confirms the
idle render throttle (30 fps) renders the cycle smoothly; it also picks the
cheapest extraction strategy for e10s02 (3 ffmpeg calls vs 1 multi-frame call).

#### ADDED: Gated cycling engine
For each Mediagrid tile whose source has ≥2 stored textures, the tile image's
`texture_tag` advances through the per-source list on a 750 ms cadence while the
Mediagrid window is visible; sources with 0-1 textures stay static. The cycle
state is pure/testable (last-switch time + frame index per source).

#### ADDED: Failed-thumbnail state with retry
After N consecutive unanswered thumb requests (N default 5 cycles ≈ 15 s), the
tile switches from "Loading thumbnail..." to a "thumbnail failed" state with a
right-click → "Regenerate Thumbnail (Random)" retry; a successful reply resets the
counter and clears the state. The retry sends `/viosc/regen_thumb/<id>` once
(contract unchanged).

#### MODIFIED: frame_sleep() cadence interplay
**Before:** full render rate while sequencer/spectrum/monitor-video animate, 30 fps
idle otherwise; the grid was never an animation source.
**After:** if the spike shows the 30 fps idle rate renders the 750 ms cycle
smoothly, no change (default); otherwise grid cycling joins the animated
conditions that drive the full rate — decided by measurement, with a
`frame_sleep` regression test either way.

## Steps

1. Spike: instrument the main loop (or a micro-benchmark) cycling 20-50 sources ×
   3 textures at 750 ms; record frame-time delta + render smoothness at 30 fps;
   write the report → verify: `test -f specs/archive/spikes/SPIKE-thumb-cycle.md`
2. RED: pure cycle-state helper tests — frame index advances on the 750 ms
   wall-clock, wraps, and only advances while the grid is visible →
   verify: `.venv/bin/python -m pytest tests/ -q -k cycle`
3. GREEN: cycle helper + main-loop scheduling (configure_item texture_tag switch
   for visible sources with ≥2 textures) → verify: `.venv/bin/python -m pytest tests/ -q -k cycle`
4. RED: failed-state tests — after N unanswered requests the tile shows the failed
   state with right-click retry; a later reply clears it →
   verify: `.venv/bin/python -m pytest tests/ -q -k failed`
5. GREEN: unanswered-count bookkeeping in the request loop; state flip at the
   threshold; reset on reply; retry re-requests + one regen →
   verify: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`
6. frame_sleep decision from the spike + regression test →
   verify: `.venv/bin/python -m pytest tests/ -q -k frame_sleep`

## Verification Script (Step-by-Step)

1. Start the stack with 2-3 videos + 1 image loaded (e10s02 live).
2. Watch the Mediagrid: video tiles cycle 3 frames at ~750 ms; the image tile is
   static; the sequencer clip slot and monitor player keep index 0 (no cycling
   there — out of scope).
3. Confirm no measurable frame-rate drop with the grid open (spike numbers).
4. Break a media file, wait ~15 s: the tile shows "thumbnail failed"; right-click
   → regenerate works once the file is back.

## Out of scope

- Cycling in the sequencer clip slots or monitor players (grid only; the shared
  per-source list makes it trivial later).
- Persisting thumbnails to disk.
- Any OSC contract change (regen address already exists).

## Risks

- The previous 3-thumb animation was slow; the spike is the gate — if cycling at
  30 fps is visibly choppy or measurably costly, the frame_sleep condition (step
  6) is the designed mitigation.
- The failed-state threshold must not false-positive for genuinely slow loads
  (huge files on a busy disk); N=5 cycles with a reset-on-any-reply bounds this.
