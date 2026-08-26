---
bug_id: BUG-2026-08-26T230838
status: open
severity: medium
scope: thumbnail-pipeline
title: Thumbnails permanently lost for a source — viosc cache emptied silently, viseq shows "Loading thumbnail..." forever
---

# BUG-2026-08-26T230838: Thumbnail permanently lost — silent cache wipe + infinite "Loading..."

## Problem

- **What happens (actual):** On the real rig, 2 of 5 loaded media never show a
  thumbnail in the Mediagrid. The tiles stay on "Loading thumbnail..." forever while
  viseq re-requests every 3 seconds, unanswered. Right-click → "Regenerate Thumbnail
  (Random)" also produces nothing. The media files themselves are valid — ffmpeg
  extracts frames from them fine when run manually.
- **What should happen (expected):** Every source with a valid media file must get a
  thumbnail. A failed thumbnail generation must be visible (logged + surfaced in the
  UI) and must not permanently strand the source: a later request or regen must
  recover it.
- **How to reproduce:** Load several media in Vimix with viOSC up. Sources that
  suffer a transient thumbnail-generation failure (or a lost reply) show
  "Loading..." forever; the failure is silent on both sides and nothing retries.

`Security impact: NONE` — local GUI display and in-memory cache only; no I/O or data
exposure.

## Root Cause Analysis

Live diagnosis (memory forensics of the running viosc/viseq processes, OSC probes,
and ffmpeg reproduction) established:

1. At load, viOSC generated and sent thumbnails for **all 5** sources (full JPEG
   blobs found in the daemon's heap, sent to the UI on port 6667).
2. viseq displayed 3 of them. For the other 2 it kept requesting
   (`/viosc/thumb/<name>` every 3 s) with no reply.
3. A live probe batch at a later point confirmed viOSC now replies only for the 3
   sources that already display — **its in-memory thumbnail list for the other 2 had
   become empty**. A `regen_thumb` request also produced no reply. viseq's OSC server
   was verified alive and dispatching (direct probes logged as received), and the
   media files extract fine with the same ffmpeg command viOSC uses — so the loss is
   entirely on the viOSC side.
4. Reading viOSC's thumbnail worker: **every regeneration unconditionally replaces
   the cached thumbnail list with the new result, including an empty one**, and every
   failure path is silent (missing file, ffprobe/ffmpeg non-zero exit, empty output —
   no log line at all). The on-demand reply path silently returns when the cache is
   empty. Therefore **any single failed extraction or lost reply permanently strands
   the source**: nothing ever regenerates (generation only runs on a URI *change*),
   and the UI can never distinguish "still loading" from "failed".

The exact trigger that emptied the cache for the 2 sources (a transiently failed
regeneration vs. a lost first reply) could not be pinned after the processes exited;
the *mechanism* is verified: **one-shot, memory-only, silent-failure thumbnail
generation with no recovery path**.

- **Modules involved:** the viOSC daemon's thumbnail worker (generate / reply /
  regen paths) and the viseq Mediagrid request/display path.
- **Why it fails:** no error logging; unconditional cache overwrite with possibly
  empty results; silent no-reply on empty cache; no regeneration on demand; viseq has
  no failed-state UI and retries indefinitely.
- **Contributing factors:** 3-way concurrency semaphore in viOSC; random-frame seek
  (10–90 % of duration) can fail transiently; the UI request loop has no backoff or
  failure cap.
- **Risk level:** Medium — data loss is recoverable by restarting the stack, and no
  security surface is involved; but the failure is user-visible and permanent per
  session.

## TDD Fix Plan

Tests live in two repos: the root-cause fixes in `viosc` (new `tests/` with pytest,
monkeypatching `subprocess`/`os.path`), the resilience/UX fixes in `viseq`
(extend `tests/test_fixes.py` with the dpg stub harness).

1. **RED**: Write a test in the viosc repo asserting that when thumbnail extraction
   fails (mocked ffmpeg non-zero exit, missing file, or empty output) the worker
   logs an error line containing the reason (file missing / ffmpeg error / empty).
   **GREEN**: Add reason-specific error logging to the generation worker's failure
   paths.
   **verify**: `cd ../viosc && .venv/bin/python -m pytest tests/ -q` (or the
   equivalent venv in use there)

2. **RED**: Test asserting that a *failed* regeneration leaves the previously cached
   thumbnail untouched (the cache is only replaced when extraction yields ≥1 frame).
   **GREEN**: Only assign the new result to the cache when it is non-empty; keep the
   old cache otherwise.
   **verify**: same viosc pytest command

3. **RED**: Test asserting that an on-demand thumbnail request for a source with an
   empty cache but a valid URI triggers a regeneration and the reply is sent with
   the freshly extracted frame (not a silent no-op).
   **GREEN**: In the reply path, when the cache is empty and a URI exists, run
   generation (bounded by the existing concurrency semaphore) and then send the
   result; keep the silent no-op only for genuinely unloadable sources (no URI /
   file missing after retry).
   **verify**: same viosc pytest command

4. **RED**: viseq test asserting the tile flips to a "thumbnail failed" state (with
   right-click → retry) after N consecutive unanswered requests instead of showing
   "Loading..." forever, and that a subsequent successful reply clears it.
   **GREEN**: Track unanswered-request count per source in the request loop; flip the
   tile label/state at the threshold; reset on reply; the retry path re-requests and
   (optionally) sends `regen_thumb` once.
   **verify**: `.venv/bin/python -m pytest tests/ -q`

**REFACTOR**: Extract the request-throttle bookkeeping (timestamps + failure counts)
into a small helper so the upcoming multi-thumb epic (specs/epics/e10) reuses it
without churn.

## Acceptance Criteria

- [ ] A transiently failed generation no longer destroys the cached thumbnail.
- [ ] A source with an empty cache but a valid URI self-heals on the next request.
- [ ] viOSC logs every thumbnail failure with a reason.
- [ ] viseq shows a failed state (with retry) instead of "Loading..." forever.
- [ ] All new tests pass; existing tests still pass (both repos).
- [ ] The frozen OSC contract is unchanged (same addresses/payloads).

## Resolution

<!-- filled in by validate-fix -->
