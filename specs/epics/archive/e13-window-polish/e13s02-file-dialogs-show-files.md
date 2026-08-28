# specs/epics/e13-window-polish/e13s02-file-dialogs-show-files.md

# e13s02 — Project dialogs show existing .viseq files

**type:** fix
**risk:** P1
**context:** ui

**Context:** The user reports the Open/Save project dialogs do not list the
existing project files inside `projects/`. Verified against the installed DPG
2.3.1: a file dialog with **no** `add_file_extension` filters defaults to
showing directories only ("If no file extensions have been added, the selector
defaults to directories"). The dialogs were also created once at module level
with `show=False`; the robust documented pattern recreates them on demand,
right before showing, with the filters in place.

## Requirements

#### MODIFIED: Open/Save project dialogs
**Before:** two `dpg.file_dialog` items created once at module level with
`show=False`, `default_path=PROJECTS_DIR` and no file-extension filters —
existing `.viseq` files were invisible (directories-only selector).
**After:** `show_open_project_dialog()` / `show_save_project_dialog()` delete
any previous dialog instance and recreate it with
`dpg.add_file_extension(".viseq")` and `dpg.add_file_extension(".*")` filters,
`default_path=PROJECTS_DIR` (created first), `default_filename="project.viseq"`
on the save dialog, then `dpg.show_item(...)`. Callbacks and tags
(`open_project_dialog` / `save_project_dialog`) are unchanged.

## Steps

1. RED: dialog tests — each dialog carries `.viseq` and `.*` extension
   filters; `show_open_project_dialog()` recreates the dialog (delete + add)
   and shows it; the save dialog keeps the default filename →
   verify: `.venv/bin/python -m pytest tests/ -q -k "project_dialog"` (updated)
2. GREEN: recreate-on-demand in the two show functions + extension filters →
   verify: same command
3. Full-suite verification →
   verify: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Verification Script (Step-by-Step)

1. Save a project via viSeq ▸ Save project: the dialog lists the existing
   `.viseq` files (and the `.*` all-files filter is selectable).
2. viSeq ▸ Open project: the same files are visible and openable.
3. Cancel the dialog: the app stays put, no crash.

## Out of scope

- Changing the save/open flow logic (e11s03) or the file format.
- Native (non-DPG) file dialogs — DPG's file_dialog with filters is the
  documented in-app pattern for this single-file GUI.

## Risks

- Issue #2080 (files not showing even with `.*` on some systems): the empty
  string `""` fallback filter is added defensively alongside `.viseq` and `.*`.
- Deleting and recreating the dialog must not leak tags — the delete guard
  (`dpg.does_item_exist`) keeps each show idempotent.
