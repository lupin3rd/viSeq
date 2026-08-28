# specs/epics/e11-project-save-load/e11s04-settings-and-boot-restore.md

# e11s04 — Settings restructure + boot restore of the last project

**type:** feat
**risk:** P1
**context:** ui

**Context:** With projects in place (e11s01-03), the Settings window's Windows
section (Save layout / Restore layout / Restore at startup) is obsolete — the
user explicitly asked to remove it and put a "Restore last project at startup"
configuration at the TOP of the Settings window. Boot (`apply_boot_config`)
re-applies the most recent project when the flag is on.

## Requirements

#### MODIFIED: Settings window top section
**Before:** Settings opens with the OSC section; the Windows section (Save
layout, Restore layout buttons + "Restore at startup" checkbox
`cb_restore_layout_boot`) sits between the OSC server block and the Theme
section.
**After:** Settings opens with a Project section FIRST: a checkbox
"Restore last project at startup" (`cb_restore_project_boot`, default True,
callback `on_restore_project_boot_toggle`), then the OSC section; the Windows
section and its widgets are gone.

#### REMOVED: Layout save/restore helpers
**Before:** `save_layout_to_config`, `restore_layout_from_config`,
`should_restore_layout_on_boot`, `on_restore_layout_boot_toggle` and their
settings widgets existed.
**After:** (removed) — dead code deleted per conventions; their behavior is
superseded by project save/load. `snapshot_window_layout` / `apply_window_layout`
and `LAYOUT_WINDOW_TAGS` / `LAYOUT_ALWAYS_HIDDEN_TAGS` stay (reused by
project capture/apply).

#### MODIFIED: apply_boot_config
**Before:** applied the config theme and, when `should_restore_layout_on_boot`,
the saved window layout.
**After:** applies the config theme (fallback look), syncs the
`cb_restore_project_boot` checkbox, and when
`should_restore_last_project_on_boot(cfg)` and a recent project file exists,
loads and applies it via `load_project_file` + `apply_project_state`.

## Steps

1. RED: settings tests — `cb_restore_project_boot` exists with default True and
   the project callback; the Save layout / Restore layout buttons and
   `cb_restore_layout_boot` are GONE; the project section precedes the OSC
   section →
   verify: `.venv/bin/python -m pytest tests/ -q -k "project_settings"`
2. GREEN: settings window restructure (Project section on top, Windows section
   removed) → verify: same command
3. RED: boot-restore tests — with the flag on and a recent project file,
   `apply_boot_config` applies its state (tracked via the stub: set_value /
   configure_item calls + globals); with the flag off it does not; with no
   recents it applies only the theme →
   verify: `.venv/bin/python -m pytest tests/ -q -k "boot_project"`
4. GREEN: `apply_boot_config` rewrite + delete the four layout helpers →
   verify: same command
5. Update `test_settings_window_has_windows_section` to assert the new Project
   section and the absence of the Windows widgets →
   verify: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Verification Script (Step-by-Step)

1. Open Settings: the first section is "Project" with "Restore last project at
   startup" checked; no Windows section exists anymore.
2. Save a project, restart viseq: the last project's layout, theme and
   sequencer configuration are restored automatically.
3. Uncheck "Restore last project at startup", restart: the app boots with the
   fallback config theme and default layout, and the checkbox stays off.

## Out of scope

- The project core (e11s01), config list (e11s02) and menu (e11s03).
- Any change to the Theme section or the OSC section contents.

## Risks

- Removing `cfg["layout"]` from the schema happened in e11s02; the layout
  helpers here are the last references and die together with their widgets, so
  no dangling callbacks remain.
- Boot restore runs before the menubar exists; `apply_project_state` touches
  only windows/theme/sequencer widgets, all built at import — the same
  precondition `apply_window_layout` already had.
