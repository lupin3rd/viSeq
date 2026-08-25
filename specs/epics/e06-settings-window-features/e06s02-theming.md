# e06s02 — Simple theming: named palette + Scuro/Chiaro presets + 5 custom pickers + live apply + persistence

**type:** feat
**risk:** P1
**context:** ui/theming

**Context:** Every color in viseq is hard-coded (theme colors on the 8 item themes, global
defaults, explicit `color=` on text items, draw-item fills for spectrum bars/LEDs). The user
wants a simple theme picker: decide dark or light background, and the colors of texts and
lines. This story routes the chrome colors through a named palette with a "Scuro" default
that reproduces the current look exactly (first launch visually identical), a "Chiaro"
preset, and 5 custom color pickers (sfondo, pannelli, testo, linee, accento) whose derived
slots are computed deterministically. Apply is live (verified headless: `set_value` on
`add_theme_color` items, `configure_item(tag, color=...)` on text items and
`configure_item(tag, fill=...)` on draw items all update colors at runtime in DPG 2.3.1).
The chosen theme persists in `viseq_config.json` (shared with e06s01) and is applied at
boot.

## Requirements

#### ADDED: Named palette slots
A `PALETTE` dict defines 14 slots: `window_bg`, `panel_bg`, `border`, `border_active`,
`text`, `text_dim`, `text_bright`, `accent`, `accent_bg`, `badge_bg`, `warning`, `play_bg`,
`play_on_bg`, `spectrum`. `DEFAULT_PALETTE` ("Scuro") reproduces the current hard-coded
look; `LIGHT_PALETTE` ("Chiaro") uses a light background with dark text.

#### ADDED: Derived slots from five primaries
`derive_palette(primaries)` computes `border_active`, `text_dim`, `text_bright`, `accent_bg`,
`badge_bg`, `warning`, `play_bg`, `play_on_bg` and `spectrum` deterministically from the
five user-editable primaries (`window_bg`, `panel_bg`, `border`, `text`, `accent`); the
result is a complete 14-slot palette and the input is not mutated.

#### ADDED: Palette-driven global theme and item themes
A global theme (`theme_global`, built lazily and bound via `dpg.bind_theme` only for
non-Scuro presets — Scuro keeps the exact DPG dark defaults) drives WindowBg, ChildBg,
Border, Text, TextDisabled, FrameBg, Button, Header, TitleBg, PopupBg, Scrollbar and Table
colors from the palette. The item themes (selected/normal clip, compact table, cell
off/on/play-off/play-on, slot clear, media badge, step copied) read their colors from the
palette instead of literals (slot clear keeps its transparent bg; the copied-step highlight
keeps its fixed dark bg). Every palette-driven `add_theme_color` records its tag and slot so
`apply_palette()` can update it live.

#### ADDED: Palette-driven text and draw colors
Explicit `color=` text items (step type labels, status labels, band labels, tile title,
loading texts, monitor head) and the spectrum bars read from palette slots and record
(tag, slot) bindings; `apply_palette()` updates them live via `configure_item`. The monitor
player's stylized turntable/seek/alpha readout, the beat LEDs and the band-selection
overlays are functional indicators and keep their fixed colors in every theme.

#### ADDED: Live palette application
`apply_palette(palette)` pushes every recorded binding (theme colors via `set_value`, text
and draw items via `configure_item`) and stores the active palette in module state. It runs
at boot after the windows are built and on every user change.

#### ADDED: Settings "Tema" section
The Settings window gains a "Tema" section: a preset combo (Scuro / Chiaro / Personalizzato)
and 5 color edits (Sfondo, Pannelli, Testo, Linee, Accento). Changing the preset loads the
preset palette; editing a color switches to "Personalizzato" and derives the rest; every
change applies live and persists to the config (`theme: {preset, colors}`).

## Steps

1. Add the palette module constants (`PALETTE_SLOTS`, `DEFAULT_PALETTE`, `LIGHT_PALETTE`)
   and `derive_palette()`; unit-test the derivation (deterministic, complete slot set,
   scuro != chiaro on the key slots, input not mutated).
   → verify: `.venv/bin/python -m pytest tests/ -q -k palette`
2. Add `apply_palette()` with the recorded-binding registry; unit-test it against the stub
   (set_value/configure_item called per recorded binding, active palette updated).
   → verify: `.venv/bin/python -m pytest tests/ -q -k apply_palette`
3. Build `theme_global` from the palette (lazily, non-Scuro only) and bind it; convert the
   item themes' `add_theme_color` literals to palette reads via the recording `theme_color`
   helper.
   → verify: `.venv/bin/python -m pytest tests/ -q -k "theme or import_time"`
4. Convert explicit text `color=` literals and the spectrum-bar colors to palette reads
   (`themed_text` / `themed_draw_rectangle` recording helpers).
   → verify: `.venv/bin/python -m pytest tests/ -q -k "themed or import_time"`
5. Add the Settings "Tema" section: preset combo + 5 color edits with live-apply callbacks
   (`on_theme_preset`, `on_theme_color`), persistence via `save_config`, boot apply via
   `apply_boot_config`.
   → verify: `.venv/bin/python -m pytest tests/ -q -k "tema or theme"`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: first launch looks identical to today (Scuro default).
3. Real rig: Settings → Tema → "Chiaro" → background turns light, text dark, borders gray;
   pick a custom Accento → step-cell borders/highlights change live.
4. Real rig: restart → the chosen theme is applied at boot.
5. Real rig: "Scuro" restores today's exact look.

## Out of scope

- Re-theming functional indicator colors (VU meter, band-selection overlays, beat LEDs,
  monitor turntable readout, copied-step flash).
- A full theme editor (swatch previews, undo, palette import/export).

## Risks

- Converting the explicit `color=` literals and item-theme colors is mechanical but touches
  many lines: the full regression suite (110 tests + new ones) is the safety net, and the
  Scuro preset reproduces the current tuples exactly (verified by test).
- The global theme is only bound for non-Scuro themes, so the legacy look (DPG dark
  defaults + item themes) is preserved byte-for-byte on first launch.
