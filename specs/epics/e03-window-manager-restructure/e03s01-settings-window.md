# e03s01 — Settings window (hidden, opened from menubar) hosting the OSC config section

**type:** refactor
**risk:** P2
**context:** ui/windowing

**Context:** The "viOSC" window (WINDOW 3) is always visible with client/server OSC setup and
the raw vimix state table. The user wants a general "Settings" window that is hidden by
default and opened from a menubar entry, with the OSC configuration as its first section
(more sections later). `autostart_osc()` must keep working while the window is hidden
(status labels live on hidden items — set_value works regardless of visibility).

## Requirements

#### ENHANCED: Settings window with OSC section, hidden by default
**Before:** always-visible "viOSC" window (no_close), no menubar entry.
**After:** a "Settings" window (tag `settings_window`, `show=False` at boot, closable with X)
whose content is grouped under an "OSC" section header; a "Settings" entry in the top menubar
shows it again (`show_settings_window`).

## Steps

1. Convert the viOSC window into the hidden settings window with an "OSC" section header
   (client setup + server setup + raw state table move verbatim).
   → verify: `.venv/bin/python -m pytest tests/ -q -k settings_window`
2. Add the "Settings" menubar entry with the show callback; autostart still reaches the
   hidden status labels.
   → verify: `.venv/bin/python -m pytest tests/ -q -k settings`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: at boot the settings window is closed; clicking "Settings" in the top bar opens
   it (client/server statuses show the autostarted state); the X closes it and the menu
   reopens it.
