# e01s03 — OSC + state sync + media grid + thumbnails + monitor players on 2.x

**type:** refactor
**risk:** P1
**context:** domain

**Context:** The viOSC integration is the app's data backbone: incoming replies
(`/viosc/replydata` JSON, `/viosc/replythumb/<name>/<idx>` blobs) drive the raw-property table,
the media grid (tiles + thumbnails), and monitor players; outgoing requests (thumbnails,
regen, monitor commands) use the verified frozen contract. This story re-verifies all of it on
2.x through the headless harness, and folds in audit item L-1 (prune stale thumbnails/
request timestamps when sources disappear).

## Requirements

#### MODIFIED: viOSC integration runs on dpg 2.x
**Before:** OSC handling and media UI built with dpg 1.x on Python <= 3.11.
**After:** identical behavior on dpg 2.3.1 with the same frozen OSC contract; input caps and
payload validation (audit MED-6/MED-4) intact.

#### ADDED: Stale-state pruning (L-1)
When a source disappears from the vimix state, its cached texture (`thumbnails_data`),
`request_timestamps` entry, and registry texture are removed so state and memory do not grow
across source churn.

## Steps

1. Re-verify `incoming_osc_handler` caps and `update_vimix_sources_ui` payload validation / defensive sorting on 2.x via the harness → verify: `python3 tests/test_fixes.py 2>&1 | grep -c "MED-4\|MED-6" | grep -qE "^(8|9|1[0-2])$"` (all MED-4/MED-6 checks pass)
2. Implement L-1: when building the new sources dict, drop `thumbnails_data`/`request_timestamps` entries whose target_id is no longer present; delete orphaned `tex_<id>` textures from the registry → verify: `python3 -c "import re; src=open('viseq.py').read(); assert 'thumbnails_data' in src.split('def update_vimix_sources_ui')[1].split('def thumbnail_decoder_worker')[0], 'prune logic missing from state path'; print('OK')"`
3. Extend the harness with an L-1 regression check (source removed -> texture entry pruned) → verify: `python3 tests/test_fixes.py 2>&1 | grep -c "L-1" | grep -q 1`
4. Re-verify the thumbnail pipeline (decoder pixel cap, texture upload, tile rebuild) and monitor players on 2.x → verify: `python3 tests/test_fixes.py`

## Verification Script (Step-by-Step)

1. `python3 tests/test_fixes.py` — MED-4/MED-6/L-1 checks all pass.
2. Static check (step 2 command) exits 0 — prune logic present in the state path.
3. At user acceptance (e01s05): with viOSC running, remove a source in Vimix — its tile/thumbnail disappears from the grid and memory doesn't grow across repeated add/remove cycles.

## Out of scope

- New OSC endpoints or payload fields (frozen contract).
- Changes to viOSC or Vimix.

## Risks

- Deleting textures that are still referenced by live image items would render garbage — prune only after the grid rebuild, and guard with `does_item_exist` (existing pattern).
- `thumbnails_data` is read by multiple threads (main loop, monitor UI) — keep the prune on the main thread only.
