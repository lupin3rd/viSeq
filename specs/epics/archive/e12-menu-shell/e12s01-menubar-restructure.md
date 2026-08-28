# specs/epics/e12-menu-shell/e12s01-menubar-restructure.md

# e12s01 — Menubar restructure: viSeq | Windows | Settings

**type:** feat
**risk:** P2
**context:** ui

**Context:** The user confirmed the e11 UAT and asked to streamline the menubar
further: after viSeq (unchanged), the second menu becomes "Windows" (New
Monitor Player, Show Logs, Show Info) and the third becomes "Settings"
(General = the Settings window, MIDI). The standalone Monitor and Show menus
and the top-level Settings/MIDI/Help entries disappear.

## Requirements

#### MODIFIED: Menubar layout
**Before:** `viSeq | Monitor | Show | Settings(item) | MIDI(item) | Help(item)`
**After:** `viSeq | Windows | Settings` where:
- `Windows` ▸ `New Monitor Player` (new_monitor_player), `Show Logs`
  (show_logs_window), `Show Info` (show_help_window — the About window keeps
  its "Help" title),
- `Settings` ▸ `General` (show_settings_window), `MIDI` (show_midi_window).

No other window, callback or config behavior changes.

## Steps

1. RED: menu-structure tests — menubar labels are exactly
   `["viSeq", "Last project", "Windows", "Settings"]`; Windows hosts the three
   items, Settings hosts General + MIDI; no Monitor/Show menus and no
   top-level Settings/MIDI/Help items → verify:
   `.venv/bin/python -m pytest tests/ -q -k "menubar_shell"`
2. GREEN: restructure the `viewport_menu_bar` block →
   verify: same command
3. Update the four legacy menu tests (Show-menu test, Help-entry test, MIDI
   item test, viSeq-order test) to the new structure; full suite green →
   verify: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Verification Script (Step-by-Step)

1. Launch viseq: the menubar reads **viSeq | Windows | Settings**.
2. Windows ▸ New Monitor Player opens a monitor player; Windows ▸ Show Logs
   opens the OSC Logs window; Windows ▸ Show Info opens the About window.
3. Settings ▸ General opens the Settings window; Settings ▸ MIDI opens the
   MIDI window.
4. viSeq ▸ Open/Last/Save/Exit still work as before.

## Out of scope

- Renaming the About window title (stays "Help").
- Any change to windows, callbacks or the e11 project flows.
- Keyboard shortcuts or icons.

## Risks

- Import-time menu assertions in the test harness: the four legacy tests must
  be updated in the same slice as the menu change so the suite never sits red.
- `import_time_menu_items` is a flat label→callback map, so item lookups work
  regardless of nesting — only the menu-set assertions change.
