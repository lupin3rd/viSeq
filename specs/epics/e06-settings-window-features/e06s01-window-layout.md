# e06s01 — Window layout save/restore: snapshot/apply + Settings "Finestre" section + boot restore

**type:** feat
**risk:** P1
**context:** ui/windowing

**Context:** Windows boot at hard-coded pos/size; Settings/Logs are hidden, the rest are
visible. The user wants to save the current layout — which windows are open, their position
and size — and restore it. Storage: a JSON config file (`viseq_config.json` next to
`viseq.py`; the user delegated the mechanism). Restore is explicit (buttons) plus an
optional "Ripristina all'avvio" checkbox (default on) that re-applies the saved layout at
boot. The two untagged boot windows get explicit tags (`sequencer_window`,
`audio_window`) so the layout can address them. Monitor-player windows are dynamic: the
snapshot records whatever exists; restore applies only to windows that still exist (no
recreation across sessions — documented in SCOPE_LATEST).

## Requirements

#### ADDED: Window layout snapshot
The app can record, for every currently existing window, its tag, whether it is shown, its
position and its size, in a JSON-serializable structure (`snapshot_window_layout()`).

#### ADDED: Window layout restore
The app can re-apply a saved layout to the currently existing windows — position, size and
shown/hidden — skipping records whose window no longer exists (`apply_window_layout()`).

#### ADDED: JSON config persistence
A `viseq_config.json` file next to `viseq.py` stores the app config (layout + theme, see
e06s02). Load (`load_config`) and save (`save_config`) are defensive: missing/corrupt file
falls back to defaults without crashing; save is written via temp file + atomic replace and
failures are logged, not fatal.

#### ADDED: Settings "Finestre" section
The Settings window gains a "Finestre" section with "Salva layout" and "Ripristina layout"
buttons and a "Ripristina all'avvio" checkbox (persisted in the config, default on).

#### ADDED: Boot-time restore
When "Ripristina all'avvio" is enabled and a saved layout exists, boot applies it after the
windows are built and before the first frame.

## Steps

1. Add explicit tags to the two untagged boot windows (`tag="sequencer_window"`,
   `tag="audio_window"`) without changing any other kwarg.
   → verify: `.venv/bin/python -m pytest tests/ -q -k "audio_window or import_time"`
2. Add the config module: `CONFIG_PATH` (dir of viseq.py + `viseq_config.json`, overridable
   in tests), `DEFAULT_CONFIG`, `load_config()` / `save_config()` with defensive
   load/merge/save (atomic replace, errors logged via `log_error`).
   → verify: `.venv/bin/python -m pytest tests/ -q -k config`
3. Add `snapshot_window_layout()` and `apply_window_layout()`: fixed window list
   (`sequencer_window`, `audio_window`, `settings_window`, `vimix_media_window`,
   `logs_window`) plus every existing `monitor_player_*` window; per-window defensive
   try/except; restore skips missing tags.
   → verify: `.venv/bin/python -m pytest tests/ -q -k layout`
4. Add `save_layout_to_config()` / `restore_layout_from_config()` and
   `should_restore_layout_on_boot(cfg)` (default True when unset); wire boot restore right
   after the windows are built.
   → verify: `.venv/bin/python -m pytest tests/ -q -k "layout or boot"`
5. Add the Settings "Finestre" section: "Salva layout" / "Ripristina layout" buttons +
   "Ripristina all'avvio" checkbox bound to the config value; UI labels in Italian.
   → verify: `.venv/bin/python -m pytest tests/ -q -k "finestre or settings"`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: open Settings → Finestre; move/resize some windows and close Logs; "Salva
   layout"; move things again; "Ripristina layout" → windows return to the saved state.
3. Real rig: restart the app → the saved layout is applied automatically ("Ripristina
   all'avvio" on); unchecking it and restarting keeps boot defaults.
4. Real rig: corrupt `viseq_config.json` (invalid JSON) → app still boots with defaults.

## Out of scope

- Restoring monitor-player windows across sessions (recreated at runtime only).
- Per-window remember toggles; window docking persistence.

## Risks

- `get_item_pos`/width/height return None transiently for a fresh window — the snapshot
  uses per-window try/except and falls back to the boot defaults.
- The config write path (repo dir) may be read-only — save failures are logged and never
  fatal (graceful degradation).
