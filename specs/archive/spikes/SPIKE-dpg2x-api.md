# Spike: dearpygui 2.x API delta for viseq

## Question

Can viseq's DearPyGui 1.x patterns (viewport flow, themes, tables, textures, callbacks,
popups, mutex) be ported to dearpygui 2.3.1 on Python 3.13 as a near-1:1 migration — and what
are the concrete API deltas?

## Result

**Answered.** The migration is viable as a near-1:1 port. The `dearpygui.dearpygui` namespace
— which is exactly what `viseq.py` imports (`import dearpygui.dearpygui as dpg`) — preserves
the 1.x API surface viseq relies on. One hard landmine found (`get_dearpygui_version()` before
`create_context()` segfaults), plus two removed aliases viseq does not use.

## Findings

1. **`dearpygui` 2.3.1 is a package, not a flat module.** Top-level `import dearpygui` exposes
   only `__version__` (2.3.1); the entire API lives in the nested `dearpygui.dearpygui`
   submodule (1,194 public members). viseq's existing import line already targets it.
2. **The full 1.x flow survives verbatim** on `dearpygui.dearpygui`:
   `create_context` → item creation → `create_viewport` → `setup_dearpygui` →
   `show_viewport` → `while is_dearpygui_running(): render_dearpygui_frame()` →
   `destroy_context`. All exist and work (verified with a real window on DISPLAY=:0.0).
3. **LANDMINE:** `dpg.get_dearpygui_version()` **segfaults the process if called before
   `create_context()`** (works after). viseq never calls it, but plan-work must note: no dpg
   API calls before `create_context()` (viseq already complies — its first dpg call is
   `create_context()`).
4. **Removed aliases (not used by viseq, do not add them in the port):** `add_popup` →
   use the context manager `with dpg.popup(...)` (works); `add_draw_circle` → `draw_circle`
   (viseq already uses `draw_circle` inside `with dpg.drawlist(...)`).
5. **All viseq patterns verified working in 2.3.1:** `texture_registry`/`add_static_texture`
   with float32 0..1 flattened arrays; `theme`/`theme_component(mvChildWindow)`/
   `add_theme_color`/`add_theme_style`; `bind_item_theme` on tables and child windows; table +
   `add_table_column(width_fixed=True, init_width_or_weight=90)` + `mvTable_SizingFixedFit`;
   `child_window`, `group(horizontal=True)`, `viewport_menu_bar`/`menu`/`menu_item`, `popup`,
   `drawlist`/`draw_circle` (beat LED), `checkbox`, `drag_float`, `drag_int(format="%ds")`,
   `color_edit(no_alpha=True)` (set_value/get_value work; get_value returns 4 components with
   alpha scaled to 255.0 — irrelevant for no_alpha usage), `progress_bar`, `combo`,
   `input_text`, `input_int(step=0)`, `image`/`image_button(texture_tag=...)`,
   `get_item_width`, `delete_item(children_only=True)` (verified: children deleted, parent
   kept), `does_item_exist`, `does_alias_exist`/`remove_alias`, `configure_item`,
   `set_item_label`, `set_value`/`get_value` (drag_float returns float32-rounded values, as in
   1.x), `mutex`, `mvThemeCol_Border`/`mvThemeCol_ChildBg`, `mvStyleVar_CellPadding`/
   `mvStyleVar_ChildRounding`, `mvMouseButton_Right`.
6. **Callback contract unchanged** per docstring: `user_data` still passed to callbacks;
   `add_button` doc shows the same (sender, app_data, user_data) convention. Real event
   firing (clicks, checkbox toggles) could not be verified headless — that is the user's
   manual smoke-test item.

## Evidence

- Version block: `get_dearpygui_version()` pre-context → **Segmentation fault, exit 139**;
  same call post-context → `2.3.1`. Minimal repro:
  `import dearpygui.dearpygui as dpg; dpg.get_dearpygui_version()` → segfault.
- Full exercise run (context → texture → theme → window/table subtree → bind → set_value →
  viewport → 3 rendered frames → destroy) exited 0 on Python 3.13.5 with dearpygui 2.3.1
  from a clean venv (`/tmp/dpg2-venv`), DISPLAY=:0.0.
- `delete_item('g', children_only=True)`: child `t1` gone, parent `g` kept.

## Implications for the plan

- **Effort estimate: LOW-MEDIUM, not a rewrite.** The port is mostly *verification* work:
  the API surface viseq touches is present with identical names. Expected real work:
  (a) replace the removed aliases if any slipped in (none in viseq today); (b) re-check any
  call site flagged by running the app; (c) re-run the headless regression harness against
  2.3.1 stubs (the harness stubs dpg anyway, so it only needs the stub's `__version__` story
  to stay honest); (d) requirements.txt bump.
- **`essentia==2.1b6.dev1389`** is the cp313 wheel (dev1438 is cp314-only) — confirmed via
  PyPI metadata in this spike's prep.
- **Python 3.13.5 works** for dpg 2.3.1 (cp313 wheel installed and ran).
- The audit HIGH-1 fix (all UI through the main-thread task queue) is *more* important on
  2.x if 2.x tightened thread-safety — plan-work should include a thread-safety check during
  the user smoke test (no crashes on rapid sequencer steps).

## What was NOT explored

- Real event firing / callback invocation (needs interaction; headless environment).
- Rendering fidelity of themes/tables (window appeared and rendered, but visual inspection
  is the user's job).
- `start_dearpygui()` blocking loop vs the manual render loop (manual loop verified; viseq
  uses the manual loop, so this is sufficient).
- Whether 2.x changed any *default* behavior that affects visuals (e.g., font scaling,
  theme defaults) — covered by the user's smoke test.

## Recommendation

**Proceed with the migration** (plan-release → plan-work). Treat it as a verification-heavy
port, not a rewrite. Plan-work must include: requirements.txt bump (dearpygui 2.3.1,
essentia 2.1b6.dev1389), a first run of viseq on 2.3.1 in the venv, the regression harness
pass, the LOW-item fold-ins (L-1..L-6 + type hints + EN comments), and the user's manual
acceptance checklist — with the segfault landmine and the no-API-before-context rule noted
for any new code.
