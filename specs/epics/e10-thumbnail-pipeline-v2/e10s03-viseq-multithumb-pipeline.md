# e10s03 — viseq multi-thumb pipeline: idx propagation, 3 textures per source, L-1 pruning

**type:** feat
**risk:** P1
**context:** thumbnail-pipeline

**Context:** viseq currently stores ONE texture per source and discards the reply
index: `thumbnails_data: dict[str, str]` maps a source to a single `tex_<name>`,
and the decoder worker drops the `idx` segment of `/viosc/replythumb/<name>/<idx>`.
This story threads the index through decode and texture creation so a source maps
to a list of up to 3 static textures (`tex_<name>_0/1/2`), created once and pruned
as a set by L-1. No visual change yet — e10s04 adds the cycling.

## Requirements

#### MODIFIED: thumbnails_data holds a texture list per source
**Before:** `thumbnails_data[target_id] -> str` (single texture tag); the decode
index is discarded.
**After:** `thumbnails_data[target_id] -> list[str]` of up to 3 tags
(`tex_<name>_<idx>`); the first texture (idx 0) creates the tile image, later
indices append to the list without deleting/recreating the image, container, or
the "Loading..." text once cleared.

#### MODIFIED: L-1 stale pruning drops the whole texture set
**Before:** prune deletes the single `tex_<target_id>` tag.
**After:** prune deletes every `tex_<target_id>_<idx>` tag and the list entry for
sources no longer present; `request_timestamps` pruning is unchanged.

#### ADDED: Consumers resolve the display texture from the list
The Mediagrid tile, the sequencer clip slot and the monitor player each resolve
the current frame from the per-source list (index 0 until cycling lands); a
missing list keeps the existing "Loading…"/"Waiting…" states.

## Steps

1. RED: decoder + main-loop test asserting idx propagates into
   `tex_<name>_<idx>` and `thumbnails_data[name]` becomes a list with 1..3
   entries → verify: `.venv/bin/python -m pytest tests/ -q -k thumb`
2. GREEN: thread idx through `incoming_osc_handler` -> `blob_queue` ->
   `thumbnail_decoder_worker` -> `texture_queue` -> main-loop texture path;
   build the per-source list → verify: `.venv/bin/python -m pytest tests/ -q -k thumb`
3. RED: grid tile test — image created on first texture (idx 0) only; later
   indices append without widget churn; "Loading..." removed exactly once →
   verify: `.venv/bin/python -m pytest tests/ -q -k thumb`
4. GREEN: first-texture-wins image creation; append-only for idx 1/2 →
   verify: `.venv/bin/python -m pytest tests/ -q -k thumb`
5. RED: L-1 prune test — a removed source drops all 3 tags and the list entry →
   verify: `.venv/bin/python -m pytest tests/ -q -k l1`
6. GREEN: prune the whole set; keep the signature-gated rebuild (e07 P0)
   regression green → verify: `.venv/bin/python -m pytest tests/ -q`

## Verification Script (Step-by-Step)

1. Run the full suite — all prior behavior (single-thumb display, clip slots,
   monitor players) must stay green.
2. On the real rig with e10s02 live: load 2 videos; confirm the Mediagrid,
   sequencer slot and monitor player all show a frame (index 0) — no regression
   to the "Loading…" state for the sources that previously worked.
3. Remove a source in Vimix; confirm its 3 texture tags are gone (memory doesn't
   grow across churn — L-1).

## Out of scope

- The cycling animation itself (e10s04).
- Any OSC contract change (idx semantics are already in the frozen contract).
- Storage of more than 3 textures per source.

## Risks

- Changing `thumbnails_data`'s shape touches every consumer (grid tile, clip
  slot, monitor player, prune, regen callback, texture path) — all in the single
  file; the headless harness covers the logic, manual smoke covers the visuals.
- The regen callback currently pops the single entry; it must pop the list.
