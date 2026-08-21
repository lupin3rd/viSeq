---
bug_id: BUG-2026-08-21T222712
status: open
severity: medium
scope: sequencer-ui
title: ColorR step square never shows the random color (always black)
---

# BUG-2026-08-21T222712: ColorR step square never shows the random color

## Problem

- **What happens (actual):** On the real rig, a step set to "Color Random"
  (ColorR) sends the random color over OSC correctly, but the little color
  square inside the step cell stays black — it never shows the generated
  color. The same happens when a ColorV step is re-rendered: its stored color
  is not reflected.
- **What should happen (expected):** The step square must repaint with the
  random color every time the step fires, and re-rendered ColorR/ColorV cells
  must open showing the stored color.
- **How to reproduce:** Set a step to "Color Random", start the sequencer,
  watch the cell: OSC log shows `OUT /vimix/.../color [0.xx, 0.xx, 0.xx]`
  every pass, but the square remains black.

`Security impact: NONE` — local GUI display only, no I/O or data exposure.

## Root Cause Analysis

The step square is a DearPyGui `color_edit` widget. Two distinct code paths
feed it a color:

1. **Interactive path (works):** the user picks a color in a ColorV square;
   ImGui writes normalized 0..1 floats straight into the widget's internal
   value, and the callback receives normalized 0..1 floats. viseq stores and
   re-sends exactly those floats.
2. **Programmatic path (broken):** viseq pushes a color with
   `set_value(tag, [r, g, b])` (ColorR tick) and with
   `add_color_edit(default_value=[r, g, b])` (cell re-render, both ColorR and
   ColorV). DearPyGui 2.3.1's `ToColor()` — the parser used by both
   `set_value` and `default_value` — **divides every channel by 255.0f**,
   i.e. it expects colors in the 0..255 API scale. viseq passes normalized
   0..1 floats, so a random 0.42 is stored as `0.42/255 ≈ 0.0016` — visually
   indistinguishable from black. `1.0` becomes `0.0039`; the square can never
   look colored.

The two paths must therefore use different scales: normalized 0..1 on the
callback side, 0..255 on the `set_value`/`default_value` side.

- **Code path involved:** step-cell widget creation and the ColorR step
  sender; color values are stored normalized in the sequencer state.
- **Why the current code fails:** the state holds normalized floats, and the
  programmatic DPG color API expects 0..255, so the conversion at the widget
  boundary is missing.
- **Contributing factors:** the headless test harness stubs `dpg.set_value`
  and `add_color_edit` without modeling DPG's internal /255 scaling, so tests
  asserted the *pass-through* value instead of the *rendered* color — the
  bug shipped green. Verified against the real DPG 2.3.1 (`ToColor` in
  `mvPyUtils.cpp`; empirical probe: `set_value(tag, [0.42, 0.42, 0.42])`
  stores ≈0.0016, while `set_value(tag, [107.1, ...])` stores exactly 0.42).
- **Risk level:** Low — isolated to the widget boundary; the OSC contract is
  untouched (frozen).

## TDD Fix Plan

Introduce a conversion at the DPG boundary: normalized 0..1 RGB → DPG 0..255
scale, used by the ColorR tick sender and by the ColorR/ColorV cell creation.

1. **RED:** Update `test_colorr_square_shows_sent_color` to assert that
   `send_colorr_step` enqueues a `set_value` whose value is the color scaled
   to the DPG 0..255 API scale (e.g. random 0.42 → `[107.1, 107.1, 107.1]`),
   not the raw normalized floats.
   **GREEN:** Add the scale conversion helper and apply it in
   `send_colorr_step` before `enqueue_set_value`.
   **verify:** `.venv/bin/python -m pytest tests/test_fixes.py -q -k colorr`

2. **RED:** Update `test_med3_colorr_normalized_value` and
   `test_colorv_square_default_is_normalized` to assert that `update_step_ui`
   passes 0..255-scaled `default_value` for both ColorR and ColorV cells, so
   re-rendered cells show the stored color.
   **GREEN:** Apply the same conversion to the `default_value` of both
   `color_edit` branches in `update_step_ui`.
   **verify:** `.venv/bin/python -m pytest tests/ -q`

**REFACTOR:** Name the 255.0 scale as a named constant (L-6) and keep the
conversion helper single-source. No OSC payload changes (still normalized).

## Acceptance Criteria

- [ ] ColorR tick repaints the step square with the generated color on the
      real rig
- [ ] Re-rendered ColorR and ColorV cells open showing the stored color
- [ ] OSC payloads stay normalized 0..1 (frozen contract untouched)
- [ ] All new tests pass
- [ ] Existing tests still pass

## Resolution

**Fixed:** 2026-08-21
**Root cause confirmed:** DearPyGui 2.3.1's `ToColor()` — used by both `set_value` and `add_color_edit(default_value=...)` — divides every channel by 255, i.e. its color API is 0..255; viseq pushed normalized 0..1 floats, so programmatic colors rendered near-black. Verified in DPG source (`mvPyUtils.cpp`) and empirically: `set_value(tag, [0.42,...])` stores 0.0016, `set_value(tag, [107.1,...])` stores exactly 0.42.
**Fix applied:** New `DPG_COLOR_SCALE` constant + `dpg_color_value()` conversion helper applied at all three DPG color boundary sites: ColorV `default_value`, ColorR `default_value`, ColorR `set_value`. OSC payloads unchanged (still normalized 0..1).
**Hardening added:** single-source boundary conversion helper with docstring documenting the /255 contract; dedicated boundary test (`test_dpg_color_scale_boundaries`) plus updated contract tests asserting the 0..255 scale at both call sites.
**Evidence:** preflight green — `ruff check .` ✓, `mypy` ✓, `pytest tests/ -q` → 23 passed; real-DPG 2.3.1 probe confirms internal channel 0.42 after set_value and 0.5/0.25/0.75 default on re-render.
**Commit:** `fix(color): ColorR step square now repaints with the random color`
