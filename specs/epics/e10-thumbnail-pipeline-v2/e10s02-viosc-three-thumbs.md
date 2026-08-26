# e10s02 — viOSC generates up to 3 thumbs per media at distinct jittered times

**type:** feat
**risk:** P2
**context:** thumbnail-pipeline

**Context:** The Mediagrid needs three frames per media so a video is identifiable
from its content (user goal). The daemon currently extracts a single frame at a
random time. This story raises the count to up to 3 at distinct jittered times
(~15/50/85 % of duration) with a fallback to fewer frames for images and very
short clips. The OSC contract already carries an index per thumb
(`/viosc/replythumb/<name>/<idx>` and the `"all"` request arg), so no contract
change is needed.

## Requirements

#### MODIFIED: Thumbnail generation count and timing
**Before:** extraction returns 1 frame at `random.uniform(0.1, 0.9) * duration`.
**After:** extraction returns up to 3 frames at distinct jittered targets
(~15/50/85 % of duration with small random jitter, avoiding collisions); media
with duration ≤ 0 (images) return a single frame; very short clips return as many
distinct frames as the duration allows (min 1).

#### MODIFIED: Generation worker stores the multi-frame list
**Before:** the worker stores a 1-element list.
**After:** the worker stores the extracted list; the reply path with `"all"`
already sends every index (contract unchanged), so viseq receives all frames in
one request/response round.

## Steps

1. RED: tests asserting (a) count=3 requests 3 distinct timestamps and returns 3
   frames, (b) duration ≤ 0 yields exactly 1 frame, (c) short clips yield min
   (distinct frames, 3) → verify: `cd ../viosc && .venv/bin/python -m pytest tests/ -q -k thumb`
2. GREEN: implement distinct-time frame picking — 3 subprocess calls OR a single
   ffmpeg invocation yielding 3 frames, whichever the e10s04 spike shows is
   cheaper; keep the pixel/blob caps per frame → verify: `cd ../viosc && .venv/bin/python -m pytest tests/ -q -k thumb`
3. Full-suite verification both repos → verify: `cd ../viosc && .venv/bin/python -m pytest tests/ -q && cd ../viseq && .venv/bin/python -m pytest tests/ -q`

## Verification Script (Step-by-Step)

1. Start the stack; load ≥2 videos and 1 image in Vimix.
2. In the viseq Mediagrid confirm each video tile cycles through 3 distinct
   frames (after e10s03/e10s04 land) and the image tile stays static.
3. Confirm the daemon log shows one "THUMB READY" line per media (not per frame).

## Out of scope

- The viseq multi-texture storage and cycling (e10s03, e10s04).
- More than 3 thumbs per media.
- Persisting thumbnails to disk.

## Risks

- 3 extractions per media triples daemon-side ffmpeg work at load; mitigated by
  the 3-way concurrency semaphore and background threads (no UI impact), and by
  the spike's choice of the cheapest extraction strategy.
- Random jitter could collide for very short clips; the distinct-time picker
  must guarantee uniqueness with a floor of 1 frame.
