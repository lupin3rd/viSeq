# specs/epics/e13-window-polish/e13s01-window-renames.md

# e13s01 — Window renames + em-dash glyph fix + APP_VERSION 0.1.0

**type:** feat
**risk:** P2
**context:** ui

**Context:** The user wants the window titles homogenized with the new menu
shell: the Settings window becomes "General", "OSC Logs" becomes "Logs" and
"Help" becomes "Info". Inside the Info window the title line rendered a "?"
— the em dash (U+2014) has no glyph in ProggyClean, the default font, exactly
like the U+2026 ellipsis fixed in e10s06. Finally, this is declared version
0.1.0 of viSeq.

## Requirements

#### RENAMED: Settings window -> General
**Before:** `dpg.window(label="Settings", tag="settings_window", ...)`
**After:** `dpg.window(label="General", tag="settings_window", ...)` — tag,
menubar menu "Settings" and its "General" item are untouched.

#### RENAMED: OSC Logs window -> Logs
**Before:** `label="OSC Logs"` (tag logs_window)
**After:** `label="Logs"` (tag logs_window)

#### RENAMED: Help window -> Info
**Before:** `label="Help"` (tag help_window)
**After:** `label="Info"` (tag help_window)

#### MODIFIED: Em-dash glyphs in visible UI strings
**Before:** `"viSeq — Audio-Reactive VJ Controller for Vimix"` (Info window)
and `dpg.add_text("—", ...)` (band value separator) used U+2014.
**After:** ASCII `-` in both; the Info window title renders without the
fallback glyph.

#### MODIFIED: APP_VERSION
**Before:** `APP_VERSION = "1.1.0"`
**After:** `APP_VERSION = "0.1.0"` — the About line and release identity follow.

## Steps

1. RED: window-label tests — settings_window label "General", logs_window
   label "Logs", help_window label "Info"; APP_VERSION "0.1.0"; no U+2014 in
   any import-time UI label/text → verify:
   `.venv/bin/python -m pytest tests/ -q -k "window_labels or no_em_dash or app_version"`
2. GREEN: rename the three window labels, replace both em dashes, bump
   APP_VERSION → verify: same command
3. Update the two legacy window-label assertions (settings hidden test, help
   hidden test); full suite green →
   verify: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`

## Verification Script (Step-by-Step)

1. Settings ▸ General opens a window titled **General**.
2. Windows ▸ Show Logs opens a window titled **Logs**.
3. Windows ▸ Show Info opens a window titled **Info** with the title line
   "viSeq - Audio-Reactive VJ Controller for Vimix" (hyphen, no "?" glyph).

## Out of scope

- The menubar structure (e12) and the file dialogs (e13s02).
- Window tags (settings_window / logs_window / help_window stay — layout
  tracking, menu callbacks and tests depend on them).

## Risks

- Two import-time tests assert the old labels; they are updated in the same
  slice (step 3) so the suite never sits red.
- Other visible U+2014 occurrences in comments/constants are left alone —
  only the two rendered strings change.
