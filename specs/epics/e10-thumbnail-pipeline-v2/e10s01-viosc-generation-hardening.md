# e10s01 — viOSC generation hardening: log failures, keep good cache, regenerate on demand

**type:** fix
**risk:** P1
**context:** thumbnail-pipeline

**Context:** Implements BUG-2026-08-26T230838. The daemon's thumbnail pipeline is
one-shot, memory-only and silent: a single failed extraction replaces the cached
thumbnail list with an empty one (or a `regen` wipes it before regenerating), the
reply path silently no-ops on an empty cache, and no log records why. A source is
then permanently stranded on "Loading thumbnail..." in viseq. This story makes
every failure visible and recoverable without touching the frozen OSC contract.

## Requirements

#### ADDED: Reason-specific thumbnail failure logging
The generation worker logs a distinct error line for each failure class: media
file missing, ffprobe/ffmpeg non-zero exit, and empty extraction output. The
existing success print is preserved.

#### MODIFIED: Failed regeneration never replaces a good cache
**Before:** the worker unconditionally assigns the extraction result to the cache,
so a failed run stores `[]` and destroys the previous thumbnail.
**After:** the cache is replaced only when extraction yields ≥1 frame; on failure
the previous cached thumbnails are kept and the error is logged.

#### ADDED: On-demand regeneration on request with empty cache
When an on-demand thumbnail request (`/viosc/thumb/<id>`) arrives for a source
whose cache is empty but whose URI is valid, the daemon regenerates (bounded by
the existing 3-way concurrency semaphore) and replies with the fresh frame(s)
instead of silently returning. The silent no-op remains only for genuinely
unloadable sources (no URI, or file still missing after the retry).

## Steps

1. viosc repo: create `.venv` (python3 -m venv), install `-r requirements.txt` +
   pytest; add `tests/test_thumbnails.py` importing `viosc` (module import needs
   python-osc only; subprocess/os.path are monkeypatched) → verify: `cd ../viosc && test -f .venv/bin/python && .venv/bin/python -m pytest --version`
2. RED: tests asserting the worker logs a reason-specific error for (a) missing
   file, (b) ffmpeg non-zero exit, (c) empty output → verify: `cd ../viosc && .venv/bin/python -m pytest tests/ -q -k thumb`
3. GREEN: add the three error branches with distinct messages → verify: same command
4. RED: test asserting a FAILED regeneration leaves the previously cached
   thumbnail untouched → verify: `cd ../viosc && .venv/bin/python -m pytest tests/ -q -k thumb`
5. GREEN: assign the new result to the cache only when non-empty → verify: same command
6. RED: test asserting an on-demand request on an empty cache + valid URI triggers
   regeneration (semaphore-bounded) and replies with the fresh frame → verify: `cd ../viosc && .venv/bin/python -m pytest tests/ -q -k thumb`
7. GREEN: reply path regenerates then sends when cache empty + URI valid; keep
   silent no-op for unloadable sources → verify: `cd ../viosc && .venv/bin/python -m pytest tests/ -q && cd ../viseq && .venv/bin/python -m pytest tests/ -q`

## Verification Script (Step-by-Step)

1. Start viosc, then vimix, then viseq.
2. In the viseq Mediagrid confirm every source with valid media gets a thumbnail
   (the bug's 2 sources included).
3. Kill the media file temporarily and watch the daemon log for the
   reason-specific error line (no silent failure).
4. Restore the file and confirm the next request cycle (≤3 s) recovers the thumb
   without a manual regen.

## Out of scope

- The 3-thumbs-per-media change (e10s02) — this story only hardens the existing
  single-thumb path.
- viseq UI changes beyond what e10s01 enables (failed-state UX is e10s04).
- Any change to the OSC contract or to Vimix.

## Risks

- A synchronous regeneration inside the reply path could block the daemon's OSC
  handler for ~1-2 s; mitigated by the 3-way semaphore and the fact that viseq
  tolerates a slow reply (it re-requests every 3 s).
- The daemon has no test suite today; the new harness is minimal (monkeypatched
  subprocess/os.path) and must not require a real media file.
