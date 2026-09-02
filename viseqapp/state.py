"""Central mutable state for viseq (REFACTOR_LATEST.md commit 3/13).

Every module-level mutable global lives here — the single home for shared
state. Access rule (see specs/REFACTOR_LATEST.md): import the MODULE and
write/read as ``state.NAME``. Scalar names must never be imported by name
(rebinds would shadow the module attribute and go stale); container names
may be imported by name since they are mutated in place (same object).

Worker modules (osc/audio/sequencer/midi) read this state and push UI
mutations through ``state.ui_task_queue`` — they never import dpg.
"""

import copy
import queue
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from viseqapp.constants import (
    BEAT_SOURCE_ANALYSIS,
    DEFAULT_MANUAL_BPM,
    DEFAULT_PALETTE,
    NUM_STEPS,
    NUM_TRACKS,
    SPECTRUM_BARS,
)

viosc_client: Any = None


local_osc_server: Any = None


local_server_thread: threading.Thread | None = None


is_server_running: bool = False


midi_enabled: bool = False


midi_bindings: list[dict[str, Any]] = []


midi_learn_mode: bool = False


midi_learn_pending: tuple[str, dict[str, Any]] | None = None


midi_learn_started_at: float = 0.0  # e14: learn-session start, for the safety timeout


midi_selected_port: str | None = None  # e14s03: port whose bindings the Bindings section shows


midi_clock_source: str | None = None  # e14s04: MIDI clock input (None = first available)


_theme_color_bindings: dict[Any, str] = {}


_text_color_bindings: dict[Any, str] = {}


_draw_color_bindings: dict[Any, tuple[str, str]] = {}


active_palette: dict[str, list[int]] = copy.deepcopy(DEFAULT_PALETTE)


theme_global: Any = None


ui_state_queue: queue.Queue[Any] = queue.Queue()


blob_queue: queue.Queue[Any] = queue.Queue()


texture_queue: queue.Queue[Any] = queue.Queue()


log_queue: queue.Queue[str] = queue.Queue()


ui_task_queue: queue.Queue[Callable[[], None]] = (
    queue.Queue()
)  # UI mutations from worker threads, drained on the main thread


_media_cell_cache: dict[str, str | float] = {}


global_vimix_state: dict[str, Any] = {"current_source": None, "sources": {}}


viseq_selected_source: str | None = None


last_ui_signature: str = ""


last_num_cols: int = 4


osc_log_history: list[str] = []


current_step: int = -1


is_playing: bool = False


phase_nudge: float = 0.0


sync_event_seq = threading.Event()


sync_event_led = threading.Event()


beat_source: str = BEAT_SOURCE_ANALYSIS  # default: current behavior (essentia BPM)


sync_event_beat = threading.Event()  # fired once per beat in band/MIDI modes


midi_pulses: int = 0  # running MIDI clock pulse count (worker thread)


tap_times: list[float] = []  # TAP timestamps for the manual BPM mode


band_prev_values: dict[int, float] = {1: 0.0, 2: 0.0, 3: 0.0}  # band rising-edge tracking


copied_step_data: dict[str, Any] | None = None  # step config copied for paste (e08)


active_step: tuple[int, int] | None = None  # last touched step (keyboard shortcuts target)


copied_step_pos: tuple[int, int] | None = None  # where the copied highlight is shown


def _pristine_step() -> dict[str, Any]:
    """A pristine runtime step cell: persisted defaults + zeroed runtime keys.

    Shared by boot and the New-project reset (e15s01) so both produce the
    identical shape; the last_rand_* keys are runtime-only and exist solely in
    live cells.
    """
    return {
        "active": False,
        "type": "NONE",
        "v1": 0.0,
        "v2": 1.0,
        "frames": 4,
        "msgs": 1,
        "color": [1.0, 1.0, 1.0],
        "last_rand_v1": 0.0,
        "last_rand_seek": 0.0,
        "last_rand_color": [0, 0, 0],
    }


def _pristine_track() -> dict[str, Any]:
    """A pristine runtime track: no clip, inactive fade, NUM_STEPS blank cells.

    Shared by boot and the New-project reset (e15s01); the New-project reset
    replaces each row wholesale, so pending fades and runtime random state
    disappear with it.
    """
    return {
        "target_id": None,
        "base_address": "",
        "active_fade": {"active": False},
        "steps": [_pristine_step() for _ in range(NUM_STEPS)],
    }


tracks_data: list[dict[str, Any]] = [_pristine_track() for _ in range(NUM_TRACKS)]


samplerate = 44100


is_audio_analyzing: bool = False


is_beat_tracking: bool = False


lowpass_enabled: bool = True  # mirrors the "Use Low-Pass Filter" checkbox (read on worker threads)


audio_buffer: np.ndarray = np.zeros(
    samplerate * 6, dtype=np.float32
)  # preallocated ring buffer (L-2)


audio_buffer_head: int = 0  # next write position in audio_buffer (modulo its length)


current_bpm: float = DEFAULT_MANUAL_BPM


bpm_last_detected: float = 0.0


beat_confidence: float = 0.0


spectrum_bars_cache: np.ndarray = np.zeros(SPECTRUM_BARS, dtype=np.float32)


spec_peak_hold: float = 0.0  # AGC running spectral peak (spectrum worker only, e10s09)


bands_enabled: dict[int, bool] = {1: False, 2: False, 3: False}


band1: float = 0.0


band2: float = 0.0


band3: float = 0.0


thumbnails_data: dict[str, list[str]] = {}


request_timestamps: dict[str, float] = {}


thumb_cycle_state: dict[str, tuple[int, float]] = {}


thumb_fail_count: dict[str, int] = {}


monitor_players: list[dict[str, Any]] = []  # each: {"id", "tag", "target_id", "props"}


monitor_player_counter = 0


_last_unmatched_log: dict[str, float] = {}  # port -> last unmatched-message log time


_midi_first_msg_logged: set[str] = set()  # ports that already logged their first message


midi_controllers: list[dict[str, Any]] = []


_controller_lock = threading.Lock()


_controller_profiles: dict[str, dict[str, Any]] = {}


# e16: Mapper state — OSC property mappings (see viseqapp/mapper.py).
# Each entry: {id, target_id, property, control, value}; ids come from the
# monotonic counter (like monitor_player_counter).
mapper_mappings: list[dict[str, Any]] = []


mapper_counter: int = 0


mapper_pending_target: str | None = None  # source the New-mapping dialog targets


# e17 / BUG-2026-09-01T194500: last focused workspace window. DPG's
# get_active_window() returns None while the viewport menu bar has focus, so
# the Windows-menu mark and the Ctrl+Tab anchor come from this tracking instead.
current_window: str | None = None
