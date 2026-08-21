# viseq — Tech Stack (seed-conventions bootstrap)

Single-file DearPyGui application. Derived from the e01 migration context;
deepen with `map-codebase` when feature work resumes.

## Stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Language | Python 3.13 | cp313 wheels required for dearpygui/essentia |
| GUI | dearpygui 2.3.1 | `dearpygui.dearpygui` namespace preserves the 1.x API surface |
| Audio analysis | essentia 2.1b6.dev1389 | RhythmExtractor2013 (BPM) + LowPass; dev1438 is cp314-only |
| Audio I/O | sounddevice 0.5.6 | ring-buffer capture (L-2), preallocated + modulo indexing |
| OSC | python-osc 1.10.2 | client to viOSC (port 6666) + listening server (6667) |
| Imaging | Pillow >=11,<13 | thumbnail decode with pixel caps (MED-6) |
| Numerics | numpy >=2,<3 | audio buffer, texture flattening |

## Architecture (single file: `viseq.py`)

- **Main thread** — owns ALL dpg UI access; drains `ui_task_queue`.
- **Worker threads** — audio callback, essentia analyzer, visual metronome,
  sequencer/fade ticks, thumbnail decoder. They never call dpg directly
  (HIGH-1); UI mutations go through `ui_task_queue` / `enqueue_set_value`.
- **Data** — module-global state: `tracks_data` (8 tracks × 8 steps, 5 step
  types), `global_vimix_state` (source list from viOSC), `thumbnails_data`,
  `request_timestamps`, ring buffer `audio_buffer`.
- **OSC contract with viOSC is frozen** — addresses/payloads in
  `specs/planning-context.yaml`.

## Gray areas

- dpg API is untyped (no stubs): `ignore_missing_imports = true`; dpg calls
  resolve to `Any`.
- GUI rendering is not verifiable headless — manual acceptance on the real rig.
- `get_dearpygui_version()` before `create_context()` segfaults (spike
  landmine): no dpg calls before context creation.
