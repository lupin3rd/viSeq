# e01s02 — Sequencer + fade engine on 2.x

**type:** refactor
**risk:** P0
**context:** domain

**Context:** The sequencer (8x8 grid, 5 step types, async fade state machine, play/stop/
resync/nudge, beat LED) is the app's core. The spike proved all its dpg primitives exist in
2.3.1 (table, child_window, popup, drag_float/int, color_edit, checkbox, drawlist/draw_circle,
bind_item_theme, delete_item(children_only=True), set/get_value). This story re-verifies every
sequencer path on 2.x through the headless harness, confirms the audit HIGH-1 (all UI via the
main-thread task queue) and HIGH-2 (fade-cancel invariant) fixes survive, and exercises the
step-type dispatch end-to-end.

## Requirements

#### MODIFIED: Sequencer step engine runs on dpg 2.x
**Before:** sequencer UI built and updated with dpg 1.x calls on Python <= 3.11.
**After:** identical behavior on dpg 2.3.1 (dearpygui.dearpygui), same step types
(AlphaV/AlphaR/AlphaF/ColorV/ColorR), same async fade semantics, no direct dpg calls from
worker threads (HIGH-1), fades cancelled when a non-fade step fires (HIGH-2).

## Steps

1. Confirm `sequencer_tick` and its helpers (update_step_theme/_apply_step_theme, flash_beat_led, enqueue_set_value) contain no direct dpg calls from worker threads → verify: `python3 -c "import re; src=open('viseq.py').read(); fns=['sequencer_tick','visual_metronome_loop','fade_tick_loop']; assert not [f for f in fns if re.search(r'def '+f+r'\(.*?(?=\ndef |\n# ===)', src, re.S) and re.search(r'dpg\.\w+', re.search(r'def '+f+r'\(.*?(?=\ndef |\n# ===)', src, re.S).group(0))], 'direct dpg call in worker fn'; print('OK')"`
2. Re-verify the HIGH-2 fade-cancel invariant on 2.x-era code (fade cancelled when a non-AlphaF active step fires; uninterrupted fade completes) → verify: `python3 tests/test_fixes.py 2>&1 | grep -c "HIGH-2" | grep -q 3` (3 HIGH-2 checks pass)
3. Re-verify step-type dispatch and play/stop/resync/nudge paths through the harness (AlphaR last-value display, ColorR normalized color, beat LED flash) → verify: `python3 tests/test_fixes.py`
4. Confirm the remaining dpg primitives used by the sequencer UI exist in 2.3.1 (popup context form, drag_int %ds, color_edit no_alpha, delete_item children_only) via a one-shot probe in the venv → verify: `.venv/bin/python -c "import dearpygui.dearpygui as dpg; dpg.create_context(); assert hasattr(dpg,'popup') and hasattr(dpg,'add_drag_int') and hasattr(dpg,'add_color_edit') and hasattr(dpg,'delete_item'); with dpg.window(tag='w'): pass; dpg.delete_item('w', children_only=True); dpg.destroy_context(); print('probe OK')"`

## Verification Script (Step-by-Step)

1. Run `python3 tests/test_fixes.py` — all HIGH-2 checks (fade running mid-sequence, non-fade step cancels, last alpha value, uninterrupted fade completes) pass.
2. Static check: no `dpg.` calls inside `sequencer_tick`/`visual_metronome_loop`/`fade_tick_loop` (step 1 command exits 0).
3. Venv probe (step 4) exits 0 — the sequencer's dpg primitives all exist in 2.3.1.

## Out of scope

- New step types, grid size changes, UI redesign (deferred).
- Beat-sync improvements beyond current behavior.

## Risks

- Fade timing drift under the 60 fps main-loop cap (e01s01 L-5) — the fade loop has its own 100 Hz thread, so risk is low; watch fade smoothness at user acceptance.
- color_edit `get_value` returns alpha scaled to 255.0 in 2.x — the app never reads alpha, so no impact; noted in the spike.
