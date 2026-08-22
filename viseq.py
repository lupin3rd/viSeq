import contextlib
import io
import json
import queue
import random
import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any

import dearpygui.dearpygui as dpg
import essentia
import essentia.standard as es
import numpy as np
import sounddevice as sd
from PIL import Image
from pythonosc import dispatcher, osc_server, udp_client

# --- HARD CAPS ON NETWORK-FED DATA (viOSC replies) ---
# Bound memory use and block PIL decompression bombs (audit MED-6).
Image.MAX_IMAGE_PIXELS = 25_000_000  # PIL's hard ceiling (~25 MP)
MAX_THUMBNAIL_PIXELS = 3_000_000  # explicit cap; real thumbs are ~58k px
MAX_THUMBNAIL_BLOB_BYTES = 8 * 1024 * 1024  # per-blob cap
MAX_STATE_JSON_BYTES = 1 * 1024 * 1024  # per-replydata cap

# --- BEHAVIORAL CONSTANTS (audit L-6) ---
THUMB_REQUEST_INTERVAL = 3.0  # min seconds between thumbnail requests per source
LOG_HISTORY_LIMIT = 25  # max entries kept in the OSC log window
MONITOR_OFFSET = (280, 260)  # grid spacing between monitor player windows
DPG_COLOR_SCALE = 255.0  # DPG ToColor divides color inputs by 255 -> its color API is 0..255

# Step-cell layout: a centered square leaves the checkbox/type row on top (audit L-6)
STEP_CELL_SIZE = 90  # px side of each sequencer step cell
STEP_COLOR_SQUARE_SIZE = 40  # px side of the centered color square inside a step cell
# ImGui WindowPadding.x inside child windows (default style; app themes don't override)
STEP_CELL_CONTENT_PADDING = 8
# Measured on DPG 2.3.1: this indent centers the swatch in the 90px cell (padding included)
STEP_COLOR_SQUARE_INDENT = (
    STEP_CELL_SIZE - 2 * STEP_CELL_CONTENT_PADDING - STEP_COLOR_SQUARE_SIZE
) // 2

# Clip-slot layout: a bare centered assign button, no table frame around it (audit L-6).
# The borderless slot has no WindowPadding (content_region_avail == full size, measured on
# 2.3.1), so horizontal centering is pure (width - button_width) / 2. Vertically the button's
# frame rect sits SLOT_BUTTON_FRAME_INSET px below its layout box (measured), hence the -inset.
SLOT_WIDTH = 135  # px width of the clip slot column
SLOT_HEIGHT = 90  # px height of a sequencer row
SLOT_BUTTON_WIDTH = 110  # px width of the assign button/thumbnail
SLOT_BUTTON_HEIGHT = 70  # px height of the assign button/thumbnail
SLOT_BUTTON_FRAME_INSET = 4  # px: ImGui button rect offset below its widget box (2.3.1)
SLOT_BUTTON_INDENT = (SLOT_WIDTH - SLOT_BUTTON_WIDTH) // 2
SLOT_BUTTON_TOP_SPACER = (SLOT_HEIGHT - SLOT_BUTTON_HEIGHT) // 2 - SLOT_BUTTON_FRAME_INSET

# --- OSC CONFIGURATION ---
# viseq talks exclusively to viOSC: /vimix/* messages are forwarded by viOSC
# to Vimix (port 7000), replies come back on viOSC's output port 6667.
VIOSC_IP = "127.0.0.1"
VIOSC_PORT = 6666
VIOSC_LISTEN_PORT = 6667  # the port viOSC sends replies to; viseq's own server listens here
osc_client = udp_client.SimpleUDPClient(VIOSC_IP, VIOSC_PORT)

viosc_client: Any = None
local_osc_server: Any = None
local_server_thread: threading.Thread | None = None
is_server_running: bool = False

# --- COMMUNICATION QUEUES ---
ui_state_queue: queue.Queue[Any] = queue.Queue()
blob_queue: queue.Queue[Any] = queue.Queue()
texture_queue: queue.Queue[Any] = queue.Queue()
log_queue: queue.Queue[str] = queue.Queue()
ui_task_queue: queue.Queue[Callable[[], None]] = (
    queue.Queue()
)  # UI mutations from worker threads, drained on the main thread


def ui_task(fn: Callable[[], None]) -> None:
    """Run a UI mutation on the main thread via the task queue."""
    ui_task_queue.put(fn)


def dpg_color_value(rgb: list[float]) -> list[float]:
    """Convert normalized 0..1 RGB to DPG's color API scale (0..255).

    DPG 2.x parses color lists by dividing every channel by 255, so colors
    pushed programmatically (default_value / set_value) must arrive on the
    0..255 scale; user-picked colors arrive via callbacks already 0..1.
    """
    return [round(c * DPG_COLOR_SCALE, 2) for c in rgb]


def dpg_color_rgba(rgb: list[float]) -> list[float]:
    """DPG-scale RGB plus an opaque alpha (color_button needs 4 components)."""
    return [*dpg_color_value(rgb), DPG_COLOR_SCALE]


def enqueue_set_value(tag: str, value: Any) -> None:
    """Queue a dpg.set_value(tag, value) for the main thread, if the item exists."""

    def _set():
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, value)

    ui_task(_set)


def log_error(context: str, message: str) -> None:
    t = time.strftime("%H:%M:%S")
    log_queue.put(f"[{t}] ERROR: {context}: {message}")


def format_osc_log(history: list[str]) -> str:
    """Render the OSC log newest-first (the latest line on top)."""
    return "\n".join(reversed(history))


# --- GLOBAL VIMIX STATE ---
global_vimix_state: dict[str, Any] = {"current_source": None, "sources": {}}

ALL_PROPERTIES = [
    "index",
    "name",
    "lock",
    "failed",
    "play",
    "pause",
    "blending",
    "alpha",
    "transparency",
    "depth",
    "position",
    "size",
    "corner",
    "angle",
    "seek",
    "speed",
    "brightness",
    "contrast",
    "saturation",
    "hue",
    "threshold",
    "gamma",
    "color",
    "posterize",
    "invert",
    "uri",
]

last_ui_signature: str = ""
last_num_cols: int = 4
osc_log_history: list[str] = []

# --- SEQUENCER STATE ---
NUM_STEPS = 8
NUM_TRACKS = 8
current_step: int = -1
is_playing: bool = False
phase_nudge: float = 0.0
sync_event_seq = threading.Event()
sync_event_led = threading.Event()

# Beat/clock source selection (e05): the sequencer can follow the analyzed BPM, a band
# hitting 1.0, standard MIDI clock, or a manual BPM (numeric/TAP). Event-driven modes wake
# the sequencer on sync_event_beat instead of sleeping a fixed interval.
BEAT_SOURCE_ANALYSIS = "bpm_analysis"
BEAT_SOURCE_BAND1 = "band1_beat"
BEAT_SOURCE_BAND2 = "band2_beat"
BEAT_SOURCE_BAND3 = "band3_beat"
BEAT_SOURCE_MIDI = "midi_sync"
BEAT_SOURCE_MANUAL = "manual_bpm"
BEAT_SOURCE_LABELS = {
    BEAT_SOURCE_ANALYSIS: "Rilevazione BPM",
    BEAT_SOURCE_BAND1: "Battito Band 1",
    BEAT_SOURCE_BAND2: "Battito Band 2",
    BEAT_SOURCE_BAND3: "Battito Band 3",
    BEAT_SOURCE_MIDI: "MIDI Sync",
    BEAT_SOURCE_MANUAL: "BPM Manuale",
}
beat_source: str = BEAT_SOURCE_ANALYSIS  # default: current behavior (essentia BPM)
sync_event_beat = threading.Event()  # fired once per beat in band/MIDI modes
MIDI_CLOCK_PULSES_PER_BEAT = 24  # MIDI standard: 24 clock pulses (0xF8) per quarter note
midi_pulses: int = 0  # running MIDI clock pulse count (worker thread)
tap_times: list[float] = []  # TAP timestamps for the manual BPM mode
band_prev_values: dict[int, float] = {1: 0.0, 2: 0.0, 3: 0.0}  # band rising-edge tracking
# Width of the transport row (PLAY + spacer + < + RESYNC + > + spacer). Measured on real
# DPG 2.3.1: buttons render wider than their declared widths, so the alignment spacer is
# 312px with the compact 28px-high transport (audit L-6).
SEQ_TRANSPORT_WIDTH = 312

# One LED per beat source, shown next to its checkbox on the sequencer (e05)
BEAT_LED_TAGS = {
    BEAT_SOURCE_ANALYSIS: "led_analysis",
    BEAT_SOURCE_BAND1: "led_band1",
    BEAT_SOURCE_BAND2: "led_band2",
    BEAT_SOURCE_BAND3: "led_band3",
    BEAT_SOURCE_MIDI: "led_midi",
    BEAT_SOURCE_MANUAL: "led_manual",
}
BEAT_CHECKBOX_TAGS = {mode: f"cb_beat_{mode}" for mode in BEAT_SOURCE_LABELS}

# Sequencer data structure with the new "msgs" parameter
tracks_data: list[dict[str, Any]] = []
for _ in range(NUM_TRACKS):
    track: dict[str, Any] = {
        "target_id": None,
        "base_address": "",
        "active_fade": {"active": False},
        "steps": [],
    }
    for _ in range(NUM_STEPS):
        track["steps"].append(
            {
                "active": False,
                "type": "NONE",
                "v1": 0.0,
                "v2": 1.0,
                "frames": 4,
                "msgs": 1,  # NEW: number of messages to send in a single step
                "color": [1.0, 1.0, 1.0],
                "last_rand_v1": 0.0,
                "last_rand_seek": 0.0,
                "last_rand_color": [0, 0, 0],
            }
        )
    tracks_data.append(track)

# --- AUDIO STATE ---
samplerate = 44100
is_audio_analyzing: bool = False
is_beat_tracking: bool = False
lowpass_enabled: bool = True  # mirrors the "Use Low-Pass Filter" checkbox (read on worker threads)
audio_stream: Any = None
audio_buffer: np.ndarray = np.zeros(
    samplerate * 6, dtype=np.float32
)  # preallocated ring buffer (L-2)
audio_buffer_head: int = 0  # next write position in audio_buffer (modulo its length)
current_bpm: float = 120.0
beat_confidence: float = 0.0
rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
lowpass_filter = es.LowPass(cutoffFrequency=250.0)

# --- SPECTRUM ANALYZER (e04) ---
SPECTRUM_FFT_SIZE = 2048  # samples per FFT frame
# 16 bars > 32: benchmarked lighter (~26% less compute per frame + half the draw calls);
# the FFT dominates either way, and 16 bars keep a clear view of the audible range.
SPECTRUM_BARS = 16
NUM_BANDS = 3  # independent selectable bands (band1/band2/band3)
SPECTRUM_FPS = 30.0  # spectrum redraw rate while analyzing
SPECTRUM_DB_FLOOR = 60.0  # dB below full scale mapped to bar level 0
SPEC_DRAWLIST_W = 330  # spectrum drawlist width (px)
SPEC_DRAWLIST_H = 66  # spectrum drawlist height (px) — tall enough to read the bars
SPECTRUM_BAR_COLOR = (80, 255, 120, 255)  # green bars
BAND_RECT_COLORS = {
    1: ((255, 255, 0, 40), (255, 255, 0, 200)),  # yellow overlay
    2: ((0, 255, 255, 40), (0, 255, 255, 200)),  # cyan overlay
    3: ((255, 0, 255, 40), (255, 0, 255, 200)),  # magenta overlay
}
BAND_DEFAULT_RANGES = {1: (0.0, 0.33), 2: (0.33, 0.66), 3: (0.66, 1.0)}  # equal thirds

# Last computed spectrum bars + per-band state. All written on the main thread inside the
# queued spectrum task; future features read band1/band2/band3 (0..1, 0 while disabled).
spectrum_bars_cache: np.ndarray = np.zeros(SPECTRUM_BARS, dtype=np.float32)
bands_enabled: dict[int, bool] = {1: False, 2: False, 3: False}
band1: float = 0.0
band2: float = 0.0
band3: float = 0.0

thumbnails_data: dict[str, str] = {}
request_timestamps: dict[str, float] = {}


def get_input_devices() -> list[str]:
    devices = sd.query_devices()
    inputs = [f"{i}: {d['name']}" for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    return inputs if inputs else ["No input device found"]


def flash_led(tag: str) -> None:
    """Flash a beat LED green on the main thread, fading back after 100ms (HIGH-1)."""

    def _on():
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, fill=(80, 255, 120, 255))

    def _off():
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, fill=(50, 50, 50, 255))

    ui_task(_on)
    threading.Timer(0.1, lambda: ui_task(_off)).start()


def append_log(direction: str, address: str) -> None:
    t = time.strftime("%H:%M:%S")
    log_msg = f"[{t}] {direction}: {address}"
    log_queue.put(log_msg)


# ==============================================================================
# SEQUENCER UI & CLIP ASSIGNMENT
# ==============================================================================


def assign_clip_to_track(sender: Any, app_data: Any, user_data: Any) -> None:
    row = user_data
    current_source = global_vimix_state.get("current_source")

    if current_source is not None:
        data_dict = global_vimix_state.get("sources", {})
        target_id = None

        for k, props in data_dict.items():
            if str(k) == str(current_source):
                name = props.get("name")
                target_id = str(name) if name else str(k)
                break

        if target_id:
            tracks_data[row]["target_id"] = target_id
            tracks_data[row]["base_address"] = f"/vimix/{target_id}"
            update_track_slot_ui(row)


def update_track_slot_ui(row: int) -> None:
    slot_tag = f"seq_slot_{row}"
    if not dpg.does_item_exist(slot_tag):
        return

    dpg.delete_item(slot_tag, children_only=True)
    target_id = tracks_data[row].get("target_id")

    dpg.add_spacer(parent=slot_tag, height=SLOT_BUTTON_TOP_SPACER)
    if target_id:
        if target_id in thumbnails_data:
            tex_tag = thumbnails_data[target_id]
            dpg.add_image_button(
                texture_tag=tex_tag,
                width=SLOT_BUTTON_WIDTH,
                height=SLOT_BUTTON_HEIGHT,
                indent=SLOT_BUTTON_INDENT,
                callback=assign_clip_to_track,
                user_data=row,
                parent=slot_tag,
            )
        else:
            dpg.add_button(
                label=f"{target_id[:10]}\n(Waiting...)",
                width=SLOT_BUTTON_WIDTH,
                height=SLOT_BUTTON_HEIGHT,
                indent=SLOT_BUTTON_INDENT,
                callback=assign_clip_to_track,
                user_data=row,
                parent=slot_tag,
            )
    else:
        dpg.add_button(
            label="ASSIGN\nCLIP",
            width=SLOT_BUTTON_WIDTH,
            height=SLOT_BUTTON_HEIGHT,
            indent=SLOT_BUTTON_INDENT,
            callback=assign_clip_to_track,
            user_data=row,
            parent=slot_tag,
        )


def set_step_type(sender: Any, app_data: Any, user_data: Any) -> None:
    row, col, step_type = user_data
    tracks_data[row]["steps"][col]["type"] = step_type
    update_step_ui(row, col)


def toggle_step_active(sender: Any, app_data: Any, user_data: Any) -> None:
    row, col = user_data
    tracks_data[row]["steps"][col]["active"] = app_data
    update_step_theme(row, col)


def update_step_val(sender: Any, app_data: Any, user_data: Any) -> None:
    row, col, param_name = user_data
    if param_name == "color":
        tracks_data[row]["steps"][col][param_name] = app_data[:3]
        # live-refresh the step square when a color is picked from the popup picker
        square_tag = f"color_square_{row}_{col}"
        if dpg.does_item_exist(square_tag):
            dpg.set_value(square_tag, dpg_color_rgba(app_data[:3]))
    else:
        tracks_data[row]["steps"][col][param_name] = app_data


def update_step_theme(row: int, col: int, is_head: bool = False) -> None:
    # Runs on any thread: capture state here, apply the theme on the main thread.
    cell_tag = f"seq_cell_{row}_{col}"
    is_active = tracks_data[row]["steps"][col]["active"]
    ui_task(lambda: _apply_step_theme(cell_tag, is_active, is_head))


def _apply_step_theme(cell_tag: str, is_active: bool, is_head: bool) -> None:
    if not dpg.does_item_exist(cell_tag):
        return
    if is_head:
        dpg.bind_item_theme(cell_tag, theme_cell_play_on if is_active else theme_cell_play_off)
    else:
        dpg.bind_item_theme(cell_tag, theme_cell_on if is_active else theme_cell_off)


def update_step_ui(row: int, col: int) -> None:
    cell_tag = f"seq_cell_{row}_{col}"
    step_data = tracks_data[row]["steps"][col]

    if not dpg.does_item_exist(cell_tag):
        return

    dpg.delete_item(cell_tag, children_only=True)

    with dpg.group(horizontal=True, parent=cell_tag):
        cb = dpg.add_checkbox(
            default_value=step_data["active"], callback=toggle_step_active, user_data=(row, col)
        )
        dpg.add_text(
            step_data["type"] if step_data["type"] != "NONE" else "", color=(200, 200, 200, 255)
        )

        with dpg.popup(cb, mousebutton=dpg.mvMouseButton_Right):
            dpg.add_menu_item(label="Empty", callback=set_step_type, user_data=(row, col, "NONE"))
            dpg.add_separator()
            dpg.add_menu_item(
                label="Alpha Value", callback=set_step_type, user_data=(row, col, "AlphaV")
            )
            dpg.add_menu_item(
                label="Alpha Random", callback=set_step_type, user_data=(row, col, "AlphaR")
            )
            dpg.add_menu_item(
                label="Alpha Fade", callback=set_step_type, user_data=(row, col, "AlphaF")
            )
            dpg.add_separator()
            dpg.add_menu_item(
                label="Color Value", callback=set_step_type, user_data=(row, col, "ColorV")
            )
            dpg.add_menu_item(
                label="Color Random", callback=set_step_type, user_data=(row, col, "ColorR")
            )
            dpg.add_separator()
            dpg.add_menu_item(
                label="Seek Random", callback=set_step_type, user_data=(row, col, "SeekR")
            )

    if step_data["type"] == "AlphaV":
        dpg.add_spacer(parent=cell_tag, height=5)
        dpg.add_drag_float(
            parent=cell_tag,
            width=70,
            default_value=step_data["v1"],
            min_value=0.0,
            max_value=1.0,
            speed=0.01,
            format="%.2f",
            callback=update_step_val,
            user_data=(row, col, "v1"),
        )

    elif step_data["type"] == "AlphaR":
        dpg.add_spacer(parent=cell_tag, height=5)
        dpg.add_text(
            f"{step_data['last_rand_v1']:.2f}",
            color=(150, 255, 150, 255),
            tag=f"rand_v1_{row}_{col}",
            parent=cell_tag,
            indent=20,
        )

    elif step_data["type"] == "AlphaF":
        dpg.add_spacer(parent=cell_tag, height=2)
        # NEW UI: split into two compact rows to fit the intermediate messages
        with dpg.group(horizontal=True, parent=cell_tag):
            dpg.add_drag_float(
                width=34,
                default_value=step_data["v1"],
                min_value=0.0,
                max_value=1.0,
                speed=0.01,
                format="%.1f",
                callback=update_step_val,
                user_data=(row, col, "v1"),
            )
            dpg.add_drag_float(
                width=34,
                default_value=step_data["v2"],
                min_value=0.0,
                max_value=1.0,
                speed=0.01,
                format="%.1f",
                callback=update_step_val,
                user_data=(row, col, "v2"),
            )
        with dpg.group(horizontal=True, parent=cell_tag):
            dpg.add_drag_int(
                width=34,
                default_value=step_data["frames"],
                min_value=1,
                max_value=32,
                speed=1,
                format="%ds",
                callback=update_step_val,
                user_data=(row, col, "frames"),
            )
            dpg.add_drag_int(
                width=34,
                default_value=step_data["msgs"],
                min_value=1,
                max_value=32,
                speed=1,
                format="%dm",
                callback=update_step_val,
                user_data=(row, col, "msgs"),
            )

    elif step_data["type"] == "ColorV":
        dpg.add_spacer(parent=cell_tag, height=6)
        # color is stored normalized (0..1); color_button needs DPG-scale RGBA (0..255)
        btn_tag = f"color_square_{row}_{col}"
        dpg.add_color_button(
            parent=cell_tag,
            default_value=dpg_color_rgba(step_data["color"]),
            no_border=True,
            no_tooltip=True,
            width=STEP_COLOR_SQUARE_SIZE,
            height=STEP_COLOR_SQUARE_SIZE,
            indent=STEP_COLOR_SQUARE_INDENT,
            tag=btn_tag,
        )
        with dpg.popup(btn_tag, mousebutton=dpg.mvMouseButton_Left):
            dpg.add_color_picker(
                default_value=dpg_color_rgba(step_data["color"]),
                no_alpha=True,
                width=200,
                callback=update_step_val,
                user_data=(row, col, "color"),
            )

    elif step_data["type"] == "ColorR":
        dpg.add_spacer(parent=cell_tag, height=6)
        # last_rand_color is stored normalized (0..1); color_button needs DPG-scale RGBA
        dpg.add_color_button(
            parent=cell_tag,
            default_value=dpg_color_rgba(step_data["last_rand_color"]),
            no_border=True,
            no_tooltip=True,
            width=STEP_COLOR_SQUARE_SIZE,
            height=STEP_COLOR_SQUARE_SIZE,
            indent=STEP_COLOR_SQUARE_INDENT,
            tag=f"rand_color_{row}_{col}",
        )

    elif step_data["type"] == "SeekR":
        dpg.add_spacer(parent=cell_tag, height=5)
        dpg.add_text(
            f"{step_data.get('last_rand_seek', 0.0):.2f}",
            color=(150, 200, 255, 255),
            tag=f"rand_seek_{row}_{col}",
            parent=cell_tag,
            indent=20,
        )

    update_step_theme(row, col, is_head=(is_playing and current_step == col))


def regen_thumb_callback(sender: Any, app_data: Any, user_data: Any) -> None:
    target_id = user_data
    if viosc_client:
        msg_addr = f"/viosc/regen_thumb/{target_id}"
        viosc_client.send_message(msg_addr, [])
        append_log("OUT", msg_addr)

    with dpg.mutex():
        if target_id in thumbnails_data:
            thumbnails_data.pop(target_id)
        if dpg.does_item_exist(f"img_{target_id}"):
            dpg.delete_item(f"img_{target_id}")
        if dpg.does_item_exist(f"tex_{target_id}"):
            dpg.delete_item(f"tex_{target_id}")

        container_tag = f"thumb_container_{target_id}"
        loading_tag = f"loading_txt_{target_id}"
        if dpg.does_item_exist(container_tag) and not dpg.does_item_exist(loading_tag):
            dpg.add_text(
                "  [ Rigenero... ]",
                color=(255, 200, 50, 255),
                tag=loading_tag,
                parent=container_tag,
            )
            with dpg.popup(loading_tag, mousebutton=dpg.mvMouseButton_Right):
                dpg.add_menu_item(
                    label="Regenerate Thumbnail (Random)",
                    callback=regen_thumb_callback,
                    user_data=target_id,
                )


def update_vimix_sources_ui(json_string: str) -> None:
    global global_vimix_state, last_ui_signature, last_num_cols
    try:
        payload = json.loads(json_string)
        if not isinstance(payload, dict):
            raise ValueError("replydata payload is not a JSON object")
        sources = payload.get("sources", {})
        if sources is None:
            sources = {}
        if not isinstance(sources, dict):
            raise ValueError("replydata 'sources' is not an object")
        # Drop malformed entries so a single bad source can't crash the UI/main loop
        sources = {k: v for k, v in sources.items() if isinstance(v, dict)}
        global_vimix_state["current_source"] = payload.get("current_source")
        global_vimix_state["sources"] = sources

        # L-1: prune cached state for sources that no longer exist (main thread only),
        # so thumbnails_data / request_timestamps / registry textures do not grow across churn.
        live_ids = set()
        for k, props in sources.items():
            name = props.get("name")
            live_ids.add(str(name) if name else str(k))
        for target_id in list(thumbnails_data):
            if target_id not in live_ids:
                thumbnails_data.pop(target_id)
                tex_tag = f"tex_{target_id}"
                if dpg.does_item_exist(tex_tag):
                    dpg.delete_item(tex_tag)
        for key in list(request_timestamps):
            if key.startswith("thumb_"):
                target_id = key[len("thumb_") :]
                if target_id not in live_ids:
                    request_timestamps.pop(key)

        current_source = global_vimix_state["current_source"]
        data_dict = global_vimix_state["sources"]

        def get_sort_index(k):
            idx_val = data_dict[k].get("index")
            if idx_val is not None:
                try:
                    return int(idx_val)
                except (TypeError, ValueError):
                    pass
            try:
                return int(k)
            except (TypeError, ValueError):
                return 0

        sorted_keys = sorted(data_dict.keys(), key=get_sort_index)
        current_signature = f"cols:{last_num_cols}_" + str(
            [(k, data_dict[k].get("name"), data_dict[k].get("index")) for k in sorted_keys]
        )

        if current_signature != last_ui_signature:
            if dpg.does_item_exist("vimix_table"):
                dpg.delete_item("vimix_table")
            if dpg.does_item_exist("media_grid"):
                dpg.delete_item("media_grid")

            t_raw = dpg.add_table(
                parent="vimix_raw_group",
                tag="vimix_table",
                header_row=True,
                borders_innerH=True,
                borders_innerV=True,
                row_background=True,
                scrollX=True,
                scrollY=True,
                freeze_columns=2,
                height=180,
            )
            for prop in ALL_PROPERTIES:
                dpg.add_table_column(label=prop.capitalize(), parent=t_raw)

            for idx in sorted_keys:
                r_id = dpg.add_table_row(parent=t_raw)
                props_i = data_dict[idx]
                for prop in ALL_PROPERTIES:
                    tag_name = f"raw_{idx}_{prop}"
                    if dpg.does_item_exist(tag_name):
                        dpg.delete_item(tag_name)
                    if dpg.does_alias_exist(tag_name):
                        dpg.remove_alias(tag_name)
                    val = props_i.get(prop)
                    if prop == "index" and val is None:
                        val = idx
                    if isinstance(val, float):
                        val_str = f"{val:.2f}"
                    elif val is None:
                        val_str = "---"
                    else:
                        val_str = str(val)
                    dpg.add_text(val_str, parent=r_id, tag=tag_name)

            num_cols = last_num_cols
            t_grid = dpg.add_table(
                parent="vimix_media_group",
                tag="media_grid",
                header_row=False,
                borders_innerH=False,
                borders_innerV=False,
                policy=dpg.mvTable_SizingFixedFit,
            )
            for _ in range(num_cols):
                dpg.add_table_column(parent=t_grid)

            for i in range(0, len(sorted_keys), num_cols):
                row_indices = sorted_keys[i : i + num_cols]
                r_id = dpg.add_table_row(parent=t_grid)
                for idx in row_indices:
                    name = data_dict[idx].get("name")
                    target_id = str(name) if name else str(idx)
                    tile_tag = f"tile_{target_id}"

                    if dpg.does_item_exist(tile_tag):
                        dpg.delete_item(tile_tag)
                    cw = dpg.add_child_window(
                        parent=r_id, width=135, height=152, border=True, tag=tile_tag
                    )

                    title_tag = f"tile_title_{target_id}"
                    if dpg.does_item_exist(title_tag):
                        dpg.delete_item(title_tag)
                    dpg.add_text(
                        "---", parent=cw, wrap=125, color=(255, 255, 255, 255), tag=title_tag
                    )
                    with dpg.popup(title_tag, mousebutton=dpg.mvMouseButton_Right):
                        dpg.add_menu_item(
                            label="Regenerate Thumbnail (Random)",
                            callback=regen_thumb_callback,
                            user_data=target_id,
                        )

                    dpg.add_spacer(parent=cw, height=4)
                    container_tag = f"thumb_container_{target_id}"
                    if dpg.does_item_exist(container_tag):
                        dpg.delete_item(container_tag)

                    g_id = dpg.add_group(parent=cw, tag=container_tag, indent=4)
                    img_tag = f"img_{target_id}"
                    if target_id in thumbnails_data:
                        tex_tag = thumbnails_data[target_id]
                        if dpg.does_item_exist(img_tag):
                            dpg.delete_item(img_tag)
                        dpg.add_image(
                            texture_tag=tex_tag, parent=g_id, tag=img_tag, width=100, height=62
                        )
                        with dpg.popup(img_tag, mousebutton=dpg.mvMouseButton_Right):
                            dpg.add_menu_item(
                                label="Regenerate Thumbnail (Random)",
                                callback=regen_thumb_callback,
                                user_data=target_id,
                            )
                    else:
                        loading_tag = f"loading_txt_{target_id}"
                        if dpg.does_item_exist(loading_tag):
                            dpg.delete_item(loading_tag)
                        dpg.add_text(
                            " [ Loading... ]",
                            parent=g_id,
                            color=(150, 150, 150, 255),
                            tag=loading_tag,
                        )
                        with dpg.popup(loading_tag, mousebutton=dpg.mvMouseButton_Right):
                            dpg.add_menu_item(
                                label="Regenerate Thumbnail (Random)",
                                callback=regen_thumb_callback,
                                user_data=target_id,
                            )

                    # Compact per-media readout: index + alpha under the photo (e06)
                    index_tag = f"tile_index_{target_id}"
                    if dpg.does_item_exist(index_tag):
                        dpg.delete_item(index_tag)
                    dpg.add_text(
                        "Idx: ---", parent=cw, indent=6, color=(200, 200, 200, 255), tag=index_tag
                    )
                    alpha_tag = f"tile_alpha_{target_id}"
                    if dpg.does_item_exist(alpha_tag):
                        dpg.delete_item(alpha_tag)
                    dpg.add_text(
                        "Alpha: ---",
                        parent=cw,
                        indent=6,
                        color=(200, 230, 200, 255),
                        tag=alpha_tag,
                    )

                for _ in range(num_cols - len(row_indices)):
                    dpg.add_text("", parent=r_id)

            last_ui_signature = current_signature

        for idx in sorted_keys:
            props = data_dict[idx]
            name = props.get("name")
            is_selected = str(idx) == str(current_source)
            target_id = str(name) if name else str(idx)
            display_name = str(name) if name else f"Idx: {idx}"
            tile_tag = f"tile_{target_id}"

            if dpg.does_item_exist(tile_tag):
                dpg.bind_item_theme(
                    tile_tag, theme_selected_clip if is_selected else theme_normal_clip
                )
            if dpg.does_item_exist(f"tile_title_{target_id}"):
                dpg.set_value(f"tile_title_{target_id}", f"{display_name}")
            if dpg.does_item_exist(f"tile_index_{target_id}"):
                idx_val = props.get("index")
                idx_str = str(idx_val) if idx_val is not None else str(idx)
                dpg.set_value(f"tile_index_{target_id}", f"Idx: {idx_str}")
            if dpg.does_item_exist(f"tile_alpha_{target_id}"):
                alpha_val = props.get("alpha")
                alpha_str = f"{alpha_val:.2f}" if isinstance(alpha_val, float) else "---"
                dpg.set_value(f"tile_alpha_{target_id}", f"Alpha: {alpha_str}")

            for prop in ALL_PROPERTIES:
                val = props.get(prop)
                if prop == "index" and val is None:
                    val = idx
                if isinstance(val, float):
                    val_str = f"{val:.2f}"
                elif val is None:
                    val_str = "---"
                else:
                    val_str = str(val)
                txt_tag = f"raw_{idx}_{prop}"
                if dpg.does_item_exist(txt_tag):
                    dpg.set_value(txt_tag, val_str)

    except Exception as e:
        log_error("UI update", str(e))


def thumbnail_decoder_worker() -> None:
    while True:
        name, _, blob_bytes = blob_queue.get()
        try:
            image = Image.open(io.BytesIO(blob_bytes))
            width, height = image.size
            if width * height > MAX_THUMBNAIL_PIXELS:
                raise ValueError(f"thumbnail too large: {width}x{height} px")
            rgba = image.convert("RGBA")
            img_data = np.array(rgba, dtype=np.float32) / 255.0
            texture_queue.put((name, img_data.flatten(), width, height))
        except Exception as e:
            print(f"[viseq Decoder Error] Unable to decode '{name}': {e}")
        blob_queue.task_done()


# ==============================================================================
# MONITOR PLAYERS
# ==============================================================================
monitor_players: list[dict[str, Any]] = []  # each: {"id", "tag", "target_id", "props"}
monitor_player_counter = 0


def get_current_target_id() -> str | None:
    """Return the name (or index) of the source currently selected in vimix."""
    current_source = global_vimix_state.get("current_source")
    if current_source is None:
        return None
    for k, props in global_vimix_state.get("sources", {}).items():
        if str(k) == str(current_source):
            name = props.get("name")
            return str(name) if name else str(k)
    return None


def find_source_by_name(name: str) -> Any:
    for idx, props in global_vimix_state.get("sources", {}).items():
        if str(props.get("name")) == str(name):
            return idx, props
    return None, None


def find_player_index(player_id: int) -> int | None:
    for i, p in enumerate(monitor_players):
        if p["id"] == player_id:
            return i
    return None


def send_monitor_command(player_id: int) -> None:
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = player["target_id"]
    if not target_id:
        return
    addr = f"/viosc/monitor/{target_id}"
    props = player.get("props", [])
    if props:
        osc_client.send_message(addr, list(props))
        append_log("OUT", f"{addr} {props}")
    else:
        osc_client.send_message(addr, [])
        append_log("OUT", f"{addr} (stop)")


def new_monitor_player(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    global monitor_player_counter
    monitor_player_counter += 1
    player_id = monitor_player_counter
    tag = f"monitor_player_{player_id}"
    player = {"id": player_id, "tag": tag, "target_id": None, "props": ["seek"]}
    monitor_players.append(player)
    pos = (
        10 + MONITOR_OFFSET[0] * ((player_id - 1) % 4),
        30 + MONITOR_OFFSET[1] * ((player_id - 1) // 4),
    )
    with dpg.window(label=f"Monitor Player {player_id}", tag=tag, width=270, height=265, pos=pos):
        head_tag = f"mon_head_{player_id}"
        dpg.add_text("Click the box below to assign the current source.", tag=head_tag, wrap=250)
        with dpg.popup(head_tag, mousebutton=dpg.mvMouseButton_Right):
            dpg.add_menu_item(
                label="Monitor Properties...",
                callback=lambda s, a, u: open_monitor_props(player_id),
                user_data=player_id,
            )
            dpg.add_separator()
            dpg.add_menu_item(
                label="Remove Player",
                callback=lambda s, a, u: remove_monitor_player(player_id),
                user_data=player_id,
            )
        dpg.add_spacer(height=6)
        with (
            dpg.child_window(
                width=160, height=120, tag=f"mon_box_{player_id}", border=True, no_scrollbar=True
            ),
            dpg.group(indent=4, tag=f"mon_box_content_{player_id}"),
        ):
            dpg.add_spacer(height=3)
            dpg.add_button(
                label="CLICK TO ASSIGN",
                width=150,
                height=110,
                callback=assign_monitor_player,
                user_data=player_id,
            )
        dpg.add_spacer(height=6)
        with dpg.group(tag=f"mon_vals_{player_id}"):
            dpg.add_text("No monitoring yet.")
        dpg.add_spacer(height=6)
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="Properties...",
                width=120,
                callback=lambda s, a, u: open_monitor_props(player_id),
                user_data=player_id,
            )
            dpg.add_button(
                label="Remove",
                width=90,
                callback=lambda s, a, u: remove_monitor_player(player_id),
                user_data=player_id,
            )


def update_monitor_player_ui(player_id: int) -> None:
    try:
        idx = find_player_index(player_id)
        if idx is None:
            return
        player = monitor_players[idx]
        tag = player["tag"]
        if not dpg.does_item_exist(tag):
            return
        target_id = player["target_id"]
        head = f"mon_head_{player_id}"
        if dpg.does_item_exist(head):
            if target_id:
                dpg.set_value(head, f"Source: {target_id}")
            else:
                dpg.set_value(head, "Click the box below to assign the current source.")
        box = f"mon_box_{player_id}"
        content = f"mon_box_content_{player_id}"
        if dpg.does_item_exist(box):
            if dpg.does_item_exist(content):
                dpg.delete_item(content)
            with dpg.group(parent=box, indent=4, tag=content):
                dpg.add_spacer(height=3)
                if target_id:
                    if target_id in thumbnails_data:
                        dpg.add_image(texture_tag=thumbnails_data[target_id], width=150, height=110)
                    else:
                        dpg.add_text("Loading thumbnail...", color=(150, 150, 150, 255), wrap=140)
                else:
                    dpg.add_button(
                        label="CLICK TO ASSIGN",
                        width=150,
                        height=110,
                        callback=assign_monitor_player,
                        user_data=player_id,
                    )
        rebuild_monitor_player_values(player_id)
    except Exception as e:
        print(f"[viseq Monitor UI] Error updating player {player_id}: {e}")


def rebuild_monitor_player_values(player_id: int) -> None:
    try:
        idx = find_player_index(player_id)
        if idx is None:
            return
        player = monitor_players[idx]
        vals = f"mon_vals_{player_id}"
        if not dpg.does_item_exist(vals):
            return
        dpg.delete_item(vals, children_only=True)
        if not player["target_id"]:
            dpg.add_text("No monitoring yet.", parent=vals)
            return
        props = player.get("props", [])
        if not props:
            dpg.add_text(
                "Monitoring stopped (no properties selected).",
                parent=vals,
                color=(200, 200, 200, 255),
            )
            return
        for prop in props:
            dpg.add_text(f"{prop}: ---", parent=vals, tag=f"mon_val_{player_id}_{prop}", wrap=250)
    except Exception as e:
        print(f"[viseq Monitor UI] Error rebuilding values of player {player_id}: {e}")


def refresh_monitor_player_values(player_id: int) -> None:
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = player["target_id"]
    if not target_id:
        return
    _, props = find_source_by_name(target_id)
    for prop in player.get("props", []):
        t = f"mon_val_{player_id}_{prop}"
        if not dpg.does_item_exist(t):
            continue
        val = props.get(prop) if props else None
        if isinstance(val, float):
            s = f"{val:.3f}"
        elif val is None:
            s = "---"
        else:
            s = str(val)
        dpg.set_value(t, f"{prop}: {s}")


def assign_monitor_player(sender: Any, app_data: Any, user_data: Any) -> None:
    player_id = user_data
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = get_current_target_id()
    if not target_id:
        if dpg.does_item_exist(f"mon_head_{player_id}"):
            dpg.set_value(f"mon_head_{player_id}", "No source selected in the media library.")
        return
    for other in monitor_players:
        if other["id"] != player_id and other.get("target_id") == target_id:
            if dpg.does_item_exist(f"mon_head_{player_id}"):
                dpg.set_value(
                    f"mon_head_{player_id}", f"Already monitored in Player {other['id']}."
                )
            return
    player["target_id"] = target_id
    player["props"] = ["seek"]
    send_monitor_command(player_id)
    update_monitor_player_ui(player_id)


def open_monitor_props(player_id: int) -> None:
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = player["target_id"]
    if not target_id:
        return
    modal_tag = f"mon_props_modal_{player_id}"
    if dpg.does_item_exist(modal_tag):
        dpg.delete_item(modal_tag)
    with dpg.window(
        label=f"Monitor Properties - {target_id}",
        tag=modal_tag,
        modal=True,
        width=270,
        height=400,
        no_resize=True,
    ):
        dpg.add_text("Select the properties to monitor:", wrap=240)
        dpg.add_separator()
        with dpg.child_window(height=310, border=True):
            for prop in ALL_PROPERTIES:
                dpg.add_checkbox(
                    label=prop,
                    default_value=(prop in player["props"]),
                    tag=f"mon_cb_{player_id}_{prop}",
                    callback=on_monitor_prop_toggle,
                    user_data=player_id,
                )


def on_monitor_prop_toggle(sender: Any, app_data: Any, user_data: Any) -> None:
    player_id = user_data
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    new_props = []
    for prop in ALL_PROPERTIES:
        cb_tag = f"mon_cb_{player_id}_{prop}"
        if dpg.does_item_exist(cb_tag) and dpg.get_value(cb_tag):
            new_props.append(prop)
    player["props"] = new_props
    send_monitor_command(player_id)
    rebuild_monitor_player_values(player_id)


def remove_monitor_player(player_id: int) -> None:
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    if player.get("target_id"):
        addr = f"/viosc/monitor/{player['target_id']}"
        osc_client.send_message(addr, [])
        append_log("OUT", f"{addr} (stop)")
    tag = player["tag"]
    if dpg.does_item_exist(tag):
        dpg.delete_item(tag)
    del monitor_players[idx]


def incoming_osc_handler(address: str, *args: Any) -> None:
    append_log("IN ", address)
    try:
        if address == "/viosc/replydata" and args and len(args[0]) <= MAX_STATE_JSON_BYTES:
            ui_state_queue.put(args[0])
        elif (
            address.startswith("/viosc/replythumb/")
            and args
            and len(args[0]) <= MAX_THUMBNAIL_BLOB_BYTES
        ):
            parts = address.split("/")
            blob_queue.put((parts[-2], parts[-1], args[0]))
    except Exception as e:
        log_error("OSC input", str(e))


def start_osc_server(ip: str, port: int) -> bool:
    """Start the local OSC listening server (main thread); True when it is up."""
    global local_osc_server, local_server_thread, is_server_running
    if is_server_running:
        return True
    try:
        disp = dispatcher.Dispatcher()
        disp.set_default_handler(incoming_osc_handler)
        local_osc_server = osc_server.ThreadingOSCUDPServer((ip, port), disp)
        local_server_thread = threading.Thread(target=local_osc_server.serve_forever, daemon=True)
        local_server_thread.start()
        is_server_running = True
        dpg.set_item_label("btn_server_toggle", "Stop Server")
        dpg.set_value("server_status", f"Server Status: Listening on {ip}:{port}")
        return True
    except Exception as e:
        dpg.set_value("server_status", f"Server Status: ERROR ({e})")
        return False


def toggle_local_server() -> None:
    global local_osc_server, local_server_thread, is_server_running
    if is_server_running:
        if local_osc_server and local_server_thread is not None:
            local_osc_server.shutdown()
            local_server_thread.join(timeout=1.0)
            local_osc_server = None
        is_server_running = False
        dpg.set_item_label("btn_server_toggle", "Start Server")
        dpg.set_value("server_status", "Server Status: Stopped")
    else:
        start_osc_server(str(dpg.get_value("listen_ip")), int(dpg.get_value("listen_port")))


def connect_osc_client(ip: str, port: int) -> bool:
    """Create the viOSC client (main thread); True when ready."""
    global viosc_client
    try:
        viosc_client = udp_client.SimpleUDPClient(ip, port)
        dpg.set_value("viosc_status", f"Client Status: Ready on {ip}:{port}")
        return True
    except Exception:
        dpg.set_value("viosc_status", "Client Status: Initialization error")
        return False


def connect_to_viosc() -> None:
    """Connect the OSC client from the viOSC panel inputs (button callback)."""
    connect_osc_client(str(dpg.get_value("viosc_ip")), int(dpg.get_value("viosc_port")))


def autostart_osc() -> None:
    """Boot wiring: auto-connect the viOSC client and start the listening server."""
    connect_osc_client(VIOSC_IP, VIOSC_PORT)
    start_osc_server(VIOSC_IP, VIOSC_LISTEN_PORT)


def on_beat_source(sender: Any, app_data: Any, user_data: Any) -> None:
    """Select the sequencer beat source; exactly one checkbox stays active."""
    global beat_source, current_bpm
    if not app_data:
        dpg.set_value(sender, True)  # a beat source must remain selected
        return
    beat_source = user_data
    for mode in BEAT_SOURCE_LABELS:
        dpg.set_value(f"cb_beat_{mode}", mode == beat_source)
    is_manual = beat_source == BEAT_SOURCE_MANUAL
    dpg.configure_item("manual_bpm_input", show=is_manual)
    dpg.configure_item("btn_tap", show=is_manual)
    if is_manual:
        current_bpm = float(dpg.get_value("manual_bpm_input"))
        dpg.set_value("testo_bpm", f"BPM: {current_bpm:.1f}")
        dpg.set_value("manual_bpm_text", f"{current_bpm:.0f} BPM")


def on_manual_bpm(sender: Any, app_data: Any, user_data: Any) -> None:
    """Set the sequencer BPM from the manual numeric input."""
    global current_bpm
    current_bpm = float(app_data)
    dpg.set_value("testo_bpm", f"BPM: {current_bpm:.1f}")
    dpg.set_value("manual_bpm_text", f"{current_bpm:.0f} BPM")


def tap_bpm(sender: Any, app_data: Any, user_data: Any) -> None:
    """Set the BPM from the average interval of the last taps (manual mode)."""
    global current_bpm
    now = time.time()
    if tap_times and now - tap_times[-1] > 2.0:
        tap_times.clear()  # stale tap starts a new sequence
    tap_times.append(now)
    del tap_times[:-8]  # keep the most recent taps
    if len(tap_times) >= 2:
        intervals = [tap_times[i + 1] - tap_times[i] for i in range(len(tap_times) - 1)]
        bpm = 60.0 / (sum(intervals) / len(intervals))
        current_bpm = round(bpm, 2)
        dpg.set_value("manual_bpm_input", round(current_bpm))
        dpg.set_value("testo_bpm", f"BPM: {current_bpm:.1f}")
        dpg.set_value("manual_bpm_text", f"{current_bpm:.0f} BPM")


def midi_beats_from_pulses(pulses: int) -> int:
    """Whole quarter-note beats contained in a MIDI clock pulse count (24 pulses/beat)."""
    return pulses // MIDI_CLOCK_PULSES_PER_BEAT


def midi_clock_loop() -> None:
    """Listen for MIDI clock (0xF8, 24 pulses/beat) and fire the sequencer beat in MIDI mode.

    Opens the first available MIDI input; without any port (or backend) it logs once and
    idles so the app keeps running.
    """
    global midi_pulses
    try:
        import mido

        ports = mido.get_input_names()
        if not ports:
            log_error("MIDI", "no MIDI input port available")
            while True:
                time.sleep(10)
        with mido.open_input(ports[0]) as port:
            append_log("MIDI", f"Listening on {ports[0]}")
            while True:
                for msg in port.iter_pending():
                    if msg.type == "clock":
                        midi_pulses += 1
                        if midi_pulses >= MIDI_CLOCK_PULSES_PER_BEAT:
                            midi_pulses = 0
                            flash_led("led_midi")
                            if beat_source == BEAT_SOURCE_MIDI and is_playing:
                                sync_event_beat.set()
                time.sleep(0.001)
    except Exception as e:
        log_error("MIDI", str(e))
        while True:
            time.sleep(10)


def show_settings_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the general settings window from the top menubar."""
    dpg.show_item("settings_window")


def show_logs_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the OSC logs window from the top menubar (Show > Logs)."""
    dpg.show_item("logs_window")


def callback_resync() -> None:
    global current_step
    current_step = -1
    for r in range(NUM_TRACKS):
        tracks_data[r]["active_fade"]["active"] = False
    sync_event_seq.set()
    sync_event_led.set()


def callback_nudge_backward() -> None:
    global phase_nudge
    phase_nudge += 0.05


def callback_nudge_forward() -> None:
    global phase_nudge
    phase_nudge -= 0.05


def audio_callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
    global audio_buffer, audio_buffer_head
    if status:
        print(status)
    samples = indata[:, 0].astype(np.float32)
    # L-2 ring-buffer write: in-place, modulo indexing, no full-buffer reallocation
    # (np.roll allocated a fresh ~1 MB array ~43x/s on every callback).
    n = len(samples)
    if n >= len(audio_buffer):  # defensive: block larger than the buffer
        audio_buffer[:] = samples[-len(audio_buffer) :]
        audio_buffer_head = 0
    else:
        end = audio_buffer_head + n
        if end <= len(audio_buffer):
            audio_buffer[audio_buffer_head:end] = samples
        else:
            split = len(audio_buffer) - audio_buffer_head
            audio_buffer[audio_buffer_head:] = samples[:split]
            audio_buffer[: n - split] = samples[split:]
        audio_buffer_head = end % len(audio_buffer)


def get_audio_snapshot() -> np.ndarray:
    """Chronological copy of the last len(audio_buffer) samples (newest at tail).

    Linearizes the ring buffer for the BPM thread. Called once per second (not per
    audio callback), so this allocation is acceptable.
    """
    head = audio_buffer_head
    if head == 0:
        return audio_buffer.copy()
    return np.concatenate((audio_buffer[head:], audio_buffer[:head]))


def compute_spectrum_bars(samples: np.ndarray, n_bars: int = SPECTRUM_BARS) -> np.ndarray:
    """Magnitude spectrum of the latest samples, binned into n_bars levels (0..1).

    Hann-windowed rfft, dB scale with a -SPECTRUM_DB_FLOOR floor; a full-scale sine
    reaches ~1.0, silence ~0.0.
    """
    if samples.size < SPECTRUM_FFT_SIZE:
        samples = np.pad(samples, (0, SPECTRUM_FFT_SIZE - samples.size))
    frame = samples[-SPECTRUM_FFT_SIZE:] * np.hanning(SPECTRUM_FFT_SIZE)
    mag = np.abs(np.fft.rfft(frame))[1:]  # drop DC
    # max per bar: averaging in dB would drown a narrow peak among quiet bins
    mag_bins = np.array_split(mag, n_bars)
    db = 20.0 * np.log10(
        np.array([np.max(b) for b in mag_bins]) / (SPECTRUM_FFT_SIZE / 4.0) + 1e-12
    )
    levels = (db + SPECTRUM_DB_FLOOR) / SPECTRUM_DB_FLOOR
    return np.clip(levels, 0.0, 1.0)


def band_value_from_bars(
    bars: np.ndarray,
    start: float,
    end: float,
    min_level: float = 0.0,
    max_level: float = 1.0,
) -> float:
    """Mean fill (0..1) of the selection rectangle over the bars.

    The horizontal window [start, end) picks the bars; the vertical window
    [min_level, max_level] maps each bar's level so 0 = at/below min and
    1 = at/above max. An inverted/empty level window falls back to the plain
    bar mean (backward compatible with the frequency-only usage).
    """
    if bars.size == 0:
        return 0.0
    n = bars.size
    lo = round(start * n)
    hi = round(end * n)
    if hi <= lo:  # inverted/degenerate horizontal selection -> at least one bar
        hi = lo + 1
    lo = max(0, min(lo, n - 1))
    hi = max(lo + 1, min(hi, n))
    selected = bars[lo:hi]
    if max_level <= min_level:
        return float(np.mean(selected))
    mapped = np.clip((selected - min_level) / (max_level - min_level), 0.0, 1.0)
    return float(np.mean(mapped))


def _set_band_variable(band_id: int, value: float) -> None:
    """Store a band level into its module variable (band1/band2/band3)."""
    global band1, band2, band3
    if band_id == 1:
        band1 = value
    elif band_id == 2:
        band2 = value
    else:
        band3 = value


def refresh_band_value(bars: np.ndarray, band_id: int) -> None:
    """Recompute one band's level from its sliders; update text and overlay (main thread)."""
    if not bands_enabled[band_id]:
        return
    f_start = float(dpg.get_value(f"band{band_id}_start"))
    f_end = float(dpg.get_value(f"band{band_id}_end"))
    l_min = float(dpg.get_value(f"band{band_id}_min"))
    l_max = float(dpg.get_value(f"band{band_id}_max"))
    value = band_value_from_bars(bars, f_start, f_end, l_min, l_max)
    _set_band_variable(band_id, value)
    # Beat trigger: any band rising to >= 1.0 flashes its LED; only the selected band mode
    # fires the sequencer beat event (edge only)
    band_source = {1: BEAT_SOURCE_BAND1, 2: BEAT_SOURCE_BAND2, 3: BEAT_SOURCE_BAND3}[band_id]
    if value >= 1.0 and band_prev_values[band_id] < 1.0:
        flash_led(f"led_band{band_id}")
        if beat_source == band_source:
            sync_event_beat.set()
    band_prev_values[band_id] = value
    dpg.set_value(f"band{band_id}_value_text", f"{value:.2f}")
    dpg.configure_item(
        f"band{band_id}_rect",
        pmin=(f_start * SPEC_DRAWLIST_W, (1 - l_max) * SPEC_DRAWLIST_H),
        pmax=(f_end * SPEC_DRAWLIST_W, (1 - l_min) * SPEC_DRAWLIST_H),
        show=True,
    )


def refresh_bands(bars: np.ndarray) -> None:
    """Refresh every enabled band (disabled bands stay 0 and hidden)."""
    for band_id in bands_enabled:
        if bands_enabled[band_id]:
            refresh_band_value(bars, band_id)


def update_spectrum_ui(bars: np.ndarray) -> None:
    """Redraw the spectrum bars and refresh the enabled bands (main thread)."""
    global spectrum_bars_cache
    n = len(bars)
    bw = SPEC_DRAWLIST_W / n
    for i, level in enumerate(bars):
        h = level * (SPEC_DRAWLIST_H - 4)
        dpg.configure_item(
            f"spec_bar_{i}",
            pmin=(i * bw + 1, SPEC_DRAWLIST_H - h),
            pmax=((i + 1) * bw - 1, SPEC_DRAWLIST_H - 2),
        )
    spectrum_bars_cache = bars
    refresh_bands(bars)


def on_band_enable(sender: Any, app_data: Any, user_data: Any) -> None:
    """Show/hide a band's overlay and (re)compute it when the checkbox toggles."""
    band_id = int(user_data)
    bands_enabled[band_id] = bool(app_data)
    if bands_enabled[band_id]:
        refresh_band_value(spectrum_bars_cache, band_id)
    else:
        _set_band_variable(band_id, 0.0)
        dpg.set_value(f"band{band_id}_value_text", "—")
        dpg.configure_item(f"band{band_id}_rect", show=False)


def on_band_change(sender: Any, app_data: Any, user_data: Any) -> None:
    """Refresh a band when its selection sliders move."""
    refresh_band_value(spectrum_bars_cache, int(user_data))


def spectrum_analyzer_loop() -> None:
    """Compute the spectrum ~30x/s and enqueue the main-thread redraw (HIGH-1)."""
    while True:
        if is_audio_analyzing:
            try:
                bars = compute_spectrum_bars(get_audio_snapshot())
                ui_task(partial(update_spectrum_ui, bars))
            except Exception as e:
                log_error("Spectrum", str(e))
        time.sleep(1.0 / SPECTRUM_FPS)


def essentia_analyzer_loop() -> None:
    global current_bpm, beat_confidence
    last_error = ""
    while True:
        if is_beat_tracking and beat_source == BEAT_SOURCE_ANALYSIS:
            try:
                audio_slice = essentia.array(get_audio_snapshot())
                if np.max(np.abs(audio_slice)) > 0.005:
                    if lowpass_enabled:
                        audio_slice = lowpass_filter(audio_slice)
                    bpm, _, confidence, _, _ = rhythm_extractor(audio_slice)
                    if confidence > 0.2 or beat_confidence == 0.0:
                        current_bpm = float(bpm)
                        beat_confidence = float(confidence)
                        enqueue_set_value(
                            "testo_bpm", f"BPM: {current_bpm:.1f} (Conf: {beat_confidence:.2f})"
                        )
            except Exception as e:
                # Log each distinct failure once, not every second
                err = f"{type(e).__name__}: {e}"
                if err != last_error:
                    last_error = err
                    log_error("BPM analysis", err)
        time.sleep(1.0)


def visual_metronome_loop() -> None:
    global phase_nudge
    while True:
        if is_beat_tracking and current_bpm > 0 and not is_playing:
            base_sleep = 60.0 / current_bpm
            actual_sleep = max(0.0, base_sleep + phase_nudge)
            flash_led(BEAT_LED_TAGS[beat_source])
            sync_event_led.wait(actual_sleep)
            if sync_event_led.is_set():
                sync_event_led.clear()
            phase_nudge = 0.0
        else:
            time.sleep(0.1)


# ==============================================================================
# NEW ASYNC THREAD FOR HIGH-RESOLUTION FADES
# ==============================================================================
def fade_tick_loop() -> None:
    while True:
        if is_playing:
            current_time = time.time()
            for track in tracks_data:
                fade = track.get("active_fade", {})
                if fade and fade.get("active"):
                    elapsed = current_time - fade["start_time"]
                    expected_msg_index = int(elapsed / fade["msg_interval"])

                    # If we fell behind, or it is time for the next tick
                    if expected_msg_index > fade["last_msg_index"]:
                        max_msg = min(expected_msg_index, fade["total_msgs"] - 1)

                        # Send all the accumulated intermediate messages
                        for i in range(fade["last_msg_index"] + 1, max_msg + 1):
                            progress = (
                                i / float(fade["total_msgs"] - 1) if fade["total_msgs"] > 1 else 1.0
                            )
                            val = (
                                fade["start_val"] + (fade["end_val"] - fade["start_val"]) * progress
                            )
                            try:
                                osc_client.send_message(fade["address"], float(val))
                                append_log("OUT", f"{fade['address']} [FADE: {val:.2f}]")
                            except Exception:
                                pass

                        fade["last_msg_index"] = max_msg

                        # Deactivate when the fade is finished
                        if fade["last_msg_index"] >= fade["total_msgs"] - 1:
                            fade["active"] = False
        time.sleep(0.01)  # 100 FPS check loop for smooth fades


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


def beat_is_event_driven() -> bool:
    """True when the beat comes from an event (band peak / MIDI clock), not a fixed interval."""
    return beat_source in (
        BEAT_SOURCE_BAND1,
        BEAT_SOURCE_BAND2,
        BEAT_SOURCE_BAND3,
        BEAT_SOURCE_MIDI,
    )


def sequencer_tick() -> None:
    global current_step, phase_nudge
    while True:
        if is_playing:
            if beat_is_event_driven():
                # Band/MIDI modes: wait for the beat event instead of a fixed interval
                sync_event_beat.wait()
                sync_event_beat.clear()
                phase_nudge = 0.0
            else:
                base_sleep = 60.0 / current_bpm if current_bpm > 0 else 0.5
                actual_sleep = max(0.0, base_sleep + phase_nudge)
                phase_nudge = 0.0
                sync_event_seq.wait(actual_sleep)
                if sync_event_seq.is_set():
                    sync_event_seq.clear()

            prev_step = current_step
            current_step = (current_step + 1) % NUM_STEPS

            for r, track in enumerate(tracks_data):
                if prev_step != -1:
                    update_step_theme(r, prev_step, is_head=False)
                update_step_theme(r, current_step, is_head=True)

                step_data = track["steps"][current_step]

                if step_data["active"]:
                    # A new step cancels any pending fade unless it starts its own (audit HIGH-2)
                    track["active_fade"]["active"] = False
                    base_addr = track["base_address"]
                    if base_addr and base_addr.strip():
                        try:
                            if step_data["type"] == "AlphaV":
                                target_addr = f"{base_addr}/alpha"
                                osc_client.send_message(target_addr, float(step_data["v1"]))
                                append_log("OUT", f"{target_addr} [{step_data['v1']:.2f}]")

                            elif step_data["type"] == "AlphaR":
                                target_addr = f"{base_addr}/alpha"
                                rand_val = random.uniform(0.0, 1.0)
                                osc_client.send_message(target_addr, float(rand_val))
                                append_log("OUT", f"{target_addr} [{rand_val:.2f}]")

                                step_data["last_rand_v1"] = rand_val
                                tag_v1 = f"rand_v1_{r}_{current_step}"
                                enqueue_set_value(tag_v1, f"{rand_val:.2f}")

                            elif step_data["type"] == "AlphaF":
                                target_addr = f"{base_addr}/alpha"
                                total_msgs = step_data["frames"] * step_data["msgs"]

                                # Start the asynchronous state machine
                                track["active_fade"] = {
                                    "active": True,
                                    "address": target_addr,
                                    "start_val": step_data["v1"],
                                    "end_val": step_data["v2"],
                                    "total_msgs": total_msgs,
                                    "msg_interval": base_sleep / step_data["msgs"]
                                    if step_data["msgs"] > 0
                                    else base_sleep,
                                    "start_time": time.time(),
                                    "last_msg_index": 0,
                                }
                                # The sequencer sends the FIRST value immediately
                                osc_client.send_message(target_addr, float(step_data["v1"]))
                                append_log(
                                    "OUT", f"{target_addr} [FADE START: {step_data['v1']:.2f}]"
                                )

                            elif step_data["type"] == "ColorV":
                                send_colorv_step(track, r, current_step)

                            elif step_data["type"] == "ColorR":
                                send_colorr_step(track, r, current_step)

                            elif step_data["type"] == "SeekR":
                                send_seekr_step(track, r, current_step)

                        except Exception as e:
                            print(f"[viseq OSC Error] {e}")

            flash_led(BEAT_LED_TAGS[beat_source])
        else:
            time.sleep(0.1)


def on_lowpass_toggle(sender: Any, app_data: Any, user_data: Any) -> None:
    global lowpass_enabled
    lowpass_enabled = bool(app_data)


def toggle_audio_stream(sender: Any, app_data: Any, user_data: Any) -> None:
    global audio_stream, is_audio_analyzing, is_beat_tracking
    if user_data == "vu_meter":
        is_audio_analyzing = app_data
    elif user_data == "beat_tracking":
        is_beat_tracking = app_data
    needs_stream = is_audio_analyzing or is_beat_tracking

    if needs_stream and audio_stream is None:
        device_string = dpg.get_value("combo_devices")
        if "No input device" in device_string:
            dpg.set_value(sender, False)
            return
        device_id = int(device_string.split(":")[0])
        try:
            audio_stream = sd.InputStream(
                device=device_id,
                channels=1,
                samplerate=samplerate,
                dtype=np.float32,
                callback=audio_callback,
            )
            audio_stream.start()
        except Exception:
            dpg.set_value(sender, False)
    elif not needs_stream and audio_stream is not None:
        audio_stream.stop()
        audio_stream.close()
        audio_stream = None
        dpg.set_value("testo_bpm", "BPM: ---")


def toggle_play() -> None:
    global is_playing, current_step
    is_playing = not is_playing
    if not is_playing:
        for r in range(NUM_TRACKS):
            for c in range(NUM_STEPS):
                update_step_theme(r, c, is_head=False)
            tracks_data[r]["active_fade"]["active"] = False
        current_step = -1
        dpg.set_item_label("btn_play", "PLAY")
    else:
        dpg.set_item_label("btn_play", "STOP")
        sync_event_seq.set()


dpg.create_context()

with dpg.texture_registry(tag="texture_registry"):
    pass

with dpg.theme() as theme_selected_clip, dpg.theme_component(dpg.mvChildWindow):
    dpg.add_theme_color(dpg.mvThemeCol_Border, (50, 255, 50, 255))
    dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (30, 80, 30, 255))
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_normal_clip, dpg.theme_component(dpg.mvChildWindow):
    dpg.add_theme_color(dpg.mvThemeCol_Border, (80, 80, 80, 255))
    dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (40, 40, 40, 255))
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_compact_table, dpg.theme_component(dpg.mvTable):
    dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 1, 1)

with dpg.theme() as theme_cell_off, dpg.theme_component(dpg.mvChildWindow):
    dpg.add_theme_color(dpg.mvThemeCol_Border, (80, 80, 80, 255))
    dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (40, 40, 40, 255))
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_cell_on, dpg.theme_component(dpg.mvChildWindow):
    dpg.add_theme_color(dpg.mvThemeCol_Border, (50, 255, 50, 255))
    dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (30, 80, 30, 255))
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_cell_play_off, dpg.theme_component(dpg.mvChildWindow):
    dpg.add_theme_color(dpg.mvThemeCol_Border, (255, 255, 255, 255))
    dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (80, 80, 80, 255))
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_cell_play_on, dpg.theme_component(dpg.mvChildWindow):
    dpg.add_theme_color(dpg.mvThemeCol_Border, (255, 255, 255, 255))
    dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (80, 220, 80, 255))
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_slot_clear, dpg.theme_component(dpg.mvChildWindow):
    # borderless clip slot: no frame, no background (border=False + transparent ChildBg)
    dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (0, 0, 0, 0))


# WINDOW 1: SEQUENCER
with dpg.window(label="Step Sequencer", width=1050, height=800, pos=(10, 10), no_close=True):
    with dpg.group(horizontal=True):
        dpg.add_button(label="PLAY", tag="btn_play", callback=toggle_play, width=100, height=28)
        dpg.add_spacer(width=14)
        dpg.add_button(label="<", callback=callback_nudge_backward, width=36, height=28)
        dpg.add_button(label="RESYNC", callback=callback_resync, width=72, height=28)
        dpg.add_button(label=">", callback=callback_nudge_forward, width=36, height=28)
        dpg.add_spacer(width=14)
        # Beat source line 1: BPM detection (with its BPM readout) + bands 1-2
        dpg.add_checkbox(
            label="Rilevazione BPM",
            tag="cb_beat_bpm_analysis",
            default_value=True,
            callback=on_beat_source,
            user_data=BEAT_SOURCE_ANALYSIS,
        )
        with dpg.drawlist(width=14, height=14):
            dpg.draw_circle(
                center=[7, 7],
                radius=5,
                color=(0, 0, 0, 255),
                fill=(50, 50, 50, 255),
                tag="led_analysis",
            )
        dpg.add_text("BPM: ---", tag="testo_bpm")
        dpg.add_spacer(width=10)
        for mode, label in ((BEAT_SOURCE_BAND1, "Band 1"), (BEAT_SOURCE_BAND2, "Band 2")):
            dpg.add_checkbox(
                label=label,
                tag=f"cb_beat_{mode}",
                callback=on_beat_source,
                user_data=mode,
            )
            with dpg.drawlist(width=14, height=14):
                dpg.draw_circle(
                    center=[7, 7],
                    radius=5,
                    color=(0, 0, 0, 255),
                    fill=(50, 50, 50, 255),
                    tag=BEAT_LED_TAGS[mode],
                )
            dpg.add_spacer(width=10)

    # Beat source line 2: band 3 + MIDI + manual (with the numeric input and TAP).
    # The leading spacer aligns it under line 1 (transport width: PLAY+sp+<+RESYNC+>+sp).
    with dpg.group(horizontal=True):
        dpg.add_spacer(width=SEQ_TRANSPORT_WIDTH)
        for mode, label in ((BEAT_SOURCE_BAND3, "Band 3"), (BEAT_SOURCE_MIDI, "MIDI Sync")):
            dpg.add_checkbox(
                label=label,
                tag=f"cb_beat_{mode}",
                callback=on_beat_source,
                user_data=mode,
            )
            with dpg.drawlist(width=14, height=14):
                dpg.draw_circle(
                    center=[7, 7],
                    radius=5,
                    color=(0, 0, 0, 255),
                    fill=(50, 50, 50, 255),
                    tag=BEAT_LED_TAGS[mode],
                )
            dpg.add_spacer(width=10)
        dpg.add_checkbox(
            label="BPM Manuale",
            tag="cb_beat_manual_bpm",
            callback=on_beat_source,
            user_data=BEAT_SOURCE_MANUAL,
        )
        with dpg.drawlist(width=14, height=14):
            dpg.draw_circle(
                center=[7, 7],
                radius=5,
                color=(0, 0, 0, 255),
                fill=(50, 50, 50, 255),
                tag="led_manual",
            )
        dpg.add_spacer(width=6)
        dpg.add_input_int(
            default_value=120,
            min_value=30,
            max_value=300,
            width=84,
            tag="manual_bpm_input",
            callback=on_manual_bpm,
            show=False,
        )
        dpg.add_button(
            label="TAP", tag="btn_tap", callback=tap_bpm, width=36, height=22, show=False
        )
        dpg.add_text("", tag="manual_bpm_text", color=(150, 255, 150, 255))

    dpg.add_spacer(height=10)

    with dpg.table(
        header_row=False,
        borders_innerH=False,
        borders_innerV=False,
        borders_outerH=False,
        borders_outerV=False,
        scrollX=True,
        scrollY=True,
        policy=dpg.mvTable_SizingFixedFit,
        tag="seq_table",
    ):
        for _ in range(NUM_STEPS):
            dpg.add_table_column(width_fixed=True, init_width_or_weight=90)
        dpg.add_table_column(width_fixed=True, init_width_or_weight=SLOT_WIDTH)

        for row in range(NUM_TRACKS):
            with dpg.table_row():
                # THE 8 PADS
                for step in range(NUM_STEPS):
                    cell_tag = f"seq_cell_{row}_{step}"
                    with dpg.child_window(
                        width=STEP_CELL_SIZE, height=STEP_CELL_SIZE, tag=cell_tag, no_scrollbar=True
                    ):
                        pass
                    update_step_ui(row, step)

                # ASSIGNABLE CLIP SLOT (bare centered button, no table frame)
                with dpg.child_window(
                    width=SLOT_WIDTH,
                    height=SLOT_HEIGHT,
                    border=False,
                    tag=f"seq_slot_{row}",
                    no_scrollbar=True,
                ):
                    pass
                dpg.bind_item_theme(f"seq_slot_{row}", theme_slot_clear)
                update_track_slot_ui(row)

    dpg.bind_item_theme("seq_table", theme_compact_table)

# WINDOW 2: AUDIO ANALYZER
input_devices_list = get_input_devices()

with dpg.window(label="Audio analyzer", width=350, height=272, pos=(10, 806), no_close=True):
    dpg.add_combo(
        items=input_devices_list, default_value=input_devices_list[0], tag="combo_devices", width=-1
    )
    dpg.add_checkbox(
        label="Enable Level Analysis (Spectrum)",
        callback=toggle_audio_stream,
        user_data="vu_meter",
    )
    dpg.add_spacer(height=2)
    with dpg.drawlist(width=SPEC_DRAWLIST_W, height=SPEC_DRAWLIST_H, tag="spec_drawlist"):
        for i in range(SPECTRUM_BARS):
            dpg.draw_rectangle(
                pmin=(i * (SPEC_DRAWLIST_W / SPECTRUM_BARS) + 1, SPEC_DRAWLIST_H - 2),
                pmax=((i + 1) * (SPEC_DRAWLIST_W / SPECTRUM_BARS) - 1, SPEC_DRAWLIST_H - 2),
                color=(0, 0, 0, 0),
                fill=SPECTRUM_BAR_COLOR,
                tag=f"spec_bar_{i}",
            )
        for band_id, (fill, edge) in BAND_RECT_COLORS.items():
            dpg.draw_rectangle(
                pmin=(0, 2),
                pmax=(SPEC_DRAWLIST_W, SPEC_DRAWLIST_H - 2),
                color=edge,
                fill=fill,
                tag=f"band{band_id}_rect",
                show=False,
            )
    for band_id, (start_default, end_default) in BAND_DEFAULT_RANGES.items():
        with dpg.group(horizontal=True):
            dpg.add_checkbox(
                label=f"Band {band_id}",
                tag=f"band{band_id}_enabled",
                callback=on_band_enable,
                user_data=band_id,
            )
            dpg.add_text("F", color=(200, 200, 200, 255))
            dpg.add_drag_float(
                default_value=start_default,
                min_value=0.0,
                max_value=0.99,
                speed=0.005,
                format="%.2f",
                width=44,
                tag=f"band{band_id}_start",
                callback=on_band_change,
                user_data=band_id,
            )
            dpg.add_drag_float(
                default_value=end_default,
                min_value=0.01,
                max_value=1.0,
                speed=0.005,
                format="%.2f",
                width=44,
                tag=f"band{band_id}_end",
                callback=on_band_change,
                user_data=band_id,
            )
            dpg.add_text("L", color=(200, 200, 200, 255))
            dpg.add_drag_float(
                default_value=0.0,
                min_value=0.0,
                max_value=1.0,
                speed=0.005,
                format="%.2f",
                width=44,
                tag=f"band{band_id}_min",
                callback=on_band_change,
                user_data=band_id,
            )
            dpg.add_drag_float(
                default_value=1.0,
                min_value=0.0,
                max_value=1.0,
                speed=0.005,
                format="%.2f",
                width=44,
                tag=f"band{band_id}_max",
                callback=on_band_change,
                user_data=band_id,
            )
            dpg.add_text("—", tag=f"band{band_id}_value_text", color=(230, 230, 120, 255))
    with dpg.group(horizontal=True):
        dpg.add_checkbox(
            label="Enable BPM Analysis (Essentia)",
            callback=toggle_audio_stream,
            user_data="beat_tracking",
        )
    dpg.add_checkbox(
        label="Use Low-Pass Filter (kick only)",
        default_value=True,
        tag="cb_lowpass",
        callback=on_lowpass_toggle,
    )

# WINDOW 3: SETTINGS (hidden; opened from the menubar "Settings" entry)
with dpg.window(
    label="Settings", width=340, height=320, pos=(370, 820), tag="settings_window", show=False
):
    dpg.add_text("OSC", color=(200, 200, 200, 255))
    dpg.add_separator()
    dpg.add_text("1. Setup Client (to viOSC):")
    with dpg.group(horizontal=True):
        dpg.add_input_text(default_value="127.0.0.1", tag="viosc_ip", width=120)
        dpg.add_input_int(default_value=6666, tag="viosc_port", width=80, step=0)
        dpg.add_button(label="Connect Client", callback=connect_to_viosc)
    dpg.add_text("Client Status: Waiting", tag="viosc_status", color=(150, 150, 150, 255))
    dpg.add_separator()
    dpg.add_spacer(height=5)
    dpg.add_text("2. Setup Server (Listening):")
    with dpg.group(horizontal=True):
        dpg.add_input_text(default_value="127.0.0.1", tag="listen_ip", width=120)
        dpg.add_input_int(default_value=VIOSC_LISTEN_PORT, tag="listen_port", width=80, step=0)
        dpg.add_button(label="Start Server", tag="btn_server_toggle", callback=toggle_local_server)
    dpg.add_text("Server Status: Stopped", tag="server_status", color=(150, 150, 150, 255))
    dpg.add_separator()
    dpg.add_spacer(height=5)

    with dpg.group(tag="vimix_raw_group"):
        pass

# WINDOW 4: VIMIX MEDIA
with (
    dpg.window(
        label="Mediagrid",
        width=550,
        height=690,
        pos=(1100, 10),
        no_close=True,
        tag="vimix_media_window",
    ),
    dpg.group(tag="vimix_media_group"),
):
    pass

# WINDOW 5: OSC LOGS (hidden; opened from the menubar "Show" > "Logs")
with dpg.window(
    label="OSC Logs", width=950, height=150, pos=(720, 820), tag="logs_window", show=False
):
    dpg.add_text("Waiting for OSC traffic...", tag="osc_log_text")

# NEW THREAD FOR HIGH-FREQUENCY FADES
threading.Thread(target=fade_tick_loop, daemon=True).start()
threading.Thread(target=spectrum_analyzer_loop, daemon=True).start()
threading.Thread(target=midi_clock_loop, daemon=True).start()

threading.Thread(target=sequencer_tick, daemon=True).start()
threading.Thread(target=visual_metronome_loop, daemon=True).start()
threading.Thread(target=essentia_analyzer_loop, daemon=True).start()
threading.Thread(target=thumbnail_decoder_worker, daemon=True).start()

dpg.create_viewport(title="viseq - Audio-Reactive VJ Controller", width=1700, height=1080)
with dpg.viewport_menu_bar():
    with dpg.menu(label="Monitor"):
        dpg.add_menu_item(label="New Monitor Player", callback=new_monitor_player)
    with dpg.menu(label="Show"):
        dpg.add_menu_item(label="Logs", callback=show_logs_window)
    dpg.add_menu_item(label="Settings", callback=show_settings_window)
dpg.setup_dearpygui()
dpg.show_viewport()
autostart_osc()  # boot: auto-connect OSC client + start listening server (no manual clicks)

try:
    while dpg.is_dearpygui_running():
        if dpg.does_item_exist("vimix_media_window"):
            w = dpg.get_item_width("vimix_media_window")
            current_cols = max(1, int((w - 20) / 145))
            if current_cols != last_num_cols:
                last_num_cols = current_cols
                if global_vimix_state.get("sources"):
                    update_vimix_sources_ui(json.dumps(global_vimix_state))

        has_new_logs = False
        while not log_queue.empty():
            osc_log_history.append(log_queue.get())
            has_new_logs = True

        if has_new_logs:
            if len(osc_log_history) > LOG_HISTORY_LIMIT:
                del osc_log_history[:-LOG_HISTORY_LIMIT]
            if dpg.does_item_exist("osc_log_text"):
                dpg.set_value("osc_log_text", format_osc_log(osc_log_history))

        # Run queued UI mutations from worker threads on the main thread (audit HIGH-1)
        while not ui_task_queue.empty():
            task = ui_task_queue.get()
            try:
                task()
            except Exception as e:
                log_error("UI task", str(e))

        latest_json = None
        while not ui_state_queue.empty():
            latest_json = ui_state_queue.get()

        if latest_json:
            update_vimix_sources_ui(latest_json)

        while not texture_queue.empty():
            name, img_data, w, h = texture_queue.get()
            target_id = name
            tex_tag = f"tex_{target_id}"
            img_tag = f"img_{target_id}"
            container_tag = f"thumb_container_{target_id}"
            loading_tag = f"loading_txt_{target_id}"

            if dpg.does_item_exist(tex_tag):
                dpg.delete_item(tex_tag)

            dpg.add_static_texture(
                width=w, height=h, default_value=img_data, tag=tex_tag, parent="texture_registry"
            )

            thumbnails_data[target_id] = tex_tag

            if not dpg.does_item_exist(img_tag) and dpg.does_item_exist(container_tag):
                if dpg.does_item_exist(loading_tag):
                    dpg.delete_item(loading_tag)
                dpg.add_image(
                    texture_tag=tex_tag, tag=img_tag, width=100, height=62, parent=container_tag
                )

                with dpg.popup(img_tag, mousebutton=dpg.mvMouseButton_Right):
                    dpg.add_menu_item(
                        label="Regenerate Thumbnail (Random)",
                        callback=regen_thumb_callback,
                        user_data=target_id,
                    )

            for r, track in enumerate(tracks_data):
                if track.get("target_id") == target_id:
                    update_track_slot_ui(r)
            for p in monitor_players:
                if p.get("target_id") == target_id:
                    update_monitor_player_ui(p["id"])

        if viosc_client:
            current_time = time.time()
            for idx, props in global_vimix_state.get("sources", {}).items():
                name = props.get("name")
                uri = props.get("uri")
                target_id = str(name) if name else str(idx)

                if uri and target_id not in thumbnails_data:
                    last_thumb = request_timestamps.get(f"thumb_{target_id}", 0)
                    if current_time - last_thumb > THUMB_REQUEST_INTERVAL:
                        msg_addr = f"/viosc/thumb/{target_id}"
                        viosc_client.send_message(msg_addr, ["all"])
                        append_log("OUT", msg_addr)
                        request_timestamps[f"thumb_{target_id}"] = current_time

        # monitor players: cleanup closed windows and refresh values
        for p in list(monitor_players):
            if not dpg.does_item_exist(p["tag"]):
                if p.get("target_id"):
                    addr = f"/viosc/monitor/{p['target_id']}"
                    osc_client.send_message(addr, [])
                    append_log("OUT", f"{addr} (stop)")
                monitor_players.remove(p)
                continue
            refresh_monitor_player_values(p["id"])

        dpg.render_dearpygui_frame()
        time.sleep(0.016)
finally:
    # L-4 clean exit: stop audio, shut down the OSC server, destroy the context
    if audio_stream is not None:
        with contextlib.suppress(Exception):
            audio_stream.stop()
            audio_stream.close()
    if local_osc_server is not None:
        with contextlib.suppress(Exception):
            local_osc_server.shutdown()
    dpg.destroy_context()
