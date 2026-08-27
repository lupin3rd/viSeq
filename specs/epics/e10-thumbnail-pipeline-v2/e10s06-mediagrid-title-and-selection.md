# specs/epics/e10-thumbnail-pipeline-v2/e10s06-mediagrid-title-and-selection.md

# e10s06 — Mediagrid: two-line title truncation + viseq-side primary selection

**type:** feat
**risk:** P2
**context:** thumbnail-pipeline

**Context:** Two Mediagrid UX gaps reported by the user on the real rig. (1)
Long media names wrap past the tile's two visible lines and get cut off by the
tile edge — names must fit on at most two lines, truncated at the end of the
second. (2) Selection currently exists only on the Vimix side: the tile of
Vimix's `current_source` is highlighted green, and the sequencer/monitor
attachment reads that selection. The user wants a **viseq-side primary
selection**: clicking a tile in the Mediagrid selects the media that
sequencer/monitor attachment uses; the Vimix current source remains visible as
a secondary indicator in a lighter color (not green), so the two states never
look alike.

## Requirements

#### ADDED: Two-line title with ellipsis truncation
The Mediagrid title text shows the media name on at most
`MEDIA_TITLE_MAX_LINES` (2) lines. A name whose measured default-font width
exceeds two wrap widths is truncated to the longest prefix that fits, with a
trailing ellipsis ("…"). The full name stays in the raw table; identity
(`target_id`) is unaffected.

#### ADDED: viseq-side primary selection
A new module state `viseq_selected_source` (target_id) is set by clicking a
Mediagrid tile (`add_clicked_handler` on the tile). The tile themes follow a
fixed precedence: **viseq selection → `theme_selected_clip` (green)**,
**Vimix current source only → `theme_vimix_current_clip` (lighter, not
green)**, otherwise plain. A stale selection (source pruned by L-1) is cleared.

#### MODIFIED: Sequencer/monitor attachment uses the viseq selection
`get_current_target_id()` returns `viseq_selected_source` when set, else the
Vimix current source (fallback keeps today's behavior before the first click).
`midi_action_track_assign` and `assign_monitor_player` inherit the change via
that function — no OSC contract change (the `/vimix/<name>` address format is
unchanged).

## Steps

1. RED: title truncation tests — short names untouched; long names truncated
   to the two-line budget with a trailing ellipsis (stub `get_text_size`
   measures width deterministically) → verify: `.venv/bin/python -m pytest tests/ -q -k title`
2. GREEN: `truncate_media_title()` (binary-search the longest fitting prefix)
   + use it in the Mediagrid structural title update →
   verify: `.venv/bin/python -m pytest tests/ -q -k title`
3. RED: selection tests — tile click sets `viseq_selected_source` and
   re-binds themes; theme precedence (viseq green > vimix light > plain);
   `get_current_target_id()` prefers the viseq selection; track assign uses
   it; L-1 prune clears a stale selection →
   verify: `.venv/bin/python -m pytest tests/ -q -k "select or current_target or track_assign"`
4. GREEN: `viseq_selected_source` state, `theme_vimix_current_clip`,
   `on_media_tile_click` + `refresh_tile_selection_themes`,
   `_tile_theme_for()` shared picker, prune clear, `get_current_target_id()`
   preference, `midi_action_track_assign` refactor →
   verify: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`
5. Full-suite verification → verify: `.venv/bin/python -m pytest tests/ -q`

## Verification Script (Step-by-Step)

1. Load a media whose name is longer than two tile lines: the Mediagrid title
   shows two lines with a trailing ellipsis; the raw table keeps the full name.
2. Click a tile in the Mediagrid: it turns green (current selection color).
3. In Vimix select a different media: its tile shows the lighter
   (non-green) indicator; the viseq-selected tile stays green.
4. Assign the current selection to a sequencer row and to a Monitor Player:
   both attach the **viseq-selected** media (not the Vimix current one).
5. Remove the viseq-selected source in Vimix: the green selection clears and
   attachment falls back to the Vimix current source.

## Out of scope

- Sending the viseq selection to Vimix (no OSC change; Vimix keeps its own
  current-source state).
- Title truncation anywhere outside the Mediagrid tile.
- Changing the frozen OSC contract or the sequencer/monitor addresses.

## Risks

- The two-line budget relies on the default font measurement; if a custom
  default font is added later the budget is re-derived automatically because
  `get_text_size` measures the live font.
- **DPG 2.3.1 handler constraint (verified on the real rig):** the deprecated
  `add_clicked_handler` shim crashes the grid build, and child windows cannot
  host a clicked handler at all (bind raises "inapplicable handler"). The
  implementation therefore creates one `item_handler_registry` per tile with a
  single left-click handler bound to the tile's clickable children (title,
  thumbnail, badge, alpha) — the demo-proven DPG 2.x pattern; right-click
  keeps the regen popup and never selects.
