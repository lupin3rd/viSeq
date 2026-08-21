# e03s02 — Show menu + Logs window (hidden by default) with newest-first rendering

**type:** feature
**risk:** P2
**context:** ui/windowing

**Context:** The "OSC Logs" window (WINDOW 5) is always visible and appends lines oldest-first
(the newest line sits at the bottom, below the window's visible area). The user wants a "Show"
menu in the top bar with a "Logs" entry that opens the logs window, which is hidden by default,
and the newest log line at the top.

## Requirements

#### ENHANCED: Logs window hidden by default, opened from Show menu, newest-first
**Before:** always-visible "OSC Logs" window; `"\n".join(osc_log_history)` renders oldest-first.
**After:** a hidden "Logs" window (tag `logs_window`, `show=False`, closable with X) opened by
"Show" > "Logs"; the log text renders newest-first via a pure `format_osc_log(history)` helper.

## Steps

1. Extract `format_osc_log(history) -> str` (join reversed) and use it in the main loop.
   → verify: `.venv/bin/python -m pytest tests/ -q -k format_osc_log`
2. Hide the logs window at boot, add the "Show" > "Logs" menubar entry with the show callback.
   → verify: `.venv/bin/python -m pytest tests/ -q -k logs_window`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: at boot the logs window is closed; "Show" > "Logs" opens it; during playback the
   newest OSC line appears at the top and older lines push down.
