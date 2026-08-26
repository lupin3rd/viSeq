# e08s01 — Help menubar entry + centered About window (ASCII logo, GPL-3.0, author)

**type:** feat
**risk:** P2
**context:** ui/windowing

**Context:** viseq has no About/help surface. The user wants a "Help" entry in the top
menubar that opens, always centered on the viewport, a window with the viseq info: the
supplied ASCII-art logo, the license (GPL-3.0) and the author (Luca Franceschini aka
Lupin3rd). This follows the proven e03 pattern (hidden boot window `show=False`, closable
with X, opened by a menubar callback). The window is a transient dialog — it stays out of
`LAYOUT_WINDOW_TAGS`, so a saved layout never re-opens it at boot. The ASCII logo needs a
monospace font to align: load DejaVu Sans Mono defensively (`os.path.exists` over common
Linux paths) and fall back to the default font when absent; headless tests never depend on
font presence.

## Requirements

#### ADDED: ASCII logo constant
A module constant `HELP_ASCII_LOGO` holds the user-supplied art verbatim (raw
triple-quoted string, 8 lines × 53 chars, trailing spaces kept) — the single source of
truth for the About window content.

#### ADDED: Pure centering helper
`centered_window_pos(viewport_w, viewport_h, window_w, window_h) -> tuple[int, int]`
returns the top-left position that centers a window of the given size on a viewport of the
given size: `((vp_w - w) // 2, (vp_h - h) // 2)`, each axis clamped at >= 0 (window larger
than the viewport never goes off-screen negative). Pure math, unit-tested without dpg.

#### ADDED: Hidden About window + Help menubar entry
A boot-time window (tag `help_window`, label "Help", `show=False`, closable with X, not in
`LAYOUT_WINDOW_TAGS`) whose content is: the ASCII logo in a monospace font (DejaVu Sans
Mono via `os.path.exists` guard over `/usr/share/fonts/truetype/dejavu/...`,
`/usr/share/fonts/dejavu/...`, `/usr/share/fonts/TTF/...`; default font fallback), a
separator, the app title, the license line ("Licenza: GPL-3.0") and the author line
("Creato da: Luca Franceschini aka Lupin3rd") using the palette-themed text helper. A
"Help" menubar entry (next to "Settings") calls `show_help_window()`.

#### ADDED: Centered-open callback
`show_help_window()` reads the live viewport and window sizes, computes the centered
position via `centered_window_pos`, applies it with `set_item_pos` and shows the window
with `show_item` — the window always re-centers on every open.

## Steps

1. Add `HELP_ASCII_LOGO` + `centered_window_pos()` (pure) → verify:
   `.venv/bin/python -m pytest tests/ -q -k 'centered_window_pos or help_logo'`
2. Build the hidden `help_window` (monospace font guard + themed content) + `show_help_window`
   callback + "Help" menubar entry → verify:
   `.venv/bin/python -m pytest tests/ -q -k help_window`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green (123 existing + new).
2. Real rig: at boot no Help window is visible; the menubar shows "Help" after "Settings";
   clicking it opens the About window centered on the screen; the ASCII logo is aligned
   (monospace), the license and author lines are readable; closing with X works; reopening
   re-centers even after the window was dragged away.
3. Preflight green: `.venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/python -m pytest tests/ -q`

## Out of scope

- Version number / changelog in the About window (deferred; needs a maintained version constant).
- Layout save/restore integration (help_window is a transient dialog; LAYOUT_WINDOW_TAGS untouched).
- Localization of the menubar (Help stays English, consistent with Monitor/Show/Settings).
- Modal / always-on-top behavior.

## Risks

- Monospace font path differs across distros → `os.path.exists` guard over 3 common paths; worst case the logo renders in the proportional default font (cosmetic only).
- Centering math off-by-one → pure helper with boundary tests (larger-than-viewport window, exact fit, odd/even sizes).
