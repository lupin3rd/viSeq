"""Sequencer core for viseq (REFACTOR_LATEST.md commit 9/13).

Beat-mode predicates and the per-step OSC senders. WORKER-SAFE: no dpg
import (HIGH-1) — the tick thread lives in the composition root and calls
these; UI text updates go through enqueue_set_value.
"""

import random
import time
from typing import Any

from viseqapp import catalog, state
from viseqapp.constants import (
    BEAT_SOURCE_BAND1,
    BEAT_SOURCE_MANUAL,
    BEAT_SOURCE_MIDI,
    BPM_DETECTION_STALE_SECONDS,
)
from viseqapp.osc import osc_client
from viseqapp.palette import dpg_color_rgba
from viseqapp.queues import append_log, enqueue_set_value


def send_colorv_step(track: dict[str, Any], row: int, col: int) -> None:
    """Send the picked RGB (0..1) for a ColorV step (HIGH-1 safe)."""
    target_addr = f"{track['base_address']}/color"
    r_val, g_val, b_val = [float(c) for c in track["steps"][col]["color"]]
    osc_client.send_message(target_addr, [r_val, g_val, b_val])
    append_log("OUT", f"{target_addr} [{r_val:.2f}, {g_val:.2f}, {b_val:.2f}]")


def send_colorr_step(track: dict[str, Any], row: int, col: int) -> None:
    """Send a random RGB for a ColorR step and show it in the step's color square."""
    target_addr = f"{track['base_address']}/color"
    r_val, g_val, b_val = (
        random.uniform(0.0, 1.0),
        random.uniform(0.0, 1.0),
        random.uniform(0.0, 1.0),
    )
    osc_client.send_message(target_addr, [r_val, g_val, b_val])
    append_log("OUT", f"{target_addr} [{r_val:.2f}, {g_val:.2f}, {b_val:.2f}]")

    step_data = track["steps"][col]
    step_data["last_rand_color"] = [r_val, g_val, b_val]
    tag_color = f"rand_color_{row}_{col}"
    enqueue_set_value(tag_color, dpg_color_rgba(step_data["last_rand_color"]))


def send_seekr_step(track: dict[str, Any], row: int, col: int) -> None:
    """Send a random seek (0..1) for a SeekR step and show the value in the cell."""
    target_addr = f"{track['base_address']}/seek"
    rand_val = random.uniform(0.0, 1.0)
    osc_client.send_message(target_addr, float(rand_val))
    append_log("OUT", f"{target_addr} [{rand_val:.2f}]")

    step_data = track["steps"][col]
    step_data["last_rand_seek"] = rand_val
    tag_seek = f"rand_seek_{row}_{col}"
    enqueue_set_value(tag_seek, f"{rand_val:.2f}")


def _timed_bpm_live() -> bool:
    """True when the fixed-interval tempo is real (e10s08).

    Manual BPM is always live (current_bpm is the entered value); BPM Analysis
    is live only while beat tracking is on and a detection arrived within the
    stale window. Band/MIDI modes never use the timed tempo.
    """
    if state.beat_source == BEAT_SOURCE_MANUAL:
        return True
    return (
        state.is_beat_tracking
        and time.time() - state.bpm_last_detected <= BPM_DETECTION_STALE_SECONDS
    )


def beat_is_event_driven() -> bool:
    """True when the beat comes from an event (band 1 peak / MIDI clock), not a fixed interval."""
    return state.beat_source in (BEAT_SOURCE_BAND1, BEAT_SOURCE_MIDI)


# ==============================================================================
# e36s04: canonical (property, mode) step tokens + the per-beat step engine.
# A step type is `Prop + mode-letter`: V value, R random, F fade (client-side
# envelope across beats), X fire (toggle/trigger), C cycle (enum). The legacy
# tokens (AlphaV/AlphaR/AlphaF/ColorV/ColorR/SeekR) are exactly the canonical
# forms, so the parser is the single dispatch path and persisted types never
# change. No ms-animation messages and no rate-family steps (user decisions).
# ==============================================================================

_MODE_SUFFIX = {"V": "value", "R": "random", "F": "fade", "X": "fire", "C": "cycle"}
_MODE_LABELS = {
    "value": "Value",
    "random": "Random",
    "fade": "Fade",
    "fire": "Fire",
    "cycle": "Cycle",
}


def _mode_letter(mode: str) -> str:
    for letter, name in _MODE_SUFFIX.items():
        if name == mode:
            return letter
    raise KeyError(f"unknown step mode {mode!r}")


def step_token(prop: str, mode: str) -> str:
    """Canonical persisted type token of (property, mode), e.g. 'SpeedF', 'PlayX'."""
    return prop[0].upper() + prop[1:] + _mode_letter(mode)


def parse_step_token(token: str) -> tuple[str, str] | None:
    """(property, mode) of a step type token; None for NONE or garbage tokens."""
    if not token or token == "NONE" or len(token) < 2:
        return None
    mode = _MODE_SUFFIX.get(token[-1])
    prop = token[:-1].lower()
    if mode is None or prop not in catalog.PROPERTY_CATALOG:
        return None
    return prop, mode


def step_modes_for(prop: str) -> list[str]:
    """The step modes a property supports (family-driven, e36s04).

    set_scalar -> value/random (+ fade, except seek whose envelope is
    meaningless); color (the vector a step cell can edit) -> value/random;
    toggle & trigger -> fire (a momentary message per beat); enum -> cycle
    (advance the option index each beat). Rate family and the other vectors
    offer nothing — no ms-animation and no rate steps (user decisions).
    """
    family = catalog.family_of(prop)
    if family == catalog.FAMILY_SET_SCALAR:
        modes = ["value", "random"]
        if prop != "seek":
            modes.append("fade")
        return modes
    if prop == "color":  # set_vec: only color has a cell editor (legacy)
        return ["value", "random"]
    if family in (catalog.FAMILY_TOGGLE, catalog.FAMILY_TRIGGER):
        return ["fire"]
    if family == catalog.FAMILY_ENUM:
        return ["cycle"]
    return []


def step_modes_covered_by_legacy(prop: str, mode: str) -> bool:
    """(prop, mode) combos the fixed top-level step menu already offers."""
    return (prop, mode) in {
        ("alpha", "value"),
        ("alpha", "random"),
        ("alpha", "fade"),
        ("color", "value"),
        ("color", "random"),
        ("seek", "random"),
    }


def more_step_offerings() -> list[dict[str, Any]]:
    """Extra (property, mode) steps for the 'More properties…' cell submenu.

    Every catalog property with at least one allowed mode, in catalog order,
    skipping the rate family and the combos the legacy top-level menu already
    lists. Each entry: {prop, label, modes: [(mode, token, menu_label), ...]}.
    """
    offerings: list[dict[str, Any]] = []
    for prop, entry in catalog.PROPERTY_CATALOG.items():
        modes = [m for m in step_modes_for(prop) if not step_modes_covered_by_legacy(prop, m)]
        if not modes:
            continue
        offerings.append(
            {
                "prop": prop,
                "label": str(entry["label"]),
                "modes": [(mode, step_token(prop, mode), _MODE_LABELS[mode]) for mode in modes],
            }
        )
    return offerings


def _component_bounds(prop: str) -> tuple[float, float]:
    """(min, max) of the first component of a scalar/single-value property."""
    comp = catalog.PROPERTY_CATALOG[prop]["components"][0]
    return float(comp["min"]), float(comp["max"])


def _rand_tag(row: int, col: int) -> str:
    return f"rand_v1_{row}_{col}"


def execute_step(
    track: dict[str, Any], row: int, col: int, beat_seconds: float | None = None
) -> None:
    """Fire one active step on the beat (e36s04; HIGH-1 worker-safe).

    Reads the step's canonical type token, parses (property, mode) and sends
    the matching OSC message through the shared log/queue paths — the same
    messages the legacy hard-coded dispatch sent, now property-driven. Fades
    use ``beat_seconds`` (the timed beat window) and fall back to 60/BPM when
    the beat is event-driven. Unknown/garbage tokens and props without the
    mode are silent no-ops.
    """
    step_data = track["steps"][col]
    if not step_data.get("active", False):
        return  # the tick only calls active steps; the engine guards anyway
    parsed = parse_step_token(str(step_data.get("type", "NONE")))
    base_addr = str(track.get("base_address") or "")
    if parsed is None or not base_addr or not base_addr.strip():
        return
    prop, mode = parsed
    if mode not in step_modes_for(prop):
        return

    # Legacy vector/seek cells keep their dedicated helpers (identical sends)
    if prop == "color":
        if mode == "value":
            send_colorv_step(track, row, col)
        else:
            send_colorr_step(track, row, col)
        return
    if prop == "seek" and mode == "random":
        send_seekr_step(track, row, col)
        return

    target_addr = f"{base_addr}/{prop}"
    lo, hi = _component_bounds(prop)

    if mode == "value":
        val = max(lo, min(hi, float(step_data.get("v1") or 0.0)))
        osc_client.send_message(target_addr, float(val))
        append_log("OUT", f"{target_addr} [{val:.2f}]")

    elif mode == "random":
        rand_val = random.uniform(lo, hi)
        osc_client.send_message(target_addr, float(rand_val))
        append_log("OUT", f"{target_addr} [{rand_val:.2f}]")
        step_data["last_rand_v1"] = rand_val
        enqueue_set_value(_rand_tag(row, col), f"{rand_val:.2f}")

    elif mode == "fade":
        total_msgs = int(step_data.get("frames") or 4) * int(step_data.get("msgs") or 1)
        if total_msgs < 1:
            total_msgs = 1
        if beat_seconds is None or beat_seconds <= 0:
            bpm = state.current_bpm if state.current_bpm > 0 else 120.0
            beat_seconds = 60.0 / bpm
        msgs = max(1, int(step_data.get("msgs") or 1))
        start_val = max(lo, min(hi, float(step_data.get("v1") or lo)))
        end_val = max(lo, min(hi, float(step_data.get("v2") or hi)))
        track["active_fade"] = {
            "active": True,
            "address": target_addr,
            "start_val": start_val,
            "end_val": end_val,
            "total_msgs": total_msgs,
            "msg_interval": beat_seconds / msgs,
            "start_time": time.time(),
            "last_msg_index": 0,
        }
        osc_client.send_message(target_addr, float(start_val))
        append_log("OUT", f"{target_addr} [FADE START: {start_val:.2f}]")

    elif mode == "fire":
        family = catalog.family_of(prop)
        if family == catalog.FAMILY_TOGGLE:
            val = max(0.0, min(1.0, float(step_data.get("v1") or 0.0)))
            osc_client.send_message(target_addr, float(val))
            append_log("OUT", f"{target_addr} [{val:.2f}]")
        else:  # trigger: replay/reset/reload no-arg; flag = next
            osc_client.send_message(target_addr, [])
            append_log("OUT", f"{target_addr} (fire)")

    elif mode == "cycle":
        options = catalog.PROPERTY_CATALOG[prop].get("options") or []
        count = max(1, len(options))
        raw = step_data.get("last_idx")
        last = int(raw) if raw is not None else -1
        index = (last + 1) % count
        step_data["last_idx"] = index
        osc_client.send_message(target_addr, float(index))
        append_log("OUT", f"{target_addr} [{index}.00]")
        enqueue_set_value(_rand_tag(row, col), f"{index}")
