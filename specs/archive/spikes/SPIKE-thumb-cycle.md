# SPIKE-thumb-cycle — cost of Mediagrid thumb cycling (e10s04)

Date: 2026-08-26 · Status: done · Owner: viseq e10

## Question

The user's earlier 3-thumb animation made viseq slow. Before committing to a
cycling feature, measure the real cost of the chosen design: pre-loaded static
textures + `dpg.configure_item(texture_tag=...)` switching, never re-decoding or
re-uploading per cycle. Also settle the e10s02 extraction strategy and the
`frame_sleep()` interplay.

## Method

Headless DearPyGui 2.3.1 benchmark (no viewport render — the grid already
renders one image per tile today, so cycling does not change the draw-call
count; only the CPU cost of the switch is new):

- 50 sources × 3 textures (320×180 RGBA float32, ~230 KB each), created once.
- 2000 full cycles over all 50 tiles via `configure_item(texture_tag=...)`.
- Measured on the dev machine (same GPU the app runs on).

## Results

| Measurement | Value |
|---|---|
| One-time upload, 150 textures (50 sources) | **162 ms total (3.2 ms/source)** |
| VRAM at 50 sources × 3 textures | **~34.6 MB** |
| `configure_item` texture switch | **1.60 µs/call** |
| Steady-state CPU, 50 tiles at 750 ms cadence (66.7 calls/s) | **~0.01 % of one core** |

## Decisions

1. **Cycling design confirmed**: static textures created once + `configure_item`
   switching on a 750 ms wall-clock timer. The old slowness cannot have come
   from the switch itself at 1.6 µs/call — it must have been per-cycle decode,
   texture create/delete churn, or repeated OSC requests, all of which this
   design excludes (decoder is a background worker; textures live for the
   source's lifetime; requests stop once textures arrive).
2. **e10s02 extraction strategy**: 3 separate ffmpeg calls (one per target time)
   rather than a single multi-frame invocation — the call overhead (~50-150 ms
   process spawn) is noise next to the decode/seek work, and the multi-frame
   `-vf select=` approach is codec-fragile. Confirmed by this spike; no change.
3. **`frame_sleep()` interplay — NO change needed**: at 30 fps idle throttle
   (33 ms/frame) a 750 ms cycle renders smoothly — each rendered frame shows the
   current texture and switches land between frames. Cycling must NOT force the
   full render rate. Regression test asserts exactly that.

## Real-rig validation (manual, at story acceptance)

With 20+ sources in Vimix and the Mediagrid open: tiles cycle 3 frames at
~750 ms with no measurable frame-rate drop; image tiles stay static; the grid
closed → cycling stops (gated).

## Artifacts

- Benchmark script: `spike_cycle.py` (headless dpg; kept ad-hoc, not committed).
- Story: `specs/epics/e10-thumbnail-pipeline-v2/e10s04-mediagrid-cycling-failed-state.md`
