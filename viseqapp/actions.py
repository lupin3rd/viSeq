"""Mappable action catalog for the MIDI engine (e33s01).

Every action a MIDI binding can trigger is described by ONE ActionSpec here —
id, English label, category and kind — so "make a feature MIDI-mappable" is
an additive change (a spec + a dispatcher in the composition root), never a
scatter of constants and branches. Pure data: no dpg, no state (HIGH-1 — the
worker modules and tests import this freely). The ids remain the
MIDI_ACTION_* constants in ``constants.py`` (single source); this module keys
off them. ``midi_execute`` in the composition root owns the dispatcher map;
this module owns the metadata.
"""

from dataclasses import dataclass

from viseqapp.constants import (
    MIDI_ACTION_BEAT_SOURCE,
    MIDI_ACTION_DRAFT_LOAD,
    MIDI_ACTION_DRAFT_NEXT,
    MIDI_ACTION_DRAFT_PREV,
    MIDI_ACTION_DRAFT_SAVE,
    MIDI_ACTION_DRAFT_STEP,
    MIDI_ACTION_ENABLE_CORRECTION,
    MIDI_ACTION_FILE_MANAGER_TOGGLE,
    MIDI_ACTION_FILE_NEXT,
    MIDI_ACTION_FILE_PREV,
    MIDI_ACTION_FILE_STEP,
    MIDI_ACTION_IO_MONITOR_TOGGLE,
    MIDI_ACTION_MAPPER_BAND,
    MIDI_ACTION_MAPPER_CUE_OPEN,
    MIDI_ACTION_MAPPER_ENABLE,
    MIDI_ACTION_MAPPER_LINE,
    MIDI_ACTION_MAPPER_MAPPING,
    MIDI_ACTION_MAPPER_RESET,
    MIDI_ACTION_MAPPING_ADD,
    MIDI_ACTION_MAPPING_COPY,
    MIDI_ACTION_MAPPING_CUT,
    MIDI_ACTION_MAPPING_MOVE,
    MIDI_ACTION_MAPPING_PASTE,
    MIDI_ACTION_MAPPING_TOGGLE,
    MIDI_ACTION_MODE_TOGGLE,
    MIDI_ACTION_MODES_CLEAR,
    MIDI_ACTION_NUDGE_BACK,
    MIDI_ACTION_NUDGE_FORWARD,
    MIDI_ACTION_PAIRING_PROMPT,
    MIDI_ACTION_PROJECT_NEW,
    MIDI_ACTION_PROJECT_OPEN,
    MIDI_ACTION_PROJECT_SAVE,
    MIDI_ACTION_REGEN_SELECTED,
    MIDI_ACTION_SEQ_ROW_ASSIGN,
    MIDI_ACTION_SEQ_ROW_DISABLE,
    MIDI_ACTION_SEQ_ROW_ENABLE,
    MIDI_ACTION_SEQ_TOGGLE,
    MIDI_ACTION_SET_CURRENT,
    MIDI_ACTION_SHOW_WINDOW,
    MIDI_ACTION_SOURCE_NEXT,
    MIDI_ACTION_SOURCE_PREV,
    MIDI_ACTION_SOURCE_STEP,
    MIDI_ACTION_TEXT_CLEAR,
    MIDI_ACTION_TEXT_LINE,
    MIDI_ACTION_TEXT_LINE_PLUS,
    MIDI_ACTION_TEXT_SEND,
    MIDI_ACTION_TEXT_WINDOW_TOGGLE,
    MIDI_ACTION_TEXT_WORD,
    MIDI_ACTION_TEXT_WORD_PLUS,
    MIDI_ACTION_TRACK_ASSIGN,
    MIDI_ACTION_TRANSPORT_PLAY,
    MIDI_ACTION_TRANSPORT_RESYNC,
    MIDI_ACTION_TRANSPORT_TAP,
)

# Categories group the actions in future UI (MIDI window catalog etc.).
CATEGORY_TRANSPORT = "transport"
CATEGORY_SEQUENCER = "sequencer"
CATEGORY_MAPPER = "mapper"

# e33s04: Mediagrid actions (source browsing + tile-anchored context rows).
CATEGORY_MEDIAGRID = "mediagrid"

# e39s01: diagnostic window actions (I/O Monitor show/hide).
CATEGORY_MONITOR = "monitor"

# e40s01: Mapping actions (arm/disarm a Mapping's Enabled gate).
CATEGORY_MAPPING = "mapping"

# e52s01: MIDI mode actions (toggle ONE named mode / clear every mode).
CATEGORY_MODES = "modes"

# e42s02: link-pairing actions (re-open the pairing prompt).
CATEGORY_SETTINGS = "settings"

# e43s02: File Manager actions (browse machine A's media over the /fs plane).
CATEGORY_FILES = "files"

# e59s02: Text window actions (toggle the window, send the document).
CATEGORY_TEXT = "text"

# e33s05: global actions (window show + project new/open/save).
CATEGORY_GLOBAL = "global"

# Kinds describe how the incoming MIDI value maps onto the action.
KIND_MOMENTARY = "momentary"  # note edges trigger; CC fires at the >=64 threshold
KIND_VALUE = "value"  # the raw CC 0..127 value is consumed (rescaleped by the action)
KIND_STEP = "step"  # e52s04: the signed CC delta becomes detents (params[\"steps\"])


@dataclass(frozen=True)
class ActionSpec:
    """Metadata for one mappable action."""

    action_id: str
    label: str  # English, human-readable (bindings list, learn status)
    category: str
    kind: str


ACTION_SPECS: dict[str, ActionSpec] = {
    MIDI_ACTION_SEQ_TOGGLE: ActionSpec(
        MIDI_ACTION_SEQ_TOGGLE, "Toggle step", CATEGORY_SEQUENCER, KIND_MOMENTARY
    ),
    MIDI_ACTION_TRANSPORT_PLAY: ActionSpec(
        MIDI_ACTION_TRANSPORT_PLAY, "Play", CATEGORY_TRANSPORT, KIND_MOMENTARY
    ),
    MIDI_ACTION_TRANSPORT_RESYNC: ActionSpec(
        MIDI_ACTION_TRANSPORT_RESYNC, "Resync playhead", CATEGORY_TRANSPORT, KIND_MOMENTARY
    ),
    MIDI_ACTION_TRANSPORT_TAP: ActionSpec(
        MIDI_ACTION_TRANSPORT_TAP, "Tap tempo", CATEGORY_TRANSPORT, KIND_MOMENTARY
    ),
    MIDI_ACTION_NUDGE_BACK: ActionSpec(
        MIDI_ACTION_NUDGE_BACK, "Nudge back", CATEGORY_TRANSPORT, KIND_MOMENTARY
    ),
    MIDI_ACTION_NUDGE_FORWARD: ActionSpec(
        MIDI_ACTION_NUDGE_FORWARD, "Nudge forward", CATEGORY_TRANSPORT, KIND_MOMENTARY
    ),
    MIDI_ACTION_BEAT_SOURCE: ActionSpec(
        MIDI_ACTION_BEAT_SOURCE, "Beat source", CATEGORY_TRANSPORT, KIND_MOMENTARY
    ),
    MIDI_ACTION_TRACK_ASSIGN: ActionSpec(
        MIDI_ACTION_TRACK_ASSIGN, "Assign media to track", CATEGORY_SEQUENCER, KIND_MOMENTARY
    ),
    MIDI_ACTION_MAPPER_MAPPING: ActionSpec(
        MIDI_ACTION_MAPPER_MAPPING, "Mapper value", CATEGORY_MAPPER, KIND_VALUE
    ),
    MIDI_ACTION_MAPPER_ENABLE: ActionSpec(
        MIDI_ACTION_MAPPER_ENABLE, "Enable mapping", CATEGORY_MAPPER, KIND_MOMENTARY
    ),
    MIDI_ACTION_MAPPER_RESET: ActionSpec(
        MIDI_ACTION_MAPPER_RESET, "Reset mapping", CATEGORY_MAPPER, KIND_MOMENTARY
    ),
    MIDI_ACTION_MAPPER_LINE: ActionSpec(
        MIDI_ACTION_MAPPER_LINE,
        "Assign selected media to mapper line",
        CATEGORY_MAPPER,
        KIND_MOMENTARY,
    ),
    MIDI_ACTION_MAPPER_BAND: ActionSpec(
        MIDI_ACTION_MAPPER_BAND, "Audio band source", CATEGORY_MAPPER, KIND_MOMENTARY
    ),
    MIDI_ACTION_MAPPER_CUE_OPEN: ActionSpec(
        MIDI_ACTION_MAPPER_CUE_OPEN, "Open cue list window", CATEGORY_MAPPER, KIND_MOMENTARY
    ),
    # e51s02: the Mapper clipboard + in-line moves (e33 MIDI-mappable rule).
    MIDI_ACTION_MAPPING_COPY: ActionSpec(
        MIDI_ACTION_MAPPING_COPY, "Copy mapping", CATEGORY_MAPPER, KIND_MOMENTARY
    ),
    MIDI_ACTION_MAPPING_CUT: ActionSpec(
        MIDI_ACTION_MAPPING_CUT, "Cut mapping", CATEGORY_MAPPER, KIND_MOMENTARY
    ),
    MIDI_ACTION_MAPPING_MOVE: ActionSpec(
        MIDI_ACTION_MAPPING_MOVE, "Move mapping", CATEGORY_MAPPER, KIND_MOMENTARY
    ),
    MIDI_ACTION_MAPPING_PASTE: ActionSpec(
        MIDI_ACTION_MAPPING_PASTE, "Paste mapping", CATEGORY_MAPPER, KIND_MOMENTARY
    ),
    # e52s01: MIDI modes — {"mode": name} on MODE_TOGGLE.
    MIDI_ACTION_MODE_TOGGLE: ActionSpec(
        MIDI_ACTION_MODE_TOGGLE, "Toggle mode", CATEGORY_MODES, KIND_MOMENTARY
    ),
    MIDI_ACTION_MODES_CLEAR: ActionSpec(
        MIDI_ACTION_MODES_CLEAR, "All modes off", CATEGORY_MODES, KIND_MOMENTARY
    ),
    MIDI_ACTION_SOURCE_NEXT: ActionSpec(
        MIDI_ACTION_SOURCE_NEXT, "Next source", CATEGORY_MEDIAGRID, KIND_MOMENTARY
    ),
    MIDI_ACTION_SOURCE_PREV: ActionSpec(
        MIDI_ACTION_SOURCE_PREV, "Previous source", CATEGORY_MEDIAGRID, KIND_MOMENTARY
    ),
    # e52s04: a knob browses the selection by rotation detents.
    MIDI_ACTION_SOURCE_STEP: ActionSpec(
        MIDI_ACTION_SOURCE_STEP, "Step source", CATEGORY_MEDIAGRID, KIND_STEP
    ),
    MIDI_ACTION_REGEN_SELECTED: ActionSpec(
        MIDI_ACTION_REGEN_SELECTED,
        "Regenerate selected source thumbnails",
        CATEGORY_MEDIAGRID,
        KIND_MOMENTARY,
    ),
    MIDI_ACTION_SEQ_ROW_ASSIGN: ActionSpec(
        MIDI_ACTION_SEQ_ROW_ASSIGN,
        "Assign selected source to sequencer line",
        CATEGORY_SEQUENCER,
        KIND_MOMENTARY,
    ),
    MIDI_ACTION_SEQ_ROW_ENABLE: ActionSpec(
        MIDI_ACTION_SEQ_ROW_ENABLE,
        "Enable sequencer line",
        CATEGORY_SEQUENCER,
        KIND_MOMENTARY,
    ),
    MIDI_ACTION_SEQ_ROW_DISABLE: ActionSpec(
        MIDI_ACTION_SEQ_ROW_DISABLE,
        "Disable sequencer line",
        CATEGORY_SEQUENCER,
        KIND_MOMENTARY,
    ),
    MIDI_ACTION_ENABLE_CORRECTION: ActionSpec(
        MIDI_ACTION_ENABLE_CORRECTION,
        "Enable color correction",
        CATEGORY_MEDIAGRID,
        KIND_MOMENTARY,
    ),
    MIDI_ACTION_SET_CURRENT: ActionSpec(
        MIDI_ACTION_SET_CURRENT,
        "Set current source",
        CATEGORY_MEDIAGRID,
        KIND_MOMENTARY,
    ),
    MIDI_ACTION_IO_MONITOR_TOGGLE: ActionSpec(
        MIDI_ACTION_IO_MONITOR_TOGGLE, "I/O Monitor window", CATEGORY_MONITOR, KIND_MOMENTARY
    ),
    MIDI_ACTION_MAPPING_TOGGLE: ActionSpec(
        MIDI_ACTION_MAPPING_TOGGLE, "Arm/disarm Mapping", CATEGORY_MAPPING, KIND_MOMENTARY
    ),
    MIDI_ACTION_MAPPING_ADD: ActionSpec(
        MIDI_ACTION_MAPPING_ADD, "Add Mapping to line", CATEGORY_MAPPING, KIND_MOMENTARY
    ),
    MIDI_ACTION_PAIRING_PROMPT: ActionSpec(
        MIDI_ACTION_PAIRING_PROMPT, "Pair with viOSC", CATEGORY_SETTINGS, KIND_MOMENTARY
    ),
    MIDI_ACTION_FILE_MANAGER_TOGGLE: ActionSpec(
        MIDI_ACTION_FILE_MANAGER_TOGGLE, "File Manager window", CATEGORY_FILES, KIND_MOMENTARY
    ),
    MIDI_ACTION_DRAFT_LOAD: ActionSpec(
        MIDI_ACTION_DRAFT_LOAD, "Send draft to Vimix", CATEGORY_FILES, KIND_MOMENTARY
    ),
    MIDI_ACTION_DRAFT_SAVE: ActionSpec(
        MIDI_ACTION_DRAFT_SAVE, "Save draft", CATEGORY_FILES, KIND_MOMENTARY
    ),
    MIDI_ACTION_DRAFT_NEXT: ActionSpec(
        MIDI_ACTION_DRAFT_NEXT, "Next draft", CATEGORY_FILES, KIND_MOMENTARY
    ),
    MIDI_ACTION_DRAFT_PREV: ActionSpec(
        MIDI_ACTION_DRAFT_PREV, "Previous draft", CATEGORY_FILES, KIND_MOMENTARY
    ),
    # e54s02: rotary stepping targets (a step action fires its pair by rotation).
    MIDI_ACTION_DRAFT_STEP: ActionSpec(
        MIDI_ACTION_DRAFT_STEP, "Step draft", CATEGORY_FILES, KIND_STEP
    ),
    MIDI_ACTION_FILE_STEP: ActionSpec(
        MIDI_ACTION_FILE_STEP, "Step file", CATEGORY_FILES, KIND_STEP
    ),
    MIDI_ACTION_FILE_NEXT: ActionSpec(
        MIDI_ACTION_FILE_NEXT, "Next file", CATEGORY_FILES, KIND_MOMENTARY
    ),
    MIDI_ACTION_FILE_PREV: ActionSpec(
        MIDI_ACTION_FILE_PREV, "Previous file", CATEGORY_FILES, KIND_MOMENTARY
    ),
    # e59s02: the Text window (e33 rule) — toggle the window, send the document.
    MIDI_ACTION_TEXT_WINDOW_TOGGLE: ActionSpec(
        MIDI_ACTION_TEXT_WINDOW_TOGGLE, "Text window", CATEGORY_TEXT, KIND_MOMENTARY
    ),
    MIDI_ACTION_TEXT_SEND: ActionSpec(
        MIDI_ACTION_TEXT_SEND, "Send text to source", CATEGORY_TEXT, KIND_MOMENTARY
    ),
    MIDI_ACTION_TEXT_CLEAR: ActionSpec(
        MIDI_ACTION_TEXT_CLEAR, "Clear text source", CATEGORY_TEXT, KIND_MOMENTARY
    ),
    MIDI_ACTION_TEXT_WORD: ActionSpec(
        MIDI_ACTION_TEXT_WORD, "Text: 1 word", CATEGORY_TEXT, KIND_MOMENTARY
    ),
    MIDI_ACTION_TEXT_WORD_PLUS: ActionSpec(
        MIDI_ACTION_TEXT_WORD_PLUS, "Text: 1 word +", CATEGORY_TEXT, KIND_MOMENTARY
    ),
    MIDI_ACTION_TEXT_LINE: ActionSpec(
        MIDI_ACTION_TEXT_LINE, "Text: 1 line", CATEGORY_TEXT, KIND_MOMENTARY
    ),
    MIDI_ACTION_TEXT_LINE_PLUS: ActionSpec(
        MIDI_ACTION_TEXT_LINE_PLUS, "Text: 1 line +", CATEGORY_TEXT, KIND_MOMENTARY
    ),
    # e33s05: the global actions (window show + project new/open/save).
    MIDI_ACTION_SHOW_WINDOW: ActionSpec(
        MIDI_ACTION_SHOW_WINDOW, "Open window", CATEGORY_GLOBAL, KIND_MOMENTARY
    ),
    MIDI_ACTION_PROJECT_NEW: ActionSpec(
        MIDI_ACTION_PROJECT_NEW, "New project", CATEGORY_GLOBAL, KIND_MOMENTARY
    ),
    MIDI_ACTION_PROJECT_OPEN: ActionSpec(
        MIDI_ACTION_PROJECT_OPEN, "Open project", CATEGORY_GLOBAL, KIND_MOMENTARY
    ),
    MIDI_ACTION_PROJECT_SAVE: ActionSpec(
        MIDI_ACTION_PROJECT_SAVE, "Save project", CATEGORY_GLOBAL, KIND_MOMENTARY
    ),
}


# e54s02: a step action id -> its (next, prev) momentary pair. ONE generic
# executor (viseq.py `_exec_step`) fires the pair by the signed `params["steps"]`
# the resolver computes, so adding a new scroll target is one row here.
STEP_ACTIONS: dict[str, tuple[str, str]] = {
    MIDI_ACTION_SOURCE_STEP: (MIDI_ACTION_SOURCE_NEXT, MIDI_ACTION_SOURCE_PREV),
    MIDI_ACTION_DRAFT_STEP: (MIDI_ACTION_DRAFT_NEXT, MIDI_ACTION_DRAFT_PREV),
    MIDI_ACTION_FILE_STEP: (MIDI_ACTION_FILE_NEXT, MIDI_ACTION_FILE_PREV),
}


# e40s09: ids persisted by e40 before the terminology rename (Route -> Mapping).
# A Binding saved with an old id must keep triggering the same action, so every
# accessor and the dispatcher resolve through canonical_action().
LEGACY_ACTION_ALIASES: dict[str, str] = {
    "route_toggle": MIDI_ACTION_MAPPING_TOGGLE,
    "route_add": MIDI_ACTION_MAPPING_ADD,
}


def canonical_action(action_id: str) -> str:
    """The current id of an action; a legacy id maps to its renamed successor."""
    return LEGACY_ACTION_ALIASES.get(action_id, action_id)


def action_spec(action_id: str) -> ActionSpec | None:
    """The spec for an action id, or None when it is not a registered action."""
    return ACTION_SPECS.get(canonical_action(action_id))


def known_action(action_id: str) -> bool:
    """True when the action id is registered and therefore dispatchable."""
    return canonical_action(action_id) in ACTION_SPECS


def action_label(action_id: str) -> str:
    """Human label of an action; the raw id when the action is not registered.

    A stale binding (an action removed from the catalog) must still render a
    readable row, so unknown ids fall back to themselves.
    """
    spec = ACTION_SPECS.get(canonical_action(action_id))
    if spec is not None:
        return spec.label
    return str(action_id)
