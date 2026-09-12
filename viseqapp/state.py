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


midi_learn_armed_tag: str | None = None  # learn marker armed by the last click (its M turns amber)


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


# e38: source video preview transport (viseqapp/preview.py) — the worker
# pushes decoded RGBA float32 frames as (source_name, frame) tuples here;
# the main loop drains them into the preview texture (worker never imports
# dpg). preview_active names the source shown right now (UI-owned);
# preview_error carries the last fatal message for the panel.
preview_frames: queue.Queue[Any] = queue.Queue()
preview_active: str | None = None
preview_playing: bool = False  # transport running (play) vs paused — UI-owned
preview_error: str | None = None


log_queue: queue.Queue[str] = queue.Queue()


ui_task_queue: queue.Queue[Callable[[], None]] = (
    queue.Queue()
)  # UI mutations from worker threads, drained on the main thread


_media_cell_cache: dict[str, str | float] = {}


global_vimix_state: dict[str, Any] = {"current_source": None, "sources": {}}


# e36s01: per-target full-component vectors for anchored OSC sends
# (target_id -> property -> [component values]). Written by full-vector sends;
# nil-capable properties never consult it. Fed by live vimix state in e36s06.
source_anchors: dict[str, dict[str, list[float]]] = {}


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


_last_unmatched_log: dict[str, float] = {}  # port -> last unmatched-message log time


_midi_first_msg_logged: set[str] = set()  # ports that already logged their first message


midi_controllers: list[dict[str, Any]] = []


_controller_lock = threading.Lock()


_controller_profiles: dict[str, dict[str, Any]] = {}


# e26: Leap Motion engine state (worker-owned). leap_values is mutated IN
# PLACE (clear + update under leap_lock) so facade re-exports stay live; the
# worker writes it from its own thread, the UI reads copies on the main thread.
leap_enabled: bool = False


leap_status: str = "missing"  # missing | disconnected | connected | tracking


leap_values: dict[str, float] = {}


leap_lock = threading.Lock()


# e26s04: visualizer toggle mirror (main-thread flag; the worker watches it to
# manage the LeapC Images policy) + the poll-thread visualizer snapshot. The
# snapshot is written only on the library poll thread under leap_lock; the main
# thread swaps a reference (microseconds), never a deep copy of the arrays.
leap_visualizer: bool = False
leap_viz_ir: np.ndarray | None = None  # latest grayscale IR copy (240, 640)
leap_viz_hands: list[dict[str, Any]] | None = None  # latest hand-geometry dicts
leap_viz_frame: np.ndarray | None = None  # latest RGBA float32 composite (h, w, 4)
leap_viz_seq: int = 0  # incremented on every composite publish
leap_viz_last_render: float = 0.0  # poll-thread rate-gate timestamp
leap_viz_uploaded_seq: int = 0  # main-thread only: last seq uploaded to the texture
leap_viz_tex_created: bool = False  # main-thread only: raw texture built once


# e26s05: stall watchdog state (worker-owned, no new lock — same practice as
# leap_status). leap_last_frame is the epoch of the last tracking event (0 =
# never); leap_stall_count counts consecutive watchdog-forced reconnects.
leap_last_frame: float = 0.0
leap_stall_count: int = 0


# e26s02: leap window tag -> last rendered text (main-thread only, avoids
# re-writing unchanged monitor cells / the status line on every main tick).
leap_monitor_cache: dict[str, str] = {}


# e26s03: per-mapping drive bookkeeping (mapping id -> {last_raw, last_push}).
# Written by the leap worker listener thread when it pushes a mapped value.
leap_drive_state: dict[int, dict[str, Any]] = {}


# e16: Mapper state — OSC property mappings (see viseqapp/mapper.py).
# Each entry: {id, target_id, property, control, value}; ids come from the
# monotonic counter (like the other UI element counters).
mapper_mappings: list[dict[str, Any]] = []


mapper_counter: int = 0


mapper_pending_target: str | None = None  # source the New-mapping dialog targets


# e35s02: active cue runs — written by the cue engine (viseqapp/cue.py) from
# the scheduler thread, read by the UI for running indicators (e35s03). Each
# entry: {mapping_id, target_id, plan, cursor} with plan entries of the shape
# (time_ms, action, payload) built by cue.build_cue_plan. Main thread only
# mutates through the queues; the engine mutates this list directly (it is
# dpg-free by construction).
cue_runs: list[dict[str, Any]] = []


# e35s05: which mapping's cue editor is open (main-thread only; None = closed).
# The window shows the mapping context, so a removed mapping must close it.
cue_editor_mapping_id: int | None = None


# e35s03: main-thread cache of the last relabel per cue-list card trigger
# (mapping id -> label). Written ONLY by tick_cue_triggers on the main thread;
# cleared by refresh_mapper_ui after a body rebuild. Worker threads never touch
# it (HIGH-1) — the engine reflects running state through cue_runs alone.
cue_trigger_label_cache: dict[int, str] = {}


# e35 UAT: last rendered progress text per cue-list card ('3 of 10'); written
# only by tick_cue_triggers on the main thread. The engine never touches it.
cue_progress_cache: dict[int, str] = {}


# e35 UAT: TWO shared button/trigger themes (OFF then ON) bound by tag. They are
# root-level theme items created once and rebuilt ONLY when the active palette
# changes — per-trigger theme churn caused DPG alias collisions (1000).
trigger_theme_tags: list[str | None] = [None, None]  # [0]=off, [1]=on
trigger_theme_signature: tuple[tuple[int, ...], ...] | None = None


# e17 / BUG-2026-09-01T194500: last focused workspace window. DPG's
# get_active_window() returns None while the viewport menu bar has focus, so
# the Windows-menu mark and the Ctrl+Tab anchor come from this tracking instead.
current_window: str | None = None


# e37 (project-save-as-titlebar): the session's project identity — which .viseq
# document the live content belongs to and whether the next Save would write
# something different. Main-thread only: the file flows and the title sync run
# on the main thread; worker modules never touch these fields.
current_project_path: str | None = None  # None = unnamed (new) project
project_dirty: bool = False  # live content differs from the last-saved baseline
saved_content_fingerprint: str = ""  # canonical JSON of the content at the last save/open/new


# e40s01: the Route engine's runtime memory — per-Route real-value bookkeeping for
# dead-reckoning (routeengine.route_raw_value) and the last emitted Destination
# value (the epsilon dedupe). Main-thread only; never persisted.
route_book: dict[int, dict[str, Any]] = {}
route_values: dict[int, float] = {}
# e40s01: the /viosc/monitor subscriptions the live routes currently hold, so the
# tick only re-issues them when the desired set changes.
route_subscriptions: dict[str, list[str]] = {}
# e40s02: Routes the orphan policy disabled (their source is gone); they are
# re-enabled automatically when the source comes back.
route_orphans: set[int] = set()
# e40s06: the targeted watch lane — the /viosc/reply deltas land on this queue
# from the OSC server thread and are applied on the main thread; the plan last
# sent to viOSC and whether the lane is proven alive (None = unknown, still
# using the 2 s monitor fallback).
watch_state_queue: queue.Queue[tuple[str, list[Any]]] = queue.Queue()
route_watch_plan: dict[str, dict[str, Any]] = {}
osc_watch_supported: bool | None = None
