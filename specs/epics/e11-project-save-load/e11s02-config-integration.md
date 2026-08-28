# specs/epics/e11-project-save-load/e11s02-config-integration.md

# e11s02 — Config integration: recent projects + restore flag + legacy layout-key migration

**type:** feat
**risk:** P2
**context:** persistence

**Context:** `viseq_config.json` currently stores the window layout under a
top-level `layout` key. With projects (e11s01) the layout, theme and sequencer
state move into `.viseq` files; the config keeps only app-level concerns:
MIDI, the fallback theme, the recent-projects list and the
restore-last-project-at-boot flag.

## Requirements

#### MODIFIED: viseq_config.json schema
**Before:** `DEFAULT_CONFIG` = `{"layout": {"restore_on_boot": True, "windows": []}, "theme": {...}, "midi": {...}}`; `load_config` merges any top-level key, so stale keys survive forever.
**After:** `DEFAULT_CONFIG` = `{"theme": {...}, "midi": {...}, "projects": {"recent": [], "restore_last_on_boot": True}}`; `load_config` drops unknown top-level keys from the merged result, so a legacy `layout` block is ignored and disappears from the file on the next save.

#### ADDED: Recent-projects list management
`RECENT_PROJECTS_MAX = 5`. `remember_recent_project(cfg, path) -> list[str]`
inserts at the front, de-duplicates, and caps the list. `recent_project_paths(cfg)`
returns the list with non-existent files pruned (a stale entry must never
survive to the Last-project menu). Persisted via the existing `save_config`.

#### ADDED: Restore-last-project flag
`should_restore_last_project_on_boot(cfg) -> bool` (default True when unset)
and `on_restore_project_boot_toggle(sender, app_data, ...)` persisting the
Settings checkbox to `cfg["projects"]["restore_last_on_boot"]`.

#### REMOVED: Legacy layout config tests
**Before:** tests asserted `cfg["layout"]["restore_on_boot"]`, `save_layout_to_config` persistence and `restore_layout_from_config` application.
**After:** (removed) — the layout save/restore buttons die with the settings Windows section in e11s04; config-level coverage moves to the projects flag. The `snapshot_window_layout` / `apply_window_layout` unit tests stay (the functions are reused by projects).

## Steps

1. RED: config tests — `DEFAULT_CONFIG` has `projects.recent == []` and
   `restore_last_on_boot is True`; a legacy config file with a `layout` key
   loads without `layout`; round trip preserves `projects` →
   verify: `.venv/bin/python -m pytest tests/ -q -k "project_config"`
2. GREEN: `DEFAULT_CONFIG` change + `load_config` unknown-key drop →
   verify: same command
3. RED: recent-list tests — insert front, dedupe, cap at 5, prune missing
   files; flag default + toggle persistence →
   verify: `.venv/bin/python -m pytest tests/ -q -k "recent_project"`
4. GREEN: `remember_recent_project`, `recent_project_paths`,
   `should_restore_last_project_on_boot`, `on_restore_project_boot_toggle` →
   verify: same command
5. Update the six legacy layout config tests (defaults/corrupt/round-trip →
   projects section; save/restore-layout tests removed; boot-toggle test →
   project flag) →
   verify: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Verification Script (Step-by-Step)

1. (Automated) Write a legacy config (with `layout`), load it: no `layout` key,
   `projects` present; save + reload: file no longer contains `layout`.
2. (Automated) Remember the same path twice + a 6th path: list stays ≤ 5,
   most recent first, no duplicates; missing files pruned.
3. (Automated) Toggle the boot flag through the callback; reload proves it
   persisted.

## Out of scope

- The project capture/apply core (e11s01) and the file dialogs (e11s03).
- Deleting `save_layout_to_config` / `restore_layout_from_config` /
  `should_restore_layout_on_boot` / `on_restore_layout_boot_toggle` — they
  stay defined until e11s04 removes their settings buttons.

## Risks

- Existing tests assert `cfg["layout"]`; step 5 updates them in the same slice
  as the schema change, so the suite never sits red.
- Dropping unknown top-level keys is a behavior change for any future config
  key added out-of-band; the merge still reads them once, and the documented
  schema is the single source of truth.
