# e02s01 — Larger centered color square in ColorV/ColorR step cells

**type:** feature
**risk:** P2
**context:** ui/sequencer

**Context:** The step cell is a 90x90 child window containing a top row (active checkbox +
step-type label + right-click menu) and, for ColorV/ColorR, a `color_edit` square currently
70x25 and left-aligned. The user wants the square larger and centered in the step button, for
every OSC message that uses the square (ColorV and ColorR). Colors are stored normalized 0..1
and must still reach DPG on the 0..255 API scale (BUG-2026-08-21T222712) via `dpg_color_value`.

## Requirements

#### ENHANCED: ColorV/ColorR step square is larger and centered
**Before:** 70x25 `color_edit`, left-aligned under the checkbox row.
**After:** a square (≈62x62) horizontally centered inside the 90x90 cell (indent
`(90-62)/2 = 14`), with a small top spacer to keep vertical balance; ColorV still opens the
picker on click, ColorR stays read-only (`no_picker`); right-click step menu unchanged.

## Steps

1. Update the ColorV branch in the step-cell builder: bigger square, centered.
   → verify: `.venv/bin/python -m pytest tests/ -q -k colorv`
2. Update the ColorR branch the same way (same size/centering constants, shared helper).
   → verify: `.venv/bin/python -m pytest tests/ -q -k colorr`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: `.venv/bin/python viseq.py`, right-click a step → Color Value / Color Random.
   Confirm the square is visibly bigger and centered, ColorV picker still opens on click,
   and the ColorR square repaints with the random color during playback.
