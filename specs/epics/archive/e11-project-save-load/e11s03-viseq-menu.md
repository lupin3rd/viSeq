# specs/epics/e11-project-save-load/e11s03-viseq-menu.md

# e11s03 — viSeq menu: Open / Last / Save / Exit + native file dialogs + submenu rebuild

**type:** feat
**risk:** P1
**context:** ui

**Context:** The menubar gains a "viSeq" menu as its FIRST item on the left
(confirmed with the user). It hosts Open project, Last project (submenu of
recent `.viseq` files), Save project and Exit. Open/Save use native DPG file
dialogs (verified against the installed 2.3.1: `add_file_dialog(*,
callback=..., default_path=..., default_filename=..., modal=...)`, callback
app_data carries `file_path_name` / `file_name` / `current_path`); the
Last-project submenu is rebuilt at boot and after every save/open.

## Requirements

#### ADDED: viSeq menu structure
`with dpg.viewport_menu_bar():` starts with `with dpg.menu(label="viSeq"):`
containing `Open project` (→ `show_open_project_dialog`), the `Last project`
submenu (tag `menu_last_project`, children rebuilt by
`rebuild_last_project_menu()`), a separator, `Save project` (→
`show_save_project_dialog`) and `Exit` (→ `exit_app`). Monitor / Show /
Settings / MIDI / Help keep their current order after it.

#### ADDED: File dialogs
`show_open_project_dialog()` / `show_save_project_dialog()` create-once
(`open_project_dialog` / `save_project_dialog` tags, `show=False`) and show
the matching DPG file dialog with `default_path=PROJECTS_DIR`; the save dialog
uses `default_filename="project.viseq"`. Callbacks read `file_path_name` from
app_data. Save ensures the `.viseq` extension.

#### ADDED: Open / Save flows
`open_project_file(path) -> bool`: `load_project_file` (None → logged, False)
→ `apply_project_state` → sync `cfg["theme"]` from the project theme →
`remember_recent_project` + `save_config` → `rebuild_last_project_menu` →
True. `save_project_file(path) -> bool`: `capture_project_state` →
`save_project_to_file` (ensures the projects dir exists) → remember +
`save_config` → `rebuild_last_project_menu` → True.

#### ADDED: Last-project submenu + Exit
`rebuild_last_project_menu()` deletes the children of `menu_last_project` and
adds one item per `recent_project_paths()` (label = basename, callback =
`open_recent_project`, user_data = path); with no recents it adds a disabled
"No recent projects" item. `exit_app()` calls `dpg.stop_dearpygui()`.

## Steps

1. RED: menu-structure tests — viSeq is the first menubar menu; it contains
   Open project / Save project / Exit items and a `menu_last_project` submenu;
   the four top-level menus after it are Monitor/Show/Settings/MIDI/Help →
   verify: `.venv/bin/python -m pytest tests/ -q -k "viseq_menu"`
2. GREEN: menu bar block + dialog creation with stable tags →
   verify: same command
3. RED: flow tests — `open_project_file` on a written project file applies
   state, remembers the path, rebuilds the submenu; on a bad file returns
   False without crashing; `save_project_file` writes a file the load path can
   read back →
   verify: `.venv/bin/python -m pytest tests/ -q -k "project_flow"`
4. GREEN: `open_project_file` / `save_project_file` /
   `remember-recent` wiring + `rebuild_last_project_menu` (empty → disabled
   item) → verify: same command
5. RED: dialog + exit tests — `exit_app` records a `stop_dearpygui` call; save
   dialog callback appends `.viseq`; open dialog callback routes to
   `open_project_file` →
   verify: `.venv/bin/python -m pytest tests/ -q -k "project_dialog"`
6. GREEN: `show_open_project_dialog` / `show_save_project_dialog` callbacks +
   `exit_app` → verify: same command
7. Full-suite verification →
   verify: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Verification Script (Step-by-Step)

1. Launch viseq: the menubar shows **viSeq** first, then Monitor, Show,
   Settings, MIDI, Help.
2. viSeq ▸ Save project: the dialog defaults to the `projects/` folder; pick a
   name → a `.viseq` file appears; viSeq ▸ Last project now lists it.
3. Change windows/theme/steps, viSeq ▸ Open project → pick the file: layout,
   colors and sequencer configuration come back.
4. viSeq ▸ Exit closes the app.
5. Delete a recent `.viseq` file: it disappears from Last project on the next
   run.

## Out of scope

- The Settings checkbox and boot restore (e11s04).
- The capture/apply/serialize core (e11s01) and the config list (e11s02).
- Reordering the existing Monitor/Show/Settings/MIDI/Help entries.

## Risks

- DPG file dialogs are modal and app_data-driven; the exact key
  (`file_path_name`) is the documented 2.3.1 contract — the callbacks read it
  defensively (`app_data.get("file_path_name")`).
- The submenu must be rebuilt after the menubar exists; the boot rebuild runs
  right after the `viewport_menu_bar` block (module level), like
  `autostart_osc`.
