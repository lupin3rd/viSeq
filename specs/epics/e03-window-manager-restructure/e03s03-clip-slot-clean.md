# e03s03 — Clip slot: centered bare assign button, no table frame

**type:** feature
**risk:** P2
**context:** ui/sequencer

**Context:** The rightmost column of each sequencer row is a bordered 135x90 child window
(`seq_slot_{row}`) holding the clip assign button (110x70) in a `group(indent=4)` with a
3px top spacer — so it looks like a framed table cell and the button hugs the top-left.
The user wants only the assign button (thumbnail / waiting / "ASSIGN CLIP"), centered in the
row, with no bordered frame around it.

## Requirements

#### ENHANCED: Clip slot shows a centered bare assign button
**Before:** bordered child window (border=True, default ChildBg) with the button at
indent 4 + spacer 3 — framed table-cell look, top-left aligned.
**After:** borderless child window with transparent ChildBg; the button is centered
horizontally (indent = (135 - 2*8 padding - 110)/2) and vertically (top spacer
(90-70)/2 = 10); assign interaction unchanged.

## Steps

1. Borderless + transparent slot: `border=False`, transparent ChildBg theme, shared
   slot/button size constants; center the button in `update_track_slot_ui`.
   → verify: `.venv/bin/python -m pytest tests/ -q -k slot`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: the clip slot shows only the button/thumbnail centered in the row, no frame;
   clicking still opens the clip assignment; with a clip assigned the thumbnail is centered.
