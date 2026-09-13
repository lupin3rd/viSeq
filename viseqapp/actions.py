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
    MIDI_ACTION_ENABLE_CORRECTION,
    MIDI_ACTION_IO_MONITOR_TOGGLE,
    MIDI_ACTION_MAPPER_BAND,
    MIDI_ACTION_MAPPER_CUE_OPEN,
    MIDI_ACTION_MAPPER_ENABLE,
    MIDI_ACTION_MAPPER_LINE,
    MIDI_ACTION_MAPPER_MAPPING,
    MIDI_ACTION_MAPPER_RESET,
    MIDI_ACTION_MAPPING_ADD,
    MIDI_ACTION_MAPPING_TOGGLE,
    MIDI_ACTION_NUDGE_BACK,
    MIDI_ACTION_NUDGE_FORWARD,
    MIDI_ACTION_REGEN_SELECTED,
    MIDI_ACTION_SEQ_ROW_ASSIGN,
    MIDI_ACTION_SEQ_ROW_DISABLE,
    MIDI_ACTION_SEQ_ROW_ENABLE,
    MIDI_ACTION_SEQ_TOGGLE,
    MIDI_ACTION_SOURCE_NEXT,
    MIDI_ACTION_SOURCE_PREV,
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

# Kinds describe how the incoming MIDI value maps onto the action.
KIND_MOMENTARY = "momentary"  # note edges trigger; CC fires at the >=64 threshold
KIND_VALUE = "value"  # the raw CC 0..127 value is consumed (rescaleped by the action)


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
    MIDI_ACTION_SOURCE_NEXT: ActionSpec(
        MIDI_ACTION_SOURCE_NEXT, "Next source", CATEGORY_MEDIAGRID, KIND_MOMENTARY
    ),
    MIDI_ACTION_SOURCE_PREV: ActionSpec(
        MIDI_ACTION_SOURCE_PREV, "Previous source", CATEGORY_MEDIAGRID, KIND_MOMENTARY
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
    MIDI_ACTION_IO_MONITOR_TOGGLE: ActionSpec(
        MIDI_ACTION_IO_MONITOR_TOGGLE, "I/O Monitor window", CATEGORY_MONITOR, KIND_MOMENTARY
    ),
    MIDI_ACTION_MAPPING_TOGGLE: ActionSpec(
        MIDI_ACTION_MAPPING_TOGGLE, "Arm/disarm Mapping", CATEGORY_MAPPING, KIND_MOMENTARY
    ),
    MIDI_ACTION_MAPPING_ADD: ActionSpec(
        MIDI_ACTION_MAPPING_ADD, "Add Mapping to line", CATEGORY_MAPPING, KIND_MOMENTARY
    ),
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
