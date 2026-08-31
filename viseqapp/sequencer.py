"""Sequencer core for viseq (REFACTOR_LATEST.md commit 9/13).

Beat-mode predicates and the per-step OSC senders. WORKER-SAFE: no dpg
import (HIGH-1) — the tick thread lives in the composition root and calls
these; UI text updates go through enqueue_set_value.
"""

import random
import time
from typing import Any

from viseqapp import state
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
