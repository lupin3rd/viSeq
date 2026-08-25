# e07s01 — P0: Mediagrid per-push cost — signature-gated structural updates + per-cell value cache

**type:** refactor
**risk:** P1
**context:** perf/ui

**Context:** SPIKE-perf measured `update_vimix_sources_ui` at 0.12–2.6 ms per viOSC state push
with ~2 bind/configure + ~18 `set_value` per source (raw-table cells + tile readouts), all
unconditional. The full table rebuild is already signature-gated, but the per-source update
loop runs on every push. The structural fields it writes (tile theme, title, index badge)
depend only on `name`/`index`/`current_source`/columns — i.e. signature fields (with
`current_source` added). The value fields it writes (tile alpha, raw cells) depend on the
actual values. Split the loop accordingly: structural updates move inside the signature
guard; value updates go through a per-cell cache so an unchanged cell performs no dpg call.

## Requirements

#### MODIFIED: Mediagrid structural updates are signature-gated
**Before:** the per-source loop (tile theme/title/index badge) ran on every state push,
re-writing the same values.
**After:** `current_source` joins `current_signature`; the structural per-source updates run
only when the signature changed (list/order/name/index/columns/current_source).

#### ADDED: Per-cell value cache for value cells
Tile-alpha readouts and raw vimix-table cells are written through `_set_media_cell(tag,
value)` which skips `set_value` when the cached string is unchanged; the cache is cleared on
every table rebuild so freshly created widgets are never skipped incorrectly.

## Steps

1. Add `current_source` to `current_signature`; move the structural per-source updates
   (theme/title/index) inside the signature guard and clear the value cache there.
   → verify: `.venv/bin/python -m pytest tests/ -q -k mediagrid`
2. Add `_media_cell_cache` + `_set_media_cell()` and route the tile-alpha/raw-cell writes
   through it.
   → verify: `.venv/bin/python -m pytest tests/ -q -k mediagrid`
3. Regression tests: identical second push performs no dpg value calls; an alpha change
   updates exactly the alpha cells; a `current_source` change still refreshes themes.
   → verify: `.venv/bin/python -m pytest tests/ -q -k "mediagrid or steady_state"`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: run with live viOSC; the Mediagrid still shows sources, titles, index badges
   and the selected-source highlight, updating as before; selecting a source re-highlights.
3. Real rig: open Settings → the raw vimix table still shows live values.

## Out of scope

- Reducing the Mediagrid widget count (SPIKE-perf P3, deferred).
- Any change to the payload shape or OSC contract.

## Risks

- The value cache could wrongly skip a cell after a widget rebuild — mitigated by clearing
  the cache inside the signature guard (every rebuild).
- `current_source` in the signature makes the guard trigger on selection changes: that is
  the intended trigger for the theme loop, no behavior change.
