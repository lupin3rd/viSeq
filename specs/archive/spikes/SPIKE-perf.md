# Spike: perf — where does viseq's CPU go?

## Question

Which parts of viseq's per-frame and per-update work dominate CPU, and how much can be
saved by throttling the Mediagrid updates, the main-loop render and the monitor refresh?

## Result

Answered (headless). The app's Python-side work is mostly cheap; two real costs exist:
(1) the per-push Mediagrid update does redundant per-source dpg work on **every** viOSC
state push, and (2) the main loop renders at 60 fps unconditionally while the render pass
cost scales with the number of live widgets (the Mediagrid can hold thousands). Everything
else (FFT, essentia, monitor refresh, thumbnails) is negligible.

## Findings

Measured with the real module (stub dpg for widget counts, real numpy/essentia for math,
real DPG 2.3.1 for per-widget costs; no GPU, so the render pass itself is not measured).

| Path | Measurement | Verdict |
|------|-------------|---------|
| Mediagrid full rebuild (`update_vimix_sources_ui`, signature change) | 1.2 ms / 397 widgets @10 src → 14.8 ms / 7282 widgets @200 src (Python side) | Rebuilds are **signature-gated** (name/index/order/cols) → rare in a session. But the widget **count** (7k @200 src) taxes every rendered frame. |
| Mediagrid steady state (same payload, 100 iters) | 0.12 ms @10 src → 2.6 ms @200 src per push, **2 dpg calls per source per push** | The per-source loop (bind theme + title + index badge) runs on **every push** even when nothing changed. At 30 Hz push with 200 sources ≈ 78 ms/s Python + 12 000 dpg calls/s. **Biggest measurable cost.** |
| Mediagrid, value-only change (alpha tweak, same list) | identical to steady state (0.63 vs 0.61 ms @50 src) | Confirms: value changes do **not** add work — the redundant per-source loop already runs unconditionally. |
| Main-loop per-frame (0/2/6 monitor players) | 0.001 / 0.009 / 0.026 ms per frame (Python), 0.6 / 8 / 24 dpg calls per frame | Negligible CPU; scales linearly with player count. |
| `compute_spectrum_bars` (real numpy FFT, 2048) | 0.069 ms per analysis (≈14 kHz sustainable) | Negligible at 30 fps. |
| essentia `RhythmExtractor2013` (real, 2 s @ 44.1 kHz) | multifeature 17.9 ms, degara 4.1 ms per analysis | At the app's 1 Hz cadence = **1.8% / 0.4% of one core**. Not a bottleneck. |
| essentia `LowPass` (2 s slice) | 0.43 ms | Negligible. |
| Real-DPG `add_text` / `configure_item` | 0.6 / 1.1 µs per widget (creation/configure only) | Layout + render are deferred to `render_dearpygui_frame` → **not measurable headless**; this is a lower bound. |

## Evidence

```
A2 — steady state, signature unchanged (100 iters of same payload)
    10 sources:   0.121 ms/call |  20.0 dpg calls/call
    50 sources:   0.613 ms/call | 100.0 dpg calls/call
   200 sources:   2.627 ms/call | 400.0 dpg calls/call

A2b — steady state with a CHANGED VALUE only (alpha tweak)
    50 sources:   0.635 ms/call | 100.0 dpg calls/call   (≈ same as unchanged)

B — main-loop per-frame work with N monitor players
  6 monitors:  0.026 ms/frame | 24.0 dpg calls/frame

C — compute_spectrum_bars: 0.069 ms/analysis
D — essentia RhythmExtractor2013: multifeature 17.9 ms, degara 4.1 ms  (1 Hz cadence)
E — real-DPG add_text 0.6 µs, configure_item 1.1 µs (deferred layout/render)
```

The per-source loop source of truth (viseq.py, `update_vimix_sources_ui`, post-signature
block): `bind_item_theme` (selected/normal — depends on `current_source` + index),
`set_value(tile_title)` (depends on `name`), `configure_item(tile_index)` (depends on
`index`). All three fields are covered by the rebuild signature **except `current_source`**,
so the loop cannot be skipped wholesale until `current_source` joins the signature.

## Implications for the plan

1. **P0 — Gate the per-source update loop on the signature (biggest measurable win).**
   Add `current_source` to `current_signature`; run the per-source loop only when the
   signature changed. Value-only pushes (alpha/seek/speed churn) then cost only
   parse + O(N) prune + signature build (~0.1 ms @200 src) instead of 2.6 ms + 400 dpg
   calls. Zero behavior change: the grid shows name/index/selection, all signature fields.
2. **P1 — Idle render throttling.** The main loop renders unconditionally at 60 fps.
   Render at full rate only while animating (spectrum on, any monitor playing, `is_playing`);
   otherwise ~30 fps. Saves GPU/CPU when idle. Needs a real-rig acceptance for input feel.
3. **P2 — Monitor refresh: skip configure when values are unchanged** (cache last
   speed/seek/alpha per player) and throttle to ~30 fps.
4. **P3 — Mediagrid widget count (structural, deferred).** 7k widgets @200 sources tax the
   render pass every frame; lighter tiles or virtualization would help, but the render pass
   is not measurable headless — validate on the real rig first.
5. **Not worth it:** spectrum FFT, essentia cadence, fade ticks, thumbnails, module split
   (zero perf effect by construction — Python module imports are cached).

## What was NOT explored

- The DPG **render pass** (layout + draw) — needs a real display; the per-widget numbers
  above are creation/configure lower bounds. The render-pass cost of the Mediagrid's widget
  count is the main unknown (P3).
- Actual viOSC push rate on the real rig — the per-push savings scale linearly with it.
- GPU load / vsync behavior.

## Recommendation

Proceed with a small optimization epic (P0 + P1 + P2) ordered by measured impact, each with
regression tests on the headless harness (signature gating is directly testable; the render
throttle and monitor skip are testable via call-count assertions). P3 first needs a
real-rig frame-time measurement. Module split remains a maintainability refactor, not a
performance lever.
