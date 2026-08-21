# e02s01 — Larger centered color square in ColorV/ColorR step cells

**type:** feature
**risk:** P2
**context:** ui/sequencer

**Context:** The step cell is a 90x90 child window containing a top row (active checkbox +
step-type label + right-click menu) and, for ColorV/ColorR, a color widget. A plain
`color_edit` cannot draw a large square: DPG ignores its `height` (ImGui draws the rect at
frame height), so the old 70x25 widget always looked ~20px tall. The fix uses
`add_color_button` (ImGui::ColorButton with explicit width×height) for both: a square swatch
sized by `STEP_COLOR_SQUARE_SIZE` (40px) horizontally centered in the cell, with the ColorV
button opening a left-click popup containing an RGB `color_picker` (callback still
`update_step_val`). Colors are stored normalized 0..1 and must still reach DPG on the 0..255
scale (BUG-2026-08-21T222712) via `dpg_color_rgba`.

## Requirements

#### ENHANCED: ColorV/ColorR step square is larger and centered
**Before:** 70x25 `color_edit` (rendered ~20px tall), left-aligned under the checkbox row.
**After:** a `STEP_COLOR_SQUARE_SIZE`x`STEP_COLOR_SQUARE_SIZE` (40px) `color_button` swatch
horizontally centered inside the 90x90 cell (indent computed from the constants,
`(90-2*8-40)/2 = 17`, measured on real DPG 2.3.1 to land dead-center), with a small top
spacer to keep vertical balance; ColorV opens the picker
in a left-click popup, ColorR stays read-only; right-click step menu unchanged.

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
