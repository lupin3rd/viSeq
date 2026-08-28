# specs/epics/e11-project-save-load/e11s01-project-file-core.md

# e11s01 — Project file core: capture / apply / JSON save+load / sanitization

**type:** feat
**risk:** P0
**context:** persistence

**Context:** The user wants a project to be a text file containing the window
layout, the theme and every sequencer configuration, saved/loaded from the new
viSeq menu (e11s03). This story builds the headless-testable core: capture the
live state into a dict, apply a dict back onto the live app, write/read the
JSON file, and sanitize anything suspicious (hand-edited or older files).

## Requirements

#### ADDED: Project format constants
`PROJECT_FORMAT = "viseq-project"`, `PROJECT_VERSION = 1`,
`PROJECT_FILE_EXTENSION = ".viseq"`, `PROJECTS_DIR = <viseq.py dir>/projects`
(created on demand by the save flow). A project file is a JSON text document
with `{"format", "version", "layout", "theme", "sequencer"}`.

#### ADDED: capture_project_state() -> dict
Snapshots, in one call: the window layout (`snapshot_window_layout()`), the
theme (preset label from the `theme_preset` combo → preset key, plus a copy of
`active_palette`), and the sequencer state: `beat_source`, manual BPM
(`manual_bpm_input` widget), the 8x8 step cells + per-track `target_id` /
`base_address` (runtime-only `last_rand_*` keys stripped), and the audio
section (`combo_devices` value, `cb_lowpass`, per-band
enabled/start/end/min/max from the band widgets).

#### ADDED: apply_project_state(state: dict) -> None
Re-applies a sanitized state on the main thread: window layout via
`apply_window_layout`, theme via `_apply_theme_config`, beat source via the
existing `midi_action_beat_source` (checkboxes + manual-widget visibility),
manual BPM readouts, `tracks_data` (deep-copied, `base_address` recomputed
from `target_id`), full step-cell + clip-slot UI rebuild (`update_step_ui` /
`update_track_slot_ui` per row/col), low-pass checkbox + global, band sliders
+ `bands_enabled` + `refresh_band_value` for enabled bands, and the audio
device combo when the saved device still exists (graceful skip otherwise).

#### ADDED: save_project_to_file(path, state) -> bool / load_project_file(path) -> dict | None
Atomic JSON write (tmp + `os.replace`, mirroring `save_config`) returning
False with a logged reason on failure. Load validates format + version,
sanitizes via `_sanitize_project_state` and returns None (logged) on any
malformed input — the app never crashes on a bad project file.

#### ADDED: _sanitize_project_state(raw) -> dict
Coerces every section to the expected shape: steps filled from per-step
defaults (missing keys healed), beat source validated against
`BEAT_SOURCE_LABELS` (fallback `bpm_analysis`), theme palette through the
existing `_sanitize_palette`, bands against `BAND_DEFAULT_RANGES`, layout
records passed through unchanged (`apply_window_layout` skips unknown tags).

## Steps

1. RED: capture tests — with the DpgStub values seeded (manual_bpm_input,
   combo_devices, cb_lowpass, band widgets, theme_preset) and a known
   `tracks_data`/`beat_source`, `capture_project_state()` returns layout +
   theme + sequencer sections; `last_rand_*` keys absent → verify:
   `.venv/bin/python -m pytest tests/ -q -k "project_capture"`
2. GREEN: `capture_project_state()` + `STEP_PERSISTED_KEYS` strip →
   verify: same command
3. RED: apply tests — a state dict applied over the stub yields mutated
   `tracks_data`, `beat_source`/`lowpass_enabled` globals, set_value calls for
   checkboxes/sliders/combo, per-row slot + cell UI rebuild calls →
   verify: `.venv/bin/python -m pytest tests/ -q -k "project_apply"`
4. GREEN: `apply_project_state()` reusing `apply_window_layout`,
   `_apply_theme_config`, `midi_action_beat_source` →
   verify: same command
5. RED: file IO tests — save/load round trip via tmp_path; missing file →
   None; wrong format/version → None; corrupt JSON → None →
   verify: `.venv/bin/python -m pytest tests/ -q -k "project_file"`
6. GREEN: `save_project_to_file` (atomic) + `load_project_file` +
   `_sanitize_project_state` (partial step dict healed, bad beat_source
   falls back) → verify: same command
7. Full-suite verification →
   verify: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Verification Script (Step-by-Step)

1. (Automated) Round trip: build a state, save to a tmp file, load it back,
   assert deep equality.
2. (Automated) Apply: mutated globals + recorded widget sets match the state.
3. (Real rig, after e11s03) Arrange the windows, pick a theme, program a few
   steps, Save project — the `.viseq` file contains layout/theme/sequencer
   sections; Open project restores them.

## Out of scope

- The viSeq menu, file dialogs and Last-project submenu (e11s03).
- The Settings checkbox and boot restore (e11s04).
- MIDI bindings stay in `viseq_config.json` (device-specific, not a project
  concern — confirmed with the user).
- Persisting thumbnails or viOSC-side state.

## Risks

- `apply_project_state` runs dpg calls at boot before the viewport shows; the
  existing `apply_boot_config` already does the same for layout/theme, and all
  target widgets (sequencer cells, audio window) exist by then.
- A project saved with a monitor player open records its tag; on restore the
  window does not exist yet and `apply_window_layout` skips it gracefully.
