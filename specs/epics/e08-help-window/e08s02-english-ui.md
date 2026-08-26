# e08s02 — Version line in the About window (APP_VERSION) + full English UI labels pass

**type:** feat
**risk:** P2
**context:** ui/localization

**Context:** The About window (e08s01) shows logo, license and author but no version, and
the app still carries Italian UI strings ("Rilevazione BPM", "Salva layout", "Scuro",
"Finestre", ...). The user wants (1) the version in the About window and (2) the whole UI
in English. The version comes from a new `APP_VERSION` constant (single source of truth,
value "1.1.0" matching `specs/release-plan.yaml`). The English pass translates every
user-facing label; internal identifiers, tags and config keys (`scuro`/`chiaro`/`custom`)
stay untouched, so persisted configs remain valid.

## Requirements

#### ADDED: Version constant + About-window version line
`APP_VERSION: str = "1.1.0"` at module scope. The About window shows
`Version: <APP_VERSION>` (themed text, before the license line). The constant is the only
place the version lives in code.

#### MODIFIED: Theme preset labels are English
**Before:** "Scuro", "Chiaro", "Personalizzato" (combo items in Settings > Tema).
**After:** "Dark", "Light", "Custom". The label→key mapping in `on_theme_preset` still
uses the constants, so behavior is unchanged.

#### MODIFIED: Beat-source labels are English
**Before:** "Rilevazione BPM", "Battito Band 1/2/3", "BPM Manuale".
**After:** "BPM Detection", "Beat Band 1/2/3", "Manual BPM". The two sequencer checkboxes
(analysis, manual) read from `BEAT_SOURCE_LABELS` so there is a single source of truth.

#### MODIFIED: Settings window sections/buttons/checkbox are English
**Before:** "Finestre", "Salva layout", "Ripristina layout", "Ripristina all'avvio", "Tema".
**After:** "Windows", "Save layout", "Restore layout", "Restore at startup", "Theme".

#### MODIFIED: About-window descriptive lines are English
**Before:** "viseq — Audio-Reactive VJ Controller per Vimix", "Licenza: GPL-3.0",
"Creato da: Luca Franceschini aka Lupin3rd".
**After:** "viseq — Audio-Reactive VJ Controller for Vimix", "License: GPL-3.0",
"Created by: Luca Franceschini aka Lupin3rd", plus the new "Version: <APP_VERSION>".

## Steps

1. Add `APP_VERSION` + the "Version:" line in the About window → verify:
   `.venv/bin/python -m pytest tests/ -q -k 'version or help_window'`
2. English pass on every label (dicts + literals + Settings sections + About lines),
   update the tests that assert Italian labels, add a regression test that no Italian UI
   string remains → verify:
   `.venv/bin/python -m pytest tests/ -q -k 'english or settings or theme or help'`
   then the full suite.

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: open Help → the window shows the ASCII logo, "Version: 1.1.0", "License:
   GPL-3.0", "Created by: Luca Franceschini aka Lupin3rd". The sequencer shows "BPM
   Detection"/"Manual BPM"/"Beat Band 1..3"; Settings shows "Windows"/"Theme" sections
   with "Save layout"/"Restore layout"/"Restore at startup" and Dark/Light/Custom presets.
3. Preflight green: `.venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/python -m pytest tests/ -q`

## Out of scope

- i18n/locale system — direct label translation only.
- Renaming tags, config keys or the scuro/chiaro/custom preset keys (persisted configs must stay valid).
- Changelog or per-feature versioning — just the single version line.

## Risks

- A persisted config written before this change references keys, not labels (preset keys
  stay `scuro`/`chiaro`/`custom`), so no migration is needed — verified by the existing
  config round-trip tests.
- Missing an Italian string → the regression test greps the known Italian terms over all
  user-facing label sources.
