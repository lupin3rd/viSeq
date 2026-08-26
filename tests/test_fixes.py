"""Regression tests for the audit HIGH/MED fixes in viseq.py.

Runs headless: stubs dearpygui/sounddevice/essentia/pythonosc, imports the real
module (the GUI main loop is skipped because is_dearpygui_running() is False),
and exercises the real code paths. Covers:
  HIGH-1  thread-safe UI updates (no direct dpg calls in worker threads)
  HIGH-2  AlphaF fade cancellation on non-fade steps
  MED-3   ColorR cell value normalization
  MED-4   payload validation + defensive sorting + error surfacing
  MED-6   network-input caps (blob/json sizes, image pixels, listen default)
  L-1     stale-state pruning
  L-2     audio ring buffer (preallocated, modulo indexing)

Run:  .venv/bin/python -m pytest tests/ -q
"""
# mypy: disable-error-code="attr-defined,assignment"
# The stubs below are built dynamically (sys.modules injection + attribute
# monkey-patching) to mirror the real package layout headless; mypy's static
# ModuleType model cannot express that, so these two codes are disabled for
# this harness file only. viseq.py itself is fully type-checked.

import io
import json
import os
import queue
import re
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
from PIL import Image


# ---------- stubs ----------
class CM:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DpgStub:
    def __init__(self):
        self.calls = []  # (name, args, kwargs)
        self.values = {}
        self.positions = {}  # tag -> [x, y] (get_item_pos)
        self.sizes = {}  # tag -> (width, height)
        self.shown = {}  # tag -> bool (is_item_shown)
        self._tag_counter = 0

    def _tagged(self, name, *a, **kw):
        """Record a call and return a stable fake tag (real DPG returns the item tag)."""
        self.calls.append((name, a, kw))
        self._tag_counter += 1
        return f"{name}_{self._tag_counter}"

    def __getattr__(self, name):
        def fn(*a, **kw):
            self.calls.append((name, a, kw))
            return CM()

        return fn

    # value-returning / stateful APIs the app code relies on (e06)
    def add_text(self, *a, **kw):
        return self._tagged("add_text", *a, **kw)

    def add_button(self, *a, **kw):
        return self._tagged("add_button", *a, **kw)

    def add_checkbox(self, *a, **kw):
        return self._tagged("add_checkbox", *a, **kw)

    def add_combo(self, *a, **kw):
        return self._tagged("add_combo", *a, **kw)

    def add_color_edit(self, *a, **kw):
        return self._tagged("add_color_edit", *a, **kw)

    def add_theme_color(self, *a, **kw):
        return self._tagged("add_theme_color", *a, **kw)

    def draw_rectangle(self, *a, **kw):
        return self._tagged("draw_rectangle", *a, **kw)

    def draw_circle(self, *a, **kw):
        return self._tagged("draw_circle", *a, **kw)

    def draw_line(self, *a, **kw):
        return self._tagged("draw_line", *a, **kw)

    def configure_item(self, tag, **kw):
        self.calls.append(("configure_item", (tag,), kw))
        return CM()

    def get_item_pos(self, item):
        self.calls.append(("get_item_pos", (item,), {}))
        return self.positions.get(item, [0, 0])

    def get_item_width(self, item):
        self.calls.append(("get_item_width", (item,), {}))
        return self.sizes.get(item, (0, 0))[0]

    def get_item_height(self, item):
        self.calls.append(("get_item_height", (item,), {}))
        return self.sizes.get(item, (0, 0))[1]

    def is_item_shown(self, item):
        self.calls.append(("is_item_shown", (item,), {}))
        return self.shown.get(item, True)

    def does_item_exist(self, item):
        return True

    def is_item_focused(self, item):
        return False  # no input focused by default (keyboard shortcuts stay active)

    def does_alias_exist(self, item):
        return False

    def set_value(self, tag, val):
        self.values[tag] = val

    def get_value(self, tag):
        return self.values.get(tag, True)

    def is_dearpygui_running(self):
        return False


dpg = DpgStub()
# dearpygui 2.x is a package: the API module is dearpygui.dearpygui (what viseq.py imports).
# The stub mirrors that layout (verified against 2.3.1 in SPIKE-dpg2x-api.md).
dpg_pkg = types.ModuleType("dearpygui")
dpg_pkg.__path__ = []
sys.modules["dearpygui"] = dpg_pkg
sys.modules["dearpygui.dearpygui"] = dpg

sd = types.ModuleType("sounddevice")
sd.query_devices = lambda: [{"name": "Mock In", "max_input_channels": 2}]
sd.InputStream = object
sys.modules["sounddevice"] = sd


class Sender:
    def __init__(self):
        self.messages = []  # (addr, payload)

    def send_message(self, addr, payload):
        self.messages.append((addr, payload))


essentia = types.ModuleType("essentia")
essentia.array = lambda x: x
standard = types.ModuleType("essentia.standard")


class RhythmExtractor2013:
    def __init__(self, *a, **kw):
        pass

    def __call__(self, audio):
        return (120.0, [], 0.9, [], [])


class LowPass:
    def __init__(self, *a, **kw):
        pass

    def __call__(self, audio):
        return audio


standard.RhythmExtractor2013 = RhythmExtractor2013
standard.LowPass = LowPass
essentia.standard = standard
sys.modules["essentia"] = essentia
sys.modules["essentia.standard"] = standard

osc = types.ModuleType("pythonosc")
udp_client = types.ModuleType("pythonosc.udp_client")
udp_client.SimpleUDPClient = lambda ip, port: Sender()
dispatcher = types.ModuleType("pythonosc.dispatcher")


class FakeDispatcher:
    """Minimal stand-in for pythonosc Dispatcher (records the default handler)."""

    def __init__(self):
        self.handler = None

    def set_default_handler(self, handler):
        self.handler = handler


dispatcher.Dispatcher = FakeDispatcher
osc_server = types.ModuleType("pythonosc.osc_server")


class FakeOSCServer:
    """Minimal stand-in for ThreadingOSCUDPServer: start/stop without real sockets."""

    def __init__(self, *a, **kw):
        pass

    def serve_forever(self):
        pass

    def shutdown(self):
        pass


osc_server.ThreadingOSCUDPServer = FakeOSCServer
osc.udp_client = udp_client
osc.dispatcher = dispatcher
osc.osc_server = osc_server
sys.modules["pythonosc"] = osc
sys.modules["pythonosc.udp_client"] = udp_client
sys.modules["pythonosc.dispatcher"] = dispatcher
sys.modules["pythonosc.osc_server"] = osc_server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import viseq  # noqa: E402  (needs the repo root on sys.path; stubs above must register first)

# capture listen default right after import, before any calls-list clears
listen_defaults = [kw.get("default_value") for n, a, kw in dpg.calls if n == "add_input_text"]

# capture the boot-time window/menubar structure (e03) before any calls-list clears.
# viseq uses the context-manager forms, so the stub records them as window/menu/child_window.
import_time_windows = {kw.get("tag"): kw for n, a, kw in dpg.calls if n == "window"}
import_time_menus = [kw for n, a, kw in dpg.calls if n == "menu"]
import_time_menu_items = [kw for n, a, kw in dpg.calls if n == "add_menu_item"]
import_time_slots = [
    kw
    for n, a, kw in dpg.calls
    if n == "child_window" and str(kw.get("tag", "")).startswith("seq_slot_")
]

# e08: help-window text content, captured before any calls-list clears
# (add_text receives the string positionally; fall back to a text= kwarg for exotic calls)
import_time_texts = [(a[0] if a else kw.get("text")) for n, a, kw in dpg.calls if n == "add_text"]

# e08: every user-facing UI label (checkbox/button/menu/combo) for the English-pass scan,
# captured before any calls-list clears; text widgets come from import_time_texts.
import_time_ui_labels = [
    kw.get("label")
    for n, a, kw in dpg.calls
    if n in ("add_checkbox", "add_button", "add_menu_item", "add_combo")
    and isinstance(kw.get("label"), str)
]

# e09s02: MIDI learn/menu wiring, captured before any calls-list clears
import_time_midi_enable_cb = [
    kw for n, a, kw in dpg.calls if n == "add_checkbox" and kw.get("tag") == "midi_enable_cb"
]
import_time_midi_learn_btn = [
    kw for n, a, kw in dpg.calls if n == "add_button" and kw.get("tag") == "midi_learn_btn"
]
import_time_midi_save_btn = [
    kw for n, a, kw in dpg.calls if n == "add_button" and kw.get("label") == "Save"
]
import_time_midi_refresh_btn = [
    kw for n, a, kw in dpg.calls if n == "add_button" and kw.get("label") == "Refresh"
]
import_time_midi_group = [
    kw for n, a, kw in dpg.calls if n == "group" and kw.get("tag") == "midi_mappings_group"
]
import_time_midi_status = [
    kw for n, a, kw in dpg.calls if n == "add_text" and kw.get("tag") == "midi_learn_status"
]
import_time_seq_cb_0_0 = [
    kw for n, a, kw in dpg.calls if n == "add_checkbox" and kw.get("tag") == "seq_cb_0_0"
]

# e04: audio-window spectrum structure, captured before any calls-list clears
spec_drawlist_tag = any(
    n == "drawlist" and kw.get("tag") == "spec_drawlist" for n, a, kw in dpg.calls
)
band_enabled_checkboxes = {
    kw.get("tag")
    for n, a, kw in dpg.calls
    if n == "add_checkbox" and str(kw.get("tag", "")).startswith("band")
}
band_slider_tags = [
    kw.get("tag")
    for n, a, kw in dpg.calls
    if n == "add_drag_float" and str(kw.get("tag", "")).startswith("band")
]
band_value_text_tags = {
    kw.get("tag")
    for n, a, kw in dpg.calls
    if n == "add_text"
    and str(kw.get("tag", "")).startswith("band")
    and kw.get("tag") != "band_value_text"
}
band_rect_tags = {
    kw.get("tag")
    for n, a, kw in dpg.calls
    if n == "draw_rectangle" and str(kw.get("tag", "")).startswith("band")
}

# e06: settings-window layout/theme sections, captured before any calls-list clears
import_time_settings_buttons = [
    kw
    for n, a, kw in dpg.calls
    if n == "add_button" and kw.get("label") in ("Save layout", "Restore layout")
]
import_time_restore_checkbox = [
    kw
    for n, a, kw in dpg.calls
    if n == "add_checkbox" and kw.get("tag") == "cb_restore_layout_boot"
]
import_time_theme_combo = [
    kw for n, a, kw in dpg.calls if n == "add_combo" and kw.get("tag") == "theme_preset"
]
import_time_theme_edits = {
    kw.get("tag"): kw
    for n, a, kw in dpg.calls
    if n == "add_color_edit" and str(kw.get("tag", "")).startswith("theme_color_")
}
import_time_spectrum_bars = [
    kw
    for n, a, kw in dpg.calls
    if n == "draw_rectangle" and str(kw.get("tag", "")).startswith("spec_bar_")
]
vu_meter_progress = any(
    n == "add_progress_bar" and kw.get("tag") == "vu_meter" for n, a, kw in dpg.calls
)
beat_checkbox_tags = {
    kw.get("tag")
    for n, a, kw in dpg.calls
    if n == "add_checkbox" and str(kw.get("tag", "")).startswith("cb_beat_")
}
beat_led_tags = {
    kw.get("tag")
    for n, a, kw in dpg.calls
    if n == "draw_circle" and str(kw.get("tag", "")).startswith("led_")
}
manual_bpm_input_widget = any(
    n == "add_input_int" and kw.get("tag") == "manual_bpm_input" for n, a, kw in dpg.calls
)
tap_button_widget = any(n == "add_button" and kw.get("tag") == "btn_tap" for n, a, kw in dpg.calls)
manual_bpm_hidden = all(
    kw.get("show") is False
    for n, a, kw in dpg.calls
    if n in ("add_input_int", "add_button") and kw.get("tag") in ("manual_bpm_input", "btn_tap")
)
tap_hidden = manual_bpm_hidden
spec_bar_tags = [
    kw.get("tag")
    for n, a, kw in dpg.calls
    if n == "draw_rectangle" and str(kw.get("tag", "")).startswith("spec_bar_")
]
# e06: the media window header (must be removed)
media_library_header = any(n == "add_text" and "Media Library" in str(a) for n, a, kw in dpg.calls)


# ---------- MED-3: ColorR cell passes DPG-scale (0..255) RGBA value ----------
def test_med3_colorr_dpg_scale_value():
    viseq.tracks_data[0]["steps"][0]["type"] = "ColorR"
    viseq.tracks_data[0]["steps"][0]["last_rand_color"] = [0.5, 0.25, 0.75]
    dpg.calls.clear()
    viseq.update_step_ui(0, 0)
    buttons = [kw for n, a, kw in dpg.calls if n == "add_color_button"]
    assert buttons, "no add_color_button call issued for ColorR step"
    assert buttons[-1].get("default_value") == [127.5, 63.75, 191.25, 255.0], (
        "ColorR default_value must be DPG-scale RGBA (ToColor divides by 255)"
    )


# ---------- MED-4: payload validation / defensive sorting ----------
def test_med4_malformed_source_entries_dropped():
    viseq.update_vimix_sources_ui(
        json.dumps(
            {
                "current_source": 1,
                "sources": {
                    "1": {"name": "clipA", "index": 1, "alpha": 0.5},
                    "2": "not-a-dict",  # must be dropped
                    "abc": {"name": "clipB"},  # non-integer key, no index
                    "3": {"name": "clipC", "index": "3"},
                },  # string index
            }
        )
    )
    assert "2" not in viseq.global_vimix_state["sources"], "malformed entry must be dropped"
    assert "abc" in viseq.global_vimix_state["sources"], "non-integer key must not crash"
    assert viseq.global_vimix_state["sources"]["3"]["index"] == "3", "string index must survive"


def test_med4_malformed_payload_logged_not_silent():
    viseq.update_vimix_sources_ui(json.dumps([1, 2, 3]))
    errs = [m for m in list(viseq.log_queue.queue) if "ERROR" in m]
    assert any("UI update" in e for e in errs), "malformed payload must be logged, not silent"


def test_med4_non_object_sources_logged():
    viseq.update_vimix_sources_ui(json.dumps({"sources": [1, 2]}))
    errs = [m for m in list(viseq.log_queue.queue) if "ERROR" in m]
    assert any("'sources' is not an object" in e for e in errs), (
        "non-object sources payload must be logged"
    )


# ---------- MED-6: input caps ----------
def test_med6_oversized_thumbnail_blob_rejected():
    viseq.incoming_osc_handler(
        "/viosc/replythumb/clipA/0", b"x" * (viseq.MAX_THUMBNAIL_BLOB_BYTES + 1)
    )
    assert viseq.blob_queue.empty(), "oversized thumbnail blob must be rejected"


def test_med6_oversized_replydata_rejected():
    viseq.incoming_osc_handler("/viosc/replydata", "x" * (viseq.MAX_STATE_JSON_BYTES + 1))
    assert viseq.ui_state_queue.empty(), "oversized replydata must be rejected"


def test_med6_normal_thumbnail_blob_accepted(monkeypatch):
    # Fresh queue so the daemon worker (parked on the old queue) cannot race the
    # assertion: the handler must enqueue normal blobs, never reject them.
    fresh = queue.Queue()
    monkeypatch.setattr(viseq, "blob_queue", fresh)
    viseq.incoming_osc_handler("/viosc/replythumb/clipA/0", b"smallblob")
    assert not fresh.empty(), "normal thumbnail blob must be accepted (enqueued)"


def test_med6_listen_default_is_loopback():
    assert "127.0.0.1" in listen_defaults, "listen default must be loopback"


def run_worker_once(blob: bytes) -> bool:
    viseq.texture_queue = queue.Queue()  # fresh queue for the assertion
    viseq.blob_queue.put(("clipA", "0", blob))
    t = threading.Thread(target=viseq.thumbnail_decoder_worker, daemon=True)
    t.start()
    for _ in range(200):  # up to 2s for decode
        if not viseq.texture_queue.empty():
            break
        time.sleep(0.01)
    time.sleep(0.05)
    return not viseq.texture_queue.empty()


def test_med6_oversized_image_rejected_by_worker():
    big = Image.new("RGB", (2000, 2000), "red")  # 4 MP > 3 MP cap
    buf = io.BytesIO()
    big.save(buf, "PNG")
    assert not run_worker_once(buf.getvalue()), "oversized image must be rejected by the worker"


def test_med6_normal_image_decoded_by_worker():
    small = Image.new("RGB", (320, 180), "red")
    buf = io.BytesIO()
    small.save(buf, "PNG")
    assert run_worker_once(buf.getvalue()), "normal image must be decoded by the worker"


# ---------- L-1: stale-state pruning ----------
def test_l1_stale_source_pruned():
    viseq.thumbnails_data.clear()
    viseq.request_timestamps.clear()
    viseq.thumbnails_data["clipA"] = "tex_clipA"
    viseq.thumbnails_data["ghost"] = "tex_ghost"
    viseq.request_timestamps["thumb_clipA"] = 1.0
    viseq.request_timestamps["thumb_ghost"] = 2.0
    viseq.update_vimix_sources_ui(
        json.dumps({"current_source": 1, "sources": {"1": {"name": "clipA", "index": 1}}})
    )
    assert "ghost" not in viseq.thumbnails_data, "stale source must be pruned from thumbnails_data"
    assert "thumb_ghost" not in viseq.request_timestamps, (
        "stale source must be pruned from request_timestamps"
    )
    assert "clipA" in viseq.thumbnails_data and "thumb_clipA" in viseq.request_timestamps, (
        "live source must survive the prune"
    )


# ---------- L-2: ring-buffer audio capture (preallocated, modulo indexing) ----------
def test_l2_ring_buffer_preallocated_and_wraps():
    sr = viseq.samplerate
    viseq.audio_buffer = np.zeros(sr * 6, dtype=np.float32)
    viseq.audio_buffer_head = 0
    buf_id = id(viseq.audio_buffer)
    block = 1024
    n_cb = 300  # 307200 samples > 264600 -> wraps
    for i in range(1, n_cb + 1):
        chunk = np.full((block, 2), float(i), dtype=np.float32)  # one constant value per callback
        viseq.audio_callback(chunk, block, None, None)
    snap = viseq.get_audio_snapshot()
    assert id(viseq.audio_buffer) == buf_id, (
        "np.roll would rebind -> new id; buffer must be preallocated"
    )
    assert len(snap) == sr * 6, "snapshot length must stay consistent"
    assert np.all(snap != 0), "buffer must be fully overwritten after wrap"
    assert np.array_equal(snap[-block:], np.full(block, float(n_cb))), (
        "newest block must be at the tail"
    )


# ---------- FEATURE: ColorV square picks+sends the color; ColorR square shows it ----------
def test_colorv_send_sends_normalized_color():
    track = viseq.tracks_data[0]
    track["base_address"] = "/vimix/clipA"
    step = track["steps"][4]
    step.update({"type": "ColorV", "color": [0.5, 0.25, 0.75]})
    osc_sender = viseq.osc_client
    osc_sender.messages.clear()

    viseq.send_colorv_step(track, 0, 4)

    assert (" /vimix/clipA/color".strip(), [0.5, 0.25, 0.75]) in osc_sender.messages, (
        "ColorV must send the picked color (0..1) without re-normalizing by 255"
    )


def test_colorv_square_default_is_dpg_scale():
    step = viseq.tracks_data[0]["steps"][5]
    step.update({"type": "ColorV", "color": [0.5, 0.25, 0.75]})
    dpg.calls.clear()
    viseq.update_step_ui(0, 5)
    buttons = [kw for n, a, kw in dpg.calls if n == "add_color_button"]
    assert buttons and buttons[-1].get("default_value") == [127.5, 63.75, 191.25, 255.0], (
        "ColorV square must open on DPG's 0..255 RGBA scale (ToColor divides by 255)"
    )


def test_colorr_square_shows_sent_color(monkeypatch):
    monkeypatch.setattr(viseq.random, "uniform", lambda a, b: 0.42)
    track = viseq.tracks_data[0]
    track["base_address"] = "/vimix/clipA"
    step = track["steps"][0]
    step.update({"type": "ColorR", "last_rand_color": [0, 0, 0]})
    osc_sender = viseq.osc_client
    osc_sender.messages.clear()
    dpg.values.clear()

    viseq.send_colorr_step(track, 0, 0)

    assert (" /vimix/clipA/color".strip(), [0.42, 0.42, 0.42]) in osc_sender.messages
    while not viseq.ui_task_queue.empty():
        viseq.ui_task_queue.get()()
    assert dpg.values.get("rand_color_0_0") == [107.1, 107.1, 107.1, 255.0], (
        "the step's little square must show the sent color as DPG-scale RGBA"
    )


def test_dpg_color_scale_boundaries():
    assert viseq.dpg_color_value([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]
    assert viseq.dpg_color_value([1.0, 1.0, 1.0]) == [255.0, 255.0, 255.0]


def test_dpg_color_rgba_appends_opaque_alpha():
    assert viseq.dpg_color_rgba([0.5, 0.25, 0.75]) == [127.5, 63.75, 191.25, 255.0]
    assert viseq.dpg_color_rgba([0.0, 0.0, 0.0])[-1] == 255.0, "alpha must be opaque"


# ---------- FEATURE: SeekR step type (random seek 0..1) ----------
def test_seekr_send_sends_random_seek(monkeypatch):
    monkeypatch.setattr(viseq.random, "uniform", lambda a, b: 0.42)
    track = viseq.tracks_data[0]
    track["base_address"] = "/vimix/clipA"
    step = track["steps"][1]
    step.update({"type": "SeekR"})
    osc_sender = viseq.osc_client
    osc_sender.messages.clear()
    dpg.values.clear()

    viseq.send_seekr_step(track, 0, 1)

    assert (" /vimix/clipA/seek".strip(), 0.42) in osc_sender.messages, (
        "SeekR must send a random 0..1 value to /seek"
    )
    assert step["last_rand_seek"] == 0.42

    while not viseq.ui_task_queue.empty():
        viseq.ui_task_queue.get()()
    assert dpg.values.get("rand_seek_0_1") == "0.42", "cell must display the last seek value"


def test_seekr_menu_item_available():
    step = viseq.tracks_data[0]["steps"][2]
    step["type"] = "NONE"
    dpg.calls.clear()
    viseq.update_step_ui(0, 2)
    menu_items = [kw for n, a, kw in dpg.calls if n == "add_menu_item"]
    assert any(
        kw.get("label") == "Seek Random" and kw.get("user_data") == (0, 2, "SeekR")
        for kw in menu_items
    ), "context menu must offer the SeekR step type"


# ---------- HIGH-1: no direct dpg calls in worker threads ----------
def test_high1_no_direct_dpg_calls_in_worker_threads():
    src = Path("viseq.py").read_text()
    thread_fns = [
        "audio_callback",
        "essentia_analyzer_loop",
        "visual_metronome_loop",
        "sequencer_tick",
        "fade_tick_loop",
        "thumbnail_decoder_worker",
    ]
    dirty = []
    for fn in thread_fns:
        m = re.search(rf"def {fn}\(.*?\n(?=def |\n# ===)", src, re.S)
        if m and re.search(r"dpg\.\w+", m.group(0)):
            dirty.append(fn)
    assert not dirty, f"worker threads must not call dpg directly: {dirty}"


def test_high1_enqueue_set_value_drains_to_main_thread():
    dpg.values.clear()
    viseq.enqueue_set_value("vu_meter", 0.42)
    while not viseq.ui_task_queue.empty():
        viseq.ui_task_queue.get()()
    assert dpg.values.get("vu_meter") == 0.42, (
        "queued set_value must land via the main-thread drain"
    )


def test_high1_lowpass_flag_mirrors_checkbox():
    viseq.on_lowpass_toggle(None, False, None)
    assert viseq.lowpass_enabled is False, "lowpass flag must mirror the checkbox"
    viseq.on_lowpass_toggle(None, True, None)


# ---------- HIGH-2: fade cancellation (live sequencer threads) ----------
def wait_until(pred, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def make_step(active: bool, stype: str, v1: float, v2: float, frames: int, msgs: int) -> dict:
    return {
        "active": active,
        "type": stype,
        "v1": v1,
        "v2": v2,
        "frames": frames,
        "msgs": msgs,
        "color": [1.0, 1.0, 1.0],
        "last_rand_v1": 0.0,
        "last_rand_color": [0, 0, 0],
    }


def test_high2_non_fade_step_cancels_pending_fade():
    viseq.is_playing = False
    viseq.current_bpm = 120.0  # 0.5s per step
    viseq.current_step = -1
    viseq.callback_resync()
    time.sleep(0.3)  # let idle threads settle

    osc_sender = viseq.osc_client
    osc_sender.messages.clear()

    # Track B: step0 = AlphaF (frames=8 -> fade would span ~8 steps), steps 1-6 inactive,
    # step7 = AlphaV active. The step7 dispatch must cancel the running fade.
    t = viseq.tracks_data[0]
    t["base_address"] = "/vimix/clipA"
    t["steps"] = [make_step(False, "NONE", 0.0, 1.0, 8, 4) for _ in range(8)]
    t["steps"][0] = make_step(True, "AlphaF", 0.1, 0.9, 8, 4)
    t["steps"][7] = make_step(True, "AlphaV", 0.33, 0.0, 1, 1)
    t["active_fade"] = {"active": False}

    viseq.is_playing = True

    # mid-sequence: fade should be running and sending intermediates before step7
    mid_ok = wait_until(
        lambda: len([m for m in osc_sender.messages if m[0].endswith("/alpha")]) > 3, timeout=3.0
    )
    assert mid_ok and t["active_fade"].get("active") is True, "fade must be running mid-sequence"

    # step7 (AlphaV) fires every 4s cycle: detect cancellation + last value, then stop promptly
    cancelled_ok = wait_until(
        lambda: (
            t["active_fade"].get("active") is False
            and osc_sender.messages
            and osc_sender.messages[-1][1] == 0.33
        ),
        timeout=12.0,
    )
    if cancelled_ok:
        viseq.is_playing = False
    assert cancelled_ok, "non-fade step must cancel the pending fade"

    last_alpha = [m for m in osc_sender.messages if m[0].endswith("/alpha")]
    assert last_alpha and last_alpha[-1][1] == 0.33, "last alpha message must be the AlphaV value"


def test_high2_uninterrupted_fade_completes():
    # Track A: uninterrupted AlphaF fade completes naturally (no regression)
    t2 = viseq.tracks_data[1]
    t2["base_address"] = "/vimix/clipB"
    t2["steps"] = [make_step(False, "NONE", 0.0, 1.0, 4, 4) for _ in range(8)]
    t2["steps"][0] = make_step(True, "AlphaF", 0.0, 1.0, 4, 4)
    t2["active_fade"] = {"active": False}
    viseq.current_step = -1
    viseq.is_playing = True
    try:
        started = wait_until(lambda: t2["active_fade"].get("active") is True, timeout=6.0)
        completed = wait_until(lambda: t2["active_fade"].get("active") is False, timeout=12.0)
        assert started and completed, "uninterrupted fade must start and complete (frames=4)"
    finally:
        viseq.is_playing = False


# ---------- e02s01: larger centered color square in step cells ----------
def _assert_color_square_large_centered(row: int, col: int, stype: str) -> None:
    dpg.calls.clear()
    viseq.update_step_ui(row, col)
    buttons = [kw for n, a, kw in dpg.calls if n == "add_color_button"]
    assert buttons, f"no add_color_button call issued for {stype} step"
    kw = buttons[-1]
    assert kw.get("width") == viseq.STEP_COLOR_SQUARE_SIZE, "square must use the shared size"
    assert kw.get("height") == viseq.STEP_COLOR_SQUARE_SIZE, "square must use the shared size"
    assert kw.get("indent") == viseq.STEP_COLOR_SQUARE_INDENT, "square must be centered"


def test_colorv_square_large_and_centered():
    step = viseq.tracks_data[0]["steps"][3]
    step.update({"type": "ColorV", "color": [0.5, 0.25, 0.75]})
    _assert_color_square_large_centered(0, 3, "ColorV")
    buttons = [kw for n, a, kw in dpg.calls if n == "add_color_button"]
    assert buttons[-1].get("default_value") == viseq.dpg_color_rgba([0.5, 0.25, 0.75]), (
        "0..255 DPG scale contract must survive the resize"
    )
    assert buttons[-1].get("no_border") is True, "square must draw as a clean swatch"


def test_color_square_indent_centers_in_cell():
    # Measured on real DPG 2.3.1: child content starts at WindowPadding.x=8, so the
    # centered indent is (cell - 2*padding - size)/2 = (90-16-40)/2 = 17, not (90-40)/2.
    assert viseq.STEP_COLOR_SQUARE_INDENT == 17, "indent must center in the padded content region"


def test_colorv_colorr_squares_share_geometry():
    # Both branches must produce the exact same size and position for the swatch.
    dpg.calls.clear()
    viseq.tracks_data[0]["steps"][3].update({"type": "ColorV", "color": [0.5, 0.25, 0.75]})
    viseq.update_step_ui(0, 3)
    v_kw = [kw for n, a, kw in dpg.calls if n == "add_color_button"][-1]
    dpg.calls.clear()
    viseq.tracks_data[0]["steps"][2].update({"type": "ColorR", "last_rand_color": [0.2, 0.4, 0.6]})
    viseq.update_step_ui(0, 2)
    r_kw = [kw for n, a, kw in dpg.calls if n == "add_color_button"][-1]
    for k in ("width", "height", "indent", "no_border"):
        assert (
            v_kw.get(k)
            == r_kw.get(k)
            == {
                "width": viseq.STEP_COLOR_SQUARE_SIZE,
                "height": viseq.STEP_COLOR_SQUARE_SIZE,
                "indent": viseq.STEP_COLOR_SQUARE_INDENT,
                "no_border": True,
            }[k]
        ), f"ColorV/ColorR must share geometry param {k}"


def test_colorv_picker_popup_available():
    dpg.calls.clear()
    viseq.update_step_ui(0, 3)
    pickers = [kw for n, a, kw in dpg.calls if n == "add_color_picker"]
    assert pickers, "ColorV must offer a color picker popup"
    assert pickers[-1].get("no_alpha") is True, "picker must be RGB-only"
    assert pickers[-1].get("callback") == viseq.update_step_val
    assert pickers[-1].get("user_data") == (0, 3, "color")


def test_colorv_pick_refreshes_square():
    step = viseq.tracks_data[0]["steps"][4]
    step.update({"type": "ColorV", "color": [0.1, 0.2, 0.3]})
    dpg.values.clear()
    dpg.values["color_square_0_4"] = [25.5, 51.0, 76.5, 255.0]  # square already exists
    viseq.update_step_val(None, [0.5, 0.25, 0.75], (0, 4, "color"))
    assert step["color"] == [0.5, 0.25, 0.75], "picked color must be stored normalized"
    assert dpg.values.get("color_square_0_4") == [127.5, 63.75, 191.25, 255.0], (
        "the step square must repaint with the picked color"
    )


def test_colorr_square_large_and_centered():
    step = viseq.tracks_data[0]["steps"][2]
    step.update({"type": "ColorR", "last_rand_color": [0.2, 0.4, 0.6]})
    _assert_color_square_large_centered(0, 2, "ColorR")
    buttons = [kw for n, a, kw in dpg.calls if n == "add_color_button"]
    assert buttons[-1].get("default_value") == viseq.dpg_color_rgba([0.2, 0.4, 0.6]), (
        "0..255 DPG scale contract must survive the resize"
    )
    assert buttons[-1].get("tag") == "rand_color_0_2", "repaint tag must survive"


# ---------- e02s02: OSC autostart (client + server at boot) ----------
def _reset_osc_state() -> None:
    viseq.viosc_client = None
    viseq.is_server_running = False
    viseq.local_osc_server = None
    viseq.local_server_thread = None
    dpg.values.clear()


def test_start_osc_server_listens_on_defaults():
    _reset_osc_state()
    ok = viseq.start_osc_server("127.0.0.1", 6667)
    assert ok and viseq.is_server_running is True
    assert dpg.values.get("server_status") == "Server Status: Listening on 127.0.0.1:6667", (
        "status label must reflect the listening server"
    )


def test_start_osc_server_idempotent():
    _reset_osc_state()
    assert viseq.start_osc_server("127.0.0.1", 6667) is True
    thread = viseq.local_server_thread
    assert viseq.start_osc_server("127.0.0.1", 9999) is True, "already running -> no-op True"
    assert viseq.local_server_thread is thread, "must not restart an already running server"


def test_connect_osc_client_sets_ready_status():
    _reset_osc_state()
    ok = viseq.connect_osc_client("127.0.0.1", 6666)
    assert ok and viseq.viosc_client is not None
    assert dpg.values.get("viosc_status") == "Client Status: Ready on 127.0.0.1:6666"


def test_autostart_osc_connects_client_and_starts_server():
    _reset_osc_state()
    viseq.autostart_osc()
    assert viseq.viosc_client is not None, "autostart must connect the OSC client"
    assert viseq.is_server_running is True, "autostart must start the listening server"
    assert dpg.values.get("viosc_status") == "Client Status: Ready on 127.0.0.1:6666"
    assert dpg.values.get("server_status") == "Server Status: Listening on 127.0.0.1:6667"
    assert "6667" in dpg.values.get("server_status", ""), "autostart must use the listen port"


# ---------- e03: menubar shell (settings/logs windows, newest-first logs, clip slot) ----------
def test_format_osc_log_newest_first():
    assert viseq.format_osc_log(["a", "b", "c"]) == "c\nb\na", "latest log line must be on top"
    assert viseq.format_osc_log([]) == ""
    assert viseq.format_osc_log(["only"]) == "only"


def test_settings_window_hidden_closable():
    w = import_time_windows.get("settings_window")
    assert w, "settings window must be tagged settings_window"
    assert w.get("show") is False, "settings window must be hidden by default"
    assert w.get("label") == "Settings"
    assert not w.get("no_close"), "settings window must be closable with X"


def test_logs_window_hidden_closable():
    w = import_time_windows.get("logs_window")
    assert w, "logs window must be tagged logs_window"
    assert w.get("show") is False, "logs window must be hidden by default"
    assert not w.get("no_close"), "logs window must be closable with X"


def test_menubar_show_menu_and_settings_entry():
    assert any(kw.get("label") == "Show" for kw in import_time_menus), "Show menu must exist"
    items = {kw.get("label"): kw.get("callback") for kw in import_time_menu_items}
    assert items.get("Logs") == viseq.show_logs_window, "Show > Logs must open the logs window"
    assert items.get("Settings") == viseq.show_settings_window, (
        "Settings menubar entry must open the settings window"
    )


def test_show_logs_window_callback():
    dpg.calls.clear()
    viseq.show_logs_window()
    assert any(n == "show_item" and a == ("logs_window",) for n, a, kw in dpg.calls)


def test_show_settings_window_callback():
    dpg.calls.clear()
    viseq.show_settings_window()
    assert any(n == "show_item" and a == ("settings_window",) for n, a, kw in dpg.calls)


def test_slot_borderless_centered_button():
    slots = import_time_slots
    assert slots, "clip slots must exist"
    assert all(kw.get("border") is False for kw in slots), "clip slot must have no frame"
    # measured on real DPG 2.3.1: borderless slot has no padding -> indent 12, spacer 6
    assert viseq.SLOT_BUTTON_INDENT == 12, "110px button centered in the 135px slot"
    assert viseq.SLOT_BUTTON_TOP_SPACER == 6, "70px button vertically centered (frame inset 4)"

    dpg.calls.clear()
    viseq.tracks_data[0]["target_id"] = None
    viseq.thumbnails_data.clear()
    viseq.update_track_slot_ui(0)
    btns = [kw for n, a, kw in dpg.calls if n == "add_button"]
    assert btns, "bare ASSIGN CLIP button must be created"
    assert btns[-1].get("parent") == "seq_slot_0", "button must be parented to the clip slot"
    assert btns[-1].get("width") == viseq.SLOT_BUTTON_WIDTH, "button width from constants"
    assert btns[-1].get("height") == viseq.SLOT_BUTTON_HEIGHT, "button height from constants"
    assert btns[-1].get("indent") == viseq.SLOT_BUTTON_INDENT, "button horizontally centered"
    spacers = [kw for n, a, kw in dpg.calls if n == "add_spacer"]
    assert any(kw.get("height") == viseq.SLOT_BUTTON_TOP_SPACER for kw in spacers), (
        "button vertically centered in the slot"
    )

    # assigned-but-no-thumbnail case: the waiting button is also parented to the slot
    dpg.calls.clear()
    viseq.tracks_data[0]["target_id"] = "clipX"
    viseq.update_track_slot_ui(0)
    wait_btns = [kw for n, a, kw in dpg.calls if n == "add_button"]
    assert wait_btns and wait_btns[-1].get("parent") == "seq_slot_0", (
        "waiting button must be parented to the clip slot"
    )


# ---------- e04: selectable-band spectrum analyzer ----------
def test_spectrum_bars_silence_is_zero():
    bars = viseq.compute_spectrum_bars(np.zeros(2048, dtype=np.float32))
    assert bars.shape == (viseq.SPECTRUM_BARS,)
    assert np.all(bars < 0.05), "silence must yield near-zero bars"


def test_spectrum_bars_tone_peaks_near_bin():
    sr = viseq.samplerate
    t = np.arange(2048) / sr
    tone = (0.8 * np.sin(2 * np.pi * 5000.0 * t)).astype(np.float32)
    bars = viseq.compute_spectrum_bars(tone)
    expected_bin = int(5000.0 / (sr / 2) * viseq.SPECTRUM_BARS)
    assert abs(int(np.argmax(bars)) - expected_bin) <= 2, (
        f"5kHz tone must peak near bin {expected_bin}, got {int(np.argmax(bars))}"
    )
    assert float(np.max(bars)) > 0.5, "a loud tone must light its bars"
    assert bool(np.all(bars >= 0.0)) and bool(np.all(bars <= 1.0)), "bars stay in 0..1"


def test_spectrum_bars_short_input_padded():
    bars = viseq.compute_spectrum_bars(np.zeros(100, dtype=np.float32))
    assert bars.shape == (viseq.SPECTRUM_BARS,)


def test_band_value_full_range():
    bars = np.full(32, 0.5)
    assert viseq.band_value_from_bars(bars, 0.0, 1.0) == 0.5


def test_band_value_partial_range():
    bars = np.arange(16, dtype=float) / 15.0
    assert viseq.band_value_from_bars(bars, 0.0, 0.25) == 0.1, "mean of the first 4 bars"


def test_band_value_inverted_range_clamps():
    bars = np.arange(16, dtype=float) / 15.0
    assert viseq.band_value_from_bars(bars, 0.75, 0.25) == 0.8, "inverted range -> one bar"


def test_band_value_empty():
    assert viseq.band_value_from_bars(np.array([]), 0.0, 1.0) == 0.0


def test_on_band_change_refreshes_value():
    viseq.spectrum_bars_cache = np.full(16, 0.4)
    viseq.bands_enabled[1] = True
    dpg.values.clear()
    viseq.on_band_change(None, None, 1)
    assert viseq.band1 == 0.4, "stub sliders read 1.0/1.0 -> last bar level"
    assert dpg.values.get("band1_value_text") == "0.40"
    viseq.bands_enabled[1] = False  # restore default


def test_bands_disabled_by_default():
    viseq.band1 = viseq.band2 = viseq.band3 = 0.0  # reset (module globals persist across tests)
    assert viseq.bands_enabled == {1: False, 2: False, 3: False}
    assert viseq.band1 == 0.0 and viseq.band2 == 0.0 and viseq.band3 == 0.0


def test_refresh_bands_only_enabled():
    bars = np.arange(16, dtype=float) / 15.0
    viseq.spectrum_bars_cache = bars
    viseq.bands_enabled[1] = True
    viseq.bands_enabled[2] = True
    dpg.values.clear()
    viseq.refresh_bands(bars)
    # stub sliders read 1.0/1.0 -> each enabled band = its last bar (15/15 = 1.0)
    assert viseq.band1 == 1.0 and viseq.band2 == 1.0
    assert viseq.band3 == 0.0, "disabled band must stay 0"
    viseq.bands_enabled[1] = False
    viseq.bands_enabled[2] = False


def test_on_band_enable_hides_disabled():
    dpg.calls.clear()
    dpg.values.clear()
    viseq.on_band_enable(None, False, 2)
    assert viseq.bands_enabled[2] is False
    assert dpg.values.get("band2_value_text") == "—", "disabled band shows a dash"
    assert any(
        n == "configure_item" and a == ("band2_rect",) and kw.get("show") is False
        for n, a, kw in dpg.calls
    ), "band2 overlay hidden"


def test_audio_window_spectrum_ui_wired():
    assert spec_drawlist_tag, "spectrum drawlist must exist in the audio window"
    assert len(spec_bar_tags) == viseq.SPECTRUM_BARS, "one tagged bar per spectrum bin"
    assert band_enabled_checkboxes == {"band1_enabled", "band2_enabled", "band3_enabled"}
    assert {f"band{i}_start" for i in (1, 2, 3)}.issubset(band_slider_tags)
    assert {f"band{i}_end" for i in (1, 2, 3)}.issubset(band_slider_tags)
    assert band_value_text_tags == {"band1_value_text", "band2_value_text", "band3_value_text"}
    assert band_rect_tags == {"band1_rect", "band2_rect", "band3_rect"}
    assert not vu_meter_progress, "VU progress bar must be replaced by the spectrum"


def test_band_value_level_window_maps_fill():
    bars = np.array([0.2, 0.5, 0.8])
    assert viseq.band_value_from_bars(bars, 0.0, 1.0, 0.25, 0.75) == 0.5, (
        "levels below min -> 0, at max -> 1, linear between"
    )


def test_band_value_degenerate_level_window_plain_mean():
    bars = np.array([0.2, 0.5, 0.8])
    assert viseq.band_value_from_bars(bars, 0.0, 1.0, 0.6, 0.4) == 0.5, (
        "inverted level window falls back to the plain mean"
    )


def test_autostart_osc_sends_no_boot_sync():
    viseq.osc_client.messages.clear()
    viseq.viosc_client = None
    viseq.is_server_running = False
    viseq.local_osc_server = None
    viseq.local_server_thread = None
    viseq.autostart_osc()
    assert not any("current/sync" in str(m[0]) for m in viseq.osc_client.messages), (
        "nothing must be sent at boot (no /vimix/current/sync)"
    )


def test_audio_window_band_level_sliders_wired():
    for i in (1, 2, 3):
        assert f"band{i}_min" in band_slider_tags, f"band{i} level Min slider missing"
        assert f"band{i}_max" in band_slider_tags, f"band{i} level Max slider missing"


# ---------- e05: beat/clock source selection ----------
def test_beat_is_event_driven():
    for source, event_driven in [
        (viseq.BEAT_SOURCE_ANALYSIS, False),
        (viseq.BEAT_SOURCE_BAND1, True),
        (viseq.BEAT_SOURCE_BAND2, True),
        (viseq.BEAT_SOURCE_BAND3, True),
        (viseq.BEAT_SOURCE_MIDI, True),
        (viseq.BEAT_SOURCE_MANUAL, False),
    ]:
        viseq.beat_source = source
        assert viseq.beat_is_event_driven() is event_driven, source
    viseq.beat_source = viseq.BEAT_SOURCE_ANALYSIS


def test_band_rising_edge_triggers_beat():
    viseq.beat_source = viseq.BEAT_SOURCE_BAND1
    viseq.bands_enabled[1] = True
    viseq.band_prev_values[1] = 0.9
    viseq.sync_event_beat.clear()
    bars = np.full(16, 1.0)
    viseq.refresh_band_value(bars, 1)
    assert viseq.sync_event_beat.is_set(), "band reaching 1.0 must fire the beat"
    # no re-fire while it stays at 1.0 (edge only)
    viseq.sync_event_beat.clear()
    viseq.refresh_band_value(bars, 1)
    assert not viseq.sync_event_beat.is_set(), "no re-trigger on a sustained 1.0"
    viseq.bands_enabled[1] = False
    viseq.band_prev_values[1] = 0.0


def test_band_beat_ignored_when_not_selected():
    viseq.beat_source = viseq.BEAT_SOURCE_BAND2
    viseq.bands_enabled[1] = True
    viseq.band_prev_values[1] = 0.9
    viseq.sync_event_beat.clear()
    viseq.refresh_band_value(np.full(16, 1.0), 1)
    assert not viseq.sync_event_beat.is_set(), "only the selected band drives the beat"
    viseq.bands_enabled[1] = False
    viseq.band_prev_values[1] = 0.0


def test_tap_bpm_averages_intervals(monkeypatch):
    clock = iter([0.0, 0.5, 1.0, 1.5])
    monkeypatch.setattr(viseq.time, "time", lambda: next(clock))
    viseq.tap_times.clear()
    viseq.tap_bpm(None, None, None)  # single tap: no BPM yet
    assert len(viseq.tap_times) == 1
    for _ in range(3):
        viseq.tap_bpm(None, None, None)
    assert viseq.current_bpm == 120.0, "0.5s taps must give 120 BPM"
    viseq.tap_times.clear()


def test_tap_bpm_stale_resets(monkeypatch):
    clock = iter([0.0, 3.0, 3.5])
    monkeypatch.setattr(viseq.time, "time", lambda: next(clock))
    viseq.tap_times.clear()
    viseq.tap_bpm(None, None, None)
    viseq.tap_bpm(None, None, None)  # 3s gap -> reset, not averaged with the first tap
    viseq.tap_bpm(None, None, None)
    assert viseq.current_bpm == 120.0, "stale taps must not skew the average"
    viseq.tap_times.clear()


def test_on_manual_bpm_sets_current_bpm():
    viseq.on_manual_bpm(None, 140, None)
    assert viseq.current_bpm == 140.0


def test_on_beat_source_switches_mode_and_widgets():
    dpg.values["manual_bpm_input"] = 128
    dpg.values["cb_beat_bpm_analysis"] = False
    viseq.on_beat_source(None, True, viseq.BEAT_SOURCE_MANUAL)
    assert viseq.beat_source == viseq.BEAT_SOURCE_MANUAL
    assert viseq.current_bpm == 128.0
    shows = {a[0]: kw.get("show") for n, a, kw in dpg.calls if n == "configure_item"}
    assert shows.get("manual_bpm_input") is True and shows.get("btn_tap") is True
    dpg.values["cb_beat_manual_bpm"] = False
    viseq.on_beat_source(None, True, viseq.BEAT_SOURCE_ANALYSIS)
    assert viseq.beat_source == viseq.BEAT_SOURCE_ANALYSIS
    shows = {a[0]: kw.get("show") for n, a, kw in dpg.calls if n == "configure_item"}
    assert shows.get("manual_bpm_input") is False and shows.get("btn_tap") is False


def test_beat_source_single_selection_radio():
    dpg.values["cb_beat_manual_bpm"] = True
    dpg.values["cb_beat_band1_beat"] = False
    viseq.on_beat_source(None, True, viseq.BEAT_SOURCE_BAND1)
    assert viseq.beat_source == viseq.BEAT_SOURCE_BAND1
    # the manual checkbox must be unchecked, band1 checked
    assert dpg.values.get("cb_beat_manual_bpm") is False
    assert dpg.values.get("cb_beat_band1_beat") is True
    # unchecking the active source must keep it selected
    viseq.on_beat_source("cb_beat_band1_beat", False, viseq.BEAT_SOURCE_BAND1)
    assert viseq.beat_source == viseq.BEAT_SOURCE_BAND1


def test_flash_led_enqueues_main_thread():
    dpg.calls.clear()
    viseq.flash_led("led_midi")
    while not viseq.ui_task_queue.empty():
        viseq.ui_task_queue.get()()
    assert any(
        n == "configure_item" and a == ("led_midi",) and kw.get("fill") == (80, 255, 120, 255)
        for n, a, kw in dpg.calls
    ), "flash must set the LED green via the main-thread queue"


def test_sequencer_waits_once_per_step():
    # Regression: a duplicated sync_event_seq.wait made the manual BPM run at half speed
    src = Path("viseq.py").read_text()
    fn = re.search(r"def sequencer_tick\(.*?\n(?=def |\n# ===)", src, re.S)
    assert fn, "sequencer_tick not found"
    assert fn.group(0).count("sync_event_seq.wait") == 1, (
        "sequencer_tick must wait exactly once per step (duplicate wait halves the BPM)"
    )


def test_midi_beats_from_pulses():
    assert viseq.midi_beats_from_pulses(0) == 0
    assert viseq.midi_beats_from_pulses(23) == 0
    assert viseq.midi_beats_from_pulses(24) == 1
    assert viseq.midi_beats_from_pulses(48) == 2


def test_beat_source_ui_wired():
    assert beat_checkbox_tags == {
        "cb_beat_bpm_analysis",
        "cb_beat_band1_beat",
        "cb_beat_band2_beat",
        "cb_beat_band3_beat",
        "cb_beat_midi_sync",
        "cb_beat_manual_bpm",
    }, "one checkbox per beat source"
    assert beat_led_tags == {
        "led_analysis",
        "led_band1",
        "led_band2",
        "led_band3",
        "led_midi",
        "led_manual",
    }, "one LED per beat source"
    assert manual_bpm_input_widget and tap_button_widget, "manual BPM widgets must exist"
    assert manual_bpm_hidden and tap_hidden, "manual widgets hidden unless manual mode"


def test_beat_lines_alignment_spacer():
    # Measured on real DPG 2.3.1 with the compact 28px transport: the row renders 312px
    # wide (buttons render wider than declared), so line 2 starts under line 1.
    assert viseq.SEQ_TRANSPORT_WIDTH == 312, "line 2 must start under line 1"


def test_manual_bpm_live_text_wired():
    dpg.values.clear()
    viseq.on_manual_bpm(None, 140, None)
    assert dpg.values.get("manual_bpm_text") == "140 BPM", "live manual BPM readout"
    assert dpg.values.get("testo_bpm") == "BPM: 140.0"


# ---------- e06: Mediagrid window polish ----------
def test_mediagrid_window_renamed_and_header_removed():
    w = import_time_windows.get("vimix_media_window")
    assert w and w.get("label") == "Mediagrid", "window must be renamed 'Mediagrid'"
    assert not media_library_header, "the 'Media Library:' header must be gone"


def test_media_tile_shows_index_and_alpha():
    viseq.thumbnails_data.clear()
    viseq.update_vimix_sources_ui(
        json.dumps(
            {"current_source": 1, "sources": {"1": {"name": "clipA", "index": 3, "alpha": 0.42}}}
        )
    )
    assert dpg.values.get("tile_title_clipA") == "clipA"
    labels = {a[0]: kw.get("label") for n, a, kw in dpg.calls if n == "configure_item"}
    assert labels.get("tile_index_clipA") == "3", "badge must show the bare index"
    assert dpg.values.get("tile_alpha_clipA") == "0.42", "tile must show the bare alpha value"


def test_media_tile_alpha_missing_shows_dash():
    viseq.thumbnails_data.clear()
    viseq.update_vimix_sources_ui(
        json.dumps({"current_source": 1, "sources": {"2": {"name": "clipB", "index": 5}}})
    )
    assert dpg.values.get("tile_alpha_clipB") == "---", "missing alpha must show a dash"


# ---------- e07: compact graphical monitor player ----------
def _monitor_cleanup(count_before: int) -> None:
    viseq.monitor_players.pop()
    viseq.monitor_player_counter = count_before


def test_monitor_default_props_alpha_seek_speed():
    count = viseq.monitor_player_counter
    viseq.new_monitor_player(None, None, None)
    player = viseq.monitor_players[-1]
    assert player["props"] == ["alpha", "seek", "speed"], (
        "a new monitor must request alpha, seek, speed"
    )
    _monitor_cleanup(count)


def test_monitor_assign_sets_default_props():
    viseq.global_vimix_state["current_source"] = 1
    viseq.global_vimix_state["sources"] = {"1": {"name": "clipA", "index": 1}}
    count = viseq.monitor_player_counter
    viseq.new_monitor_player(None, None, None)
    player = viseq.monitor_players[-1]
    viseq.assign_monitor_player(None, None, player["id"])
    assert player["target_id"] == "clipA"
    assert player["props"] == ["alpha", "seek", "speed"], "assign must request the defaults"
    _monitor_cleanup(count)


def test_monitor_graphical_elements_and_refresh():
    viseq.global_vimix_state["current_source"] = 1
    viseq.global_vimix_state["sources"] = {
        "1": {"name": "clipA", "index": 1, "alpha": 0.5, "seek": 0.3, "speed": 1.5, "play": True}
    }
    viseq.thumbnails_data["clipA"] = "tex_clipA"
    count = viseq.monitor_player_counter
    viseq.new_monitor_player(None, None, None)
    player = viseq.monitor_players[-1]
    dpg.calls.clear()
    viseq.assign_monitor_player(None, None, player["id"])
    pid = player["id"]
    assert any(n == "draw_line" and kw.get("tag") == f"mon_arm_{pid}" for n, a, kw in dpg.calls)
    assert any(
        n == "draw_rectangle" and kw.get("tag") == f"mon_alpha_fill_{pid}" for n, a, kw in dpg.calls
    )
    assert any(
        n == "draw_rectangle" and kw.get("tag") == f"mon_seek_fill_{pid}" for n, a, kw in dpg.calls
    )
    dpg.calls.clear()
    viseq.refresh_monitor_display(pid)
    assert any(n == "configure_item" and a == (f"mon_arm_{pid}",) for n, a, kw in dpg.calls), (
        "turntable arm must spin"
    )
    alpha_cfg = [
        kw for n, a, kw in dpg.calls if n == "configure_item" and a == (f"mon_alpha_fill_{pid}",)
    ]
    assert alpha_cfg and alpha_cfg[-1].get("pmin") == [0, 32.0], "alpha bar at 50% of the 64px disc"
    seek_cfg = [
        kw for n, a, kw in dpg.calls if n == "configure_item" and a == (f"mon_seek_fill_{pid}",)
    ]
    assert seek_cfg and seek_cfg[-1].get("pmax") == [75.0, 10], "seek bar at 30%"
    _monitor_cleanup(count)


def test_new_monitor_shows_assign_button():
    count = viseq.monitor_player_counter
    dpg.calls.clear()
    viseq.new_monitor_player(None, None, None)
    player = viseq.monitor_players[-1]
    assigns = [
        kw for n, a, kw in dpg.calls if n == "add_button" and "ASSIGN" in str(kw.get("label", ""))
    ]
    assert assigns, "a fresh monitor player must show the CLICK TO ASSIGN button"
    assert not player["target_id"]
    _monitor_cleanup(count)


# ---------- e08: step copy/paste + robust disc play detection ----------
def test_video_is_playing_handles_formats():
    assert viseq.video_is_playing({"play": True}, 0.0, 0.5) is True
    assert viseq.video_is_playing({"play": 1}, 0.0, 0.5) is True
    assert viseq.video_is_playing({"play": "true"}, 0.0, 0.5) is True
    assert viseq.video_is_playing({"play": False}, 0.0, 0.5) is False
    assert viseq.video_is_playing({"play": 0}, 0.0, 0.5) is False
    assert viseq.video_is_playing({"play": "false"}, 0.0, 0.5) is False, (
        "a 'false' string must not count as playing"
    )
    assert viseq.video_is_playing({}, 0.0, 0.5) is True, "advancing seek -> playing"
    assert viseq.video_is_playing({}, 0.5, 0.5) is False, "static seek -> paused"


def test_copy_paste_step():
    step = viseq.tracks_data[0]["steps"][2]
    step.update(
        {"type": "ColorR", "active": True, "v1": 0.33, "last_rand_color": [0.5, 0.25, 0.75]}
    )
    viseq.copy_step(None, None, (0, 2))
    assert viseq.copied_step_data is not None
    viseq.paste_step(None, None, (0, 5))
    pasted = viseq.tracks_data[0]["steps"][5]
    assert pasted["type"] == "ColorR" and pasted["active"] is True
    assert pasted["v1"] == 0.33
    assert pasted["last_rand_color"] == [0.5, 0.25, 0.75]
    assert pasted["last_rand_color"] is not step["last_rand_color"], "deep copy expected"


def test_paste_step_to_row():
    src = viseq.tracks_data[1]["steps"][0]
    src.update({"type": "AlphaV", "v1": 0.77, "active": True})
    viseq.copy_step(None, None, (1, 0))
    viseq.paste_step_to_row(None, None, (1, 3))
    for c in range(viseq.NUM_STEPS):
        st = viseq.tracks_data[1]["steps"][c]
        assert st["type"] == "AlphaV" and st["v1"] == 0.77, f"step {c} must match the copy"
    viseq.copied_step_data = None


def test_step_popup_has_copy_paste_items():
    dpg.calls.clear()
    viseq.update_step_ui(0, 0)
    items = [kw.get("label") for n, a, kw in dpg.calls if n == "add_menu_item"]
    assert "Copy Step" in items and "Paste Step" in items and "Paste to Row" in items


def test_copy_highlights_cell():
    viseq.tracks_data[0]["steps"][2].update({"type": "ColorR", "v1": 0.5})
    dpg.calls.clear()
    viseq.copy_step(None, None, (0, 2))
    binds = [a for n, a, kw in dpg.calls if n == "bind_item_theme" and a and a[0] == "seq_cell_0_2"]
    assert binds and binds[-1][1] == viseq.theme_step_copied, "the copied cell must be highlighted"
    assert viseq.active_step == (0, 2)
    viseq.copied_step_data = None


def test_copy_moves_highlight_to_new_cell():
    viseq.copy_step(None, None, (0, 0))
    dpg.calls.clear()
    viseq.copy_step(None, None, (0, 1))
    assert any(n == "bind_item_theme" and a and a[0] == "seq_cell_0_1" for n, a, kw in dpg.calls), (
        "the new copy must be highlighted"
    )
    assert viseq.copied_step_pos == (0, 1)
    viseq.copied_step_data = None


def test_toggle_step_sets_active_step():
    viseq.toggle_step_active(None, True, (1, 5))
    assert viseq.active_step == (1, 5)
    viseq.toggle_step_active(None, False, (1, 5))


def test_shortcut_copy_paste_active_step():
    viseq.copied_step_data = None
    viseq.tracks_data[0]["steps"][0].update({"type": "AlphaV", "v1": 0.9, "active": True})
    viseq.copy_step(None, None, (0, 0))
    viseq.active_step = (0, 4)
    viseq.on_paste_shortcut(None, None, None)
    assert viseq.tracks_data[0]["steps"][4]["type"] == "AlphaV", (
        "Ctrl+V must paste into the active step"
    )
    # Ctrl+C captures the active step, Ctrl+V restores it after a change
    viseq.active_step = (0, 7)
    orig_type = viseq.tracks_data[0]["steps"][7]["type"]
    viseq.on_copy_shortcut(None, None, None)
    viseq.tracks_data[0]["steps"][7]["type"] = "NONE"
    viseq.on_paste_shortcut(None, None, None)
    assert viseq.tracks_data[0]["steps"][7]["type"] == orig_type, "Ctrl+C then Ctrl+V round trip"
    viseq.copied_step_data = None


def test_shortcut_ignored_when_input_focused(monkeypatch):
    monkeypatch.setattr(viseq, "_any_input_focused", lambda: True)
    viseq.copied_step_data = None
    viseq.active_step = (0, 2)
    viseq.on_copy_shortcut(None, None, None)
    assert viseq.copied_step_data is None, "Ctrl+C must not fire while typing in an input"


# ---------- e06s01: window layout save/restore ----------
def test_sequencer_and_audio_windows_have_explicit_tags():
    assert import_time_windows.get("sequencer_window"), "sequencer must carry an explicit tag"
    assert import_time_windows.get("audio_window"), "audio analyzer must carry an explicit tag"
    assert import_time_windows["sequencer_window"].get("label") == "Step Sequencer"
    assert import_time_windows["audio_window"].get("label") == "Audio analyzer"


def test_layout_window_tags_cover_all_fixed_windows():
    assert set(viseq.LAYOUT_WINDOW_TAGS) == {
        "sequencer_window",
        "audio_window",
        "settings_window",
        "vimix_media_window",
        "logs_window",
    }


def test_snapshot_window_layout_records_shown_pos_size(monkeypatch):
    positions = {"sequencer_window": [10, 10], "settings_window": [370, 820]}
    sizes = {"sequencer_window": (1050, 800), "settings_window": (340, 320)}
    shown = {"settings_window": False}
    monkeypatch.setattr(dpg, "get_item_pos", lambda tag: positions.get(tag, [0, 0]))
    monkeypatch.setattr(dpg, "get_item_width", lambda tag: sizes.get(tag, (0, 0))[0])
    monkeypatch.setattr(dpg, "get_item_height", lambda tag: sizes.get(tag, (0, 0))[1])
    monkeypatch.setattr(dpg, "is_item_shown", lambda tag: shown.get(tag, True))
    records = viseq.snapshot_window_layout()
    by_tag = {r["tag"]: r for r in records}
    assert by_tag["sequencer_window"]["pos"] == [10, 10]
    assert by_tag["sequencer_window"]["size"] == [1050, 800]
    assert by_tag["sequencer_window"]["shown"] is True
    assert by_tag["settings_window"]["shown"] is False


def test_apply_window_layout_sets_geometry_and_visibility(monkeypatch):
    monkeypatch.setattr(dpg, "does_item_exist", lambda tag: tag != "ghost_window")
    dpg.calls.clear()
    records = [
        {"tag": "settings_window", "shown": False, "pos": [100, 200], "size": [400, 300]},
        {"tag": "logs_window", "shown": True, "pos": [50, 60], "size": [900, 150]},
        {"tag": "ghost_window", "shown": True, "pos": [1, 2], "size": [3, 4]},  # missing -> skipped
    ]
    viseq.apply_window_layout(records)
    set_pos = [a for n, a, kw in dpg.calls if n == "set_item_pos"]
    set_w = [a for n, a, kw in dpg.calls if n == "set_item_width"]
    set_h = [a for n, a, kw in dpg.calls if n == "set_item_height"]
    assert ("settings_window", [100, 200]) in set_pos
    assert ("settings_window", 400) in set_w
    assert ("settings_window", 300) in set_h
    assert any(a == ("logs_window",) for n, a, kw in dpg.calls if n == "show_item")
    assert any(a == ("settings_window",) for n, a, kw in dpg.calls if n == "hide_item")
    assert not any("ghost_window" in a for n, a, kw in dpg.calls), "missing windows must be skipped"


def test_load_config_missing_file_returns_defaults(monkeypatch):
    monkeypatch.setattr(viseq, "CONFIG_PATH", "/nonexistent/viseq_config.json")
    cfg = viseq.load_config()
    assert cfg["layout"]["restore_on_boot"] is True
    assert cfg["layout"]["windows"] == []
    assert cfg["theme"]["preset"] == "scuro"
    assert cfg["theme"]["colors"]["window_bg"] == list(viseq.DEFAULT_PALETTE["window_bg"])


def test_load_config_corrupt_file_returns_defaults(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ not valid json !!!")
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    cfg = viseq.load_config()
    assert cfg["layout"]["restore_on_boot"] is True, "corrupt config must fall back to defaults"


def test_save_then_load_config_round_trip(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    cfg = viseq.load_config()
    cfg["layout"]["restore_on_boot"] = False
    cfg["theme"]["preset"] = "chiaro"
    viseq.save_config(cfg)
    loaded = viseq.load_config()
    assert loaded["layout"]["restore_on_boot"] is False
    assert loaded["theme"]["preset"] == "chiaro"


def test_should_restore_layout_on_boot_defaults_true(monkeypatch):
    monkeypatch.setattr(viseq, "CONFIG_PATH", "/nonexistent/viseq_config.json")
    cfg = viseq.load_config()
    assert viseq.should_restore_layout_on_boot(cfg) is True
    cfg["layout"]["restore_on_boot"] = False
    assert viseq.should_restore_layout_on_boot(cfg) is False


def test_save_layout_to_config_persists_snapshot(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    monkeypatch.setattr(dpg, "get_item_pos", lambda tag: [7, 7])
    monkeypatch.setattr(dpg, "get_item_width", lambda tag: 111)
    monkeypatch.setattr(dpg, "get_item_height", lambda tag: 222)
    monkeypatch.setattr(dpg, "is_item_shown", lambda tag: True)
    viseq.save_layout_to_config()
    cfg = viseq.load_config()
    assert cfg["layout"]["windows"], "the layout snapshot must persist"
    for r in cfg["layout"]["windows"]:
        assert set(r.keys()) == {"tag", "shown", "pos", "size"}


def test_restore_layout_from_config_applies(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    cfg = {
        "layout": {
            "restore_on_boot": True,
            "windows": [{"tag": "logs_window", "shown": True, "pos": [5, 6], "size": [700, 200]}],
        },
        "theme": {"preset": "scuro", "colors": viseq.DEFAULT_PALETTE},
    }
    p.write_text(json.dumps(cfg))
    dpg.calls.clear()
    viseq.restore_layout_from_config()
    assert any(n == "show_item" and a == ("logs_window",) for n, a, kw in dpg.calls)
    assert any(n == "set_item_pos" and a == ("logs_window", [5, 6]) for n, a, kw in dpg.calls)


def test_restore_layout_boot_toggle_persists(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    viseq.on_restore_layout_boot_toggle(None, False)
    cfg = viseq.load_config()
    assert cfg["layout"]["restore_on_boot"] is False
    viseq.on_restore_layout_boot_toggle(None, True)
    cfg = viseq.load_config()
    assert cfg["layout"]["restore_on_boot"] is True


def test_settings_window_has_windows_section():
    labels = [kw.get("label") for kw in import_time_settings_buttons]
    assert "Save layout" in labels, "Save layout button must exist"
    assert "Restore layout" in labels, "Restore layout button must exist"
    assert import_time_restore_checkbox, "Restore at startup checkbox must exist"
    cb = import_time_restore_checkbox[0]
    assert cb.get("default_value") is True, "restore-at-boot must default on"
    assert cb.get("callback") == viseq.on_restore_layout_boot_toggle


# ---------- e06s02: theming ----------
def test_default_palette_reproduces_current_look():
    pal = viseq.DEFAULT_PALETTE
    assert pal["panel_bg"] == [40, 40, 40]
    assert pal["border"] == [80, 80, 80]
    assert pal["accent"] == [50, 255, 50]
    assert pal["accent_bg"] == [30, 80, 30]
    assert pal["text"] == [200, 200, 200]
    assert pal["text_dim"] == [150, 150, 150]
    assert pal["badge_bg"] == [45, 55, 75]
    assert pal["warning"] == [255, 220, 80]


def test_palettes_cover_all_slots():
    assert set(viseq.DEFAULT_PALETTE.keys()) == set(viseq.PALETTE_SLOTS)
    assert set(viseq.LIGHT_PALETTE.keys()) == set(viseq.PALETTE_SLOTS)
    assert viseq.LIGHT_PALETTE["window_bg"] != viseq.DEFAULT_PALETTE["window_bg"]
    assert viseq.LIGHT_PALETTE["text"] != viseq.DEFAULT_PALETTE["text"]


def test_derive_palette_fills_all_slots_and_keeps_primaries():
    primaries = {
        "window_bg": [24, 24, 24],
        "panel_bg": [40, 40, 40],
        "border": [80, 80, 80],
        "text": [200, 200, 200],
        "accent": [50, 255, 50],
    }
    pal = viseq.derive_palette(primaries)
    assert set(pal.keys()) == set(viseq.PALETTE_SLOTS)
    assert pal["border_active"] == [50, 255, 50], "active border follows the accent"
    for slot, color in pal.items():
        assert len(color) == 3, f"{slot} must be RGB"
        assert all(0 <= c <= 255 for c in color), f"{slot} channels out of range"
    assert pal["text_dim"] != pal["text"], "dim text must differ from plain text"
    assert pal["accent_bg"] != pal["panel_bg"], "accent-tinted panel must differ from panel"


def test_derive_palette_does_not_mutate_input():
    import copy as _copy

    primaries = {
        "window_bg": [24, 24, 24],
        "panel_bg": [40, 40, 40],
        "border": [80, 80, 80],
        "text": [200, 200, 200],
        "accent": [50, 255, 50],
    }
    before = _copy.deepcopy(primaries)
    viseq.derive_palette(primaries)
    assert primaries == before, "derive_palette must not mutate its input"


def test_apply_palette_updates_recorded_bindings():
    dpg.calls.clear()
    viseq.apply_palette(viseq.LIGHT_PALETTE)
    # theme color items are updated via set_value (stub stores them in values)
    tc_sets = {k: v for k, v in dpg.values.items() if str(k).startswith("add_theme_color_")}
    assert tc_sets, "theme color items must be updated via set_value"
    for color in tc_sets.values():
        assert len(color) == 4 and color[3] == 255, "theme colors must carry opaque alpha"
    cfg_items = [kw for n, a, kw in dpg.calls if n == "configure_item"]
    assert cfg_items, "text/draw items must be updated via configure_item"
    assert viseq.active_palette == viseq.LIGHT_PALETTE
    viseq.apply_palette(viseq.DEFAULT_PALETTE)  # restore the shared default for other tests


def test_theme_color_creation_records_binding():
    dpg.calls.clear()
    viseq.theme_color(dpg.mvThemeCol_Border, "border")
    assert dpg.calls and dpg.calls[-1][0] == "add_theme_color"
    assert len(viseq._theme_color_bindings) >= 1


def test_settings_theme_section_wired():
    assert import_time_theme_combo, "Theme preset combo must exist"
    assert import_time_theme_combo[0].get("items") == ["Dark", "Light", "Custom"]
    assert import_time_theme_combo[0].get("callback") == viseq.on_theme_preset
    assert set(import_time_theme_edits.keys()) == {
        "theme_color_window_bg",
        "theme_color_panel_bg",
        "theme_color_border",
        "theme_color_text",
        "theme_color_accent",
    }
    for tag, kw in import_time_theme_edits.items():
        assert kw.get("callback") == viseq.on_theme_color, f"{tag} must use on_theme_color"
        assert kw.get("user_data") in viseq.THEME_PRIMARY_SLOTS


def test_spectrum_bars_are_palette_driven():
    assert import_time_spectrum_bars, "spectrum bars must exist"
    assert len(import_time_spectrum_bars) == viseq.SPECTRUM_BARS
    for kw in import_time_spectrum_bars:
        assert kw.get("fill") == [*viseq.DEFAULT_PALETTE["spectrum"], 255], (
            "bar fill must come from the spectrum palette slot"
        )


def test_on_theme_preset_light_applies_and_persists(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    dpg.calls.clear()
    viseq.on_theme_preset(None, "Light")
    assert viseq.active_palette == viseq.LIGHT_PALETTE
    cfg = viseq.load_config()
    assert cfg["theme"]["preset"] == "chiaro"
    assert cfg["theme"]["colors"] == viseq.LIGHT_PALETTE
    synced = [v for k, v in dpg.values.items() if str(k).startswith("theme_color_")]
    assert len(synced) == 5, "all five color edits must be synced to the new palette"
    assert all(
        v[:3] == viseq.LIGHT_PALETTE[slot]
        for v, slot in zip(synced, viseq.THEME_PRIMARY_SLOTS, strict=True)
    ), "edits must hold the preset colors"
    viseq.apply_palette(viseq.DEFAULT_PALETTE)  # restore shared state


def test_on_theme_color_derives_from_edits_and_persists(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    # edits hold 0..255 values (real DPG color_edit get_value scale)
    for i, slot in enumerate(viseq.THEME_PRIMARY_SLOTS):
        dpg.values[f"theme_color_{slot}"] = [30 + i * 20, 128, 178, 255]
    dpg.calls.clear()
    viseq.on_theme_color("theme_color_accent", [0.9, 0.2, 0.3, 1.0], "accent")
    pal = viseq.active_palette
    assert set(pal.keys()) == set(viseq.PALETTE_SLOTS)
    assert pal["window_bg"] == [30, 128, 178], "other edits read on the 0..255 scale"
    assert pal["accent"] == [230, 51, 76], "sender's callback payload (0..1) wins over get_value"
    cfg = viseq.load_config()
    assert cfg["theme"]["preset"] == "custom"
    assert cfg["theme"]["colors"] == pal
    assert dpg.values.get("theme_preset") == "Custom", "editing a color switches to custom"
    viseq.apply_palette(viseq.DEFAULT_PALETTE)


def test_boot_applies_saved_theme_and_layout(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "layout": {
                    "restore_on_boot": True,
                    "windows": [
                        {"tag": "logs_window", "shown": True, "pos": [5, 6], "size": [700, 200]}
                    ],
                },
                "theme": {"preset": "chiaro", "colors": viseq.LIGHT_PALETTE},
            }
        )
    )
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    dpg.calls.clear()
    viseq.apply_boot_config()
    assert viseq.active_palette == viseq.LIGHT_PALETTE
    assert any(n == "show_item" and a == ("logs_window",) for n, a, kw in dpg.calls), (
        "boot must restore the saved layout"
    )
    assert dpg.values.get("cb_restore_layout_boot") is True
    viseq.apply_palette(viseq.DEFAULT_PALETTE)


# ---------- e06 regression: color_edit get_value is 0..255, not 0..1 ----------
def test_read_primary_colors_uses_dpg_0_255_scale():
    for slot in viseq.THEME_PRIMARY_SLOTS:
        dpg.values[f"theme_color_{slot}"] = [24, 60, 100, 255]  # 0..255 like real DPG
    colors = viseq._read_primary_colors_from_edits()
    assert colors["window_bg"] == [24, 60, 100], "0..255 must not be re-scaled"
    assert colors["accent"] == [24, 60, 100]


def test_load_config_sanitizes_out_of_range_palette(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "theme": {
                    "preset": "custom",
                    "colors": {
                        "window_bg": [6120, 6120, 6120],
                        "text": [51000, 24, 12],
                        "accent": [70000, 65025, 12750],
                    },
                }
            }
        )
    )
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    cfg = viseq.load_config()
    colors = cfg["theme"]["colors"]
    assert colors["window_bg"] == [24, 24, 24], "6120 = 24*255 must recover to 24, not white"
    assert colors["text"] == [200, 24, 12], "51000 = 200*255 recovers; in-range channels stay"
    assert colors["accent"] == [255, 255, 50], "genuinely huge channels clamp to 255"
    assert set(colors.keys()) == set(viseq.PALETTE_SLOTS), "missing slots must be refilled"


def test_theme_preset_custom_with_untouched_edits_stays_sane(monkeypatch, tmp_path):
    # untouched edits hold the Scuro defaults on the 0..255 scale (real DPG get_value)
    for slot in viseq.THEME_PRIMARY_SLOTS:
        dpg.values[f"theme_color_{slot}"] = [*viseq.DEFAULT_PALETTE[slot], 255]
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    viseq.on_theme_preset(None, "Custom")
    pal = viseq.active_palette
    assert all(0 <= c <= 255 for slot in pal for c in pal[slot]), "no out-of-range channels"
    assert pal["window_bg"] == viseq.DEFAULT_PALETTE["window_bg"]
    cfg = viseq.load_config()
    assert cfg["theme"]["preset"] == "custom"
    stored = cfg["theme"]["colors"]
    assert all(0 <= c <= 255 for slot in stored for c in stored[slot])
    viseq.apply_palette(viseq.DEFAULT_PALETTE)


# ---------- e06s01 revision: saved layout never re-opens the Settings window ----------
def test_snapshot_never_records_settings_as_open(monkeypatch):
    monkeypatch.setattr(dpg, "is_item_shown", lambda tag: True)  # every window open
    records = viseq.snapshot_window_layout()
    by_tag = {r["tag"]: r for r in records}
    assert by_tag["settings_window"]["shown"] is False, (
        "the Settings window must always be saved as closed"
    )
    assert by_tag["logs_window"]["shown"] is True, "other windows keep their real state"
    assert by_tag["settings_window"]["pos"] == [0, 0] or "pos" in by_tag["settings_window"], (
        "position/size are still saved for Settings"
    )


def test_apply_layout_never_shows_settings(monkeypatch):
    monkeypatch.setattr(dpg, "does_item_exist", lambda tag: True)
    dpg.calls.clear()
    records = [{"tag": "settings_window", "shown": True, "pos": [1, 2], "size": [3, 4]}]
    viseq.apply_window_layout(records)
    assert any(a == ("settings_window",) for n, a, kw in dpg.calls if n == "hide_item"), (
        "a saved-open Settings window must be hidden on restore"
    )
    assert not any(a == ("settings_window",) for n, a, kw in dpg.calls if n == "show_item"), (
        "Settings must never be shown by a layout restore"
    )


# ---------- e07s01 (P0): Mediagrid per-push cost ----------
def _media_payload(n=6, current="0"):
    sources = {}
    for i in range(n):
        sources[str(i)] = {
            "index": i,
            "name": f"clip_{i}",
            "lock": 0,
            "failed": 0,
            "play": 0,
            "pause": 0,
            "blending": 0,
            "alpha": 0.5,
            "transparency": 0.0,
            "depth": 0,
            "position": 0,
            "size": 0,
            "corner": 0,
            "angle": 0.0,
            "seek": 0.0,
            "speed": 1.0,
        }
    return json.dumps({"current_source": current, "sources": sources})


def test_mediagrid_identical_push_performs_no_dpg_value_calls():
    payload = _media_payload()
    viseq.update_vimix_sources_ui(payload)
    dpg.calls.clear()
    dpg.values.clear()
    viseq.update_vimix_sources_ui(payload)
    assert dpg.values == {}, "an identical push must not call set_value anywhere"
    assert not any(n in ("bind_item_theme", "configure_item") for n, a, kw in dpg.calls), (
        "an identical push must not bind themes or configure items"
    )


def test_mediagrid_value_change_updates_only_affected_cells():
    payload = _media_payload()
    viseq.update_vimix_sources_ui(payload)
    p2 = json.loads(payload)
    p2["sources"]["3"]["alpha"] = 0.9
    dpg.calls.clear()
    dpg.values.clear()
    viseq.update_vimix_sources_ui(json.dumps(p2))
    assert dpg.values.get("tile_alpha_clip_3") == "0.90", "the changed tile alpha must update"
    assert dpg.values.get("raw_3_alpha") == "0.90", "the changed raw cell must update"
    assert len(dpg.values) == 2, "only the affected cells may be written"
    assert not any(n == "bind_item_theme" for n, a, kw in dpg.calls), (
        "a value-only push must not touch tile themes"
    )


def test_mediagrid_selection_change_refreshes_themes():
    payload = _media_payload(current="0")
    viseq.update_vimix_sources_ui(payload)
    p2 = json.loads(payload)
    p2["current_source"] = "3"
    dpg.calls.clear()
    viseq.update_vimix_sources_ui(json.dumps(p2))
    binds = [a for n, a, kw in dpg.calls if n == "bind_item_theme"]
    assert binds, "a selection change must re-bind the tile themes"
    assert any(a[0] == "tile_clip_3" for a in binds), "the newly selected tile must highlight"


# ---------- e07s02 (P1): idle render throttle ----------
def test_frame_sleep_idle_is_throttled():
    saved = (viseq.is_playing, viseq.is_audio_analyzing, list(viseq.monitor_players))
    viseq.is_playing = False
    viseq.is_audio_analyzing = False
    viseq.monitor_players.clear()
    try:
        assert viseq.frame_sleep() == viseq.FRAME_SLEEP_IDLE, "idle must use the throttled rate"
    finally:
        viseq.is_playing, viseq.is_audio_analyzing, viseq.monitor_players = (
            saved[0],
            saved[1],
            saved[2],
        )


def test_frame_sleep_full_rate_while_animating():
    saved = (viseq.is_playing, viseq.is_audio_analyzing, list(viseq.monitor_players))
    viseq.monitor_players.clear()
    try:
        viseq.is_audio_analyzing = False
        viseq.is_playing = True
        assert viseq.frame_sleep() == viseq.FRAME_SLEEP_ANIMATED, "sequencer running -> full rate"
        viseq.is_playing = False
        viseq.is_audio_analyzing = True
        assert viseq.frame_sleep() == viseq.FRAME_SLEEP_ANIMATED, "spectrum on -> full rate"
        viseq.is_audio_analyzing = False
        # a monitor whose video is playing keeps full rate; a paused one does not
        viseq.global_vimix_state["sources"] = {
            "0": {"name": "clip_0", "index": 0, "alpha": 0.5, "seek": 0.5, "speed": 1.0}
        }
        viseq.monitor_players.append(
            {
                "id": 99,
                "tag": "monitor_player_99",
                "target_id": "clip_0",
                "props": ["alpha", "seek", "speed"],
                "disc_angle": 0.0,
                "disc_last": 0.0,
                "prev_seek": 0.0,
            }
        )
        assert viseq.frame_sleep() == viseq.FRAME_SLEEP_ANIMATED, "advancing seek -> full rate"
        # pause: seek no longer advances past prev_seek -> idle
        viseq.global_vimix_state["sources"]["0"]["seek"] = 0.0
        assert viseq.frame_sleep() == viseq.FRAME_SLEEP_IDLE, "paused video -> throttled"
    finally:
        viseq.is_playing, viseq.is_audio_analyzing, viseq.monitor_players = (
            saved[0],
            saved[1],
            saved[2],
        )


# ---------- e07s03 (P2): monitor refresh skip-unchanged ----------
def _seed_monitor(player_id=7, seek=0.2, alpha=0.5, speed=1.0, play=1):
    viseq.global_vimix_state["sources"] = {
        "0": {
            "name": "clip_0",
            "index": 0,
            "alpha": alpha,
            "seek": seek,
            "speed": speed,
            "play": play,
        }
    }
    player = {
        "id": player_id,
        "tag": f"monitor_player_{player_id}",
        "target_id": "clip_0",
        "props": ["alpha", "seek", "speed"],
        "disc_angle": 0.0,
        "disc_last": 0.0,
        "prev_seek": 0.0,
    }
    viseq.monitor_players.append(player)
    return player


def test_monitor_refresh_skips_unchanged_configure():
    saved_players = list(viseq.monitor_players)
    saved_sources = dict(viseq.global_vimix_state["sources"])
    viseq.monitor_players.clear()
    try:
        player = _seed_monitor(play=0)  # paused: the arm does not spin
        dpg.calls.clear()
        viseq.refresh_monitor_display(player["id"])  # first refresh: configure everything
        first = [n for n, a, kw in dpg.calls if n == "configure_item"]
        assert first, "first refresh must configure the widgets"
        dpg.calls.clear()
        viseq.refresh_monitor_display(player["id"])  # unchanged values
        second = [n for n, a, kw in dpg.calls if n == "configure_item"]
        assert second == [], "unchanged values must not re-configure anything"
    finally:
        viseq.monitor_players = saved_players
        viseq.global_vimix_state["sources"] = saved_sources


def test_monitor_refresh_updates_only_changed_seek():
    saved_players = list(viseq.monitor_players)
    saved_sources = dict(viseq.global_vimix_state["sources"])
    viseq.monitor_players.clear()
    try:
        player = _seed_monitor(play=0)  # paused: only value changes may configure
        dpg.calls.clear()
        viseq.refresh_monitor_display(player["id"])  # prime caches
        dpg.calls.clear()
        viseq.global_vimix_state["sources"]["0"]["seek"] = 0.8
        viseq.refresh_monitor_display(player["id"])
        configs = [(a, kw) for n, a, kw in dpg.calls if n == "configure_item"]
        assert configs, "the changed seek must be configured"
        assert all("seek" in a[0] for a, kw in configs), "only the seek fill may update"
    finally:
        viseq.monitor_players = saved_players
        viseq.global_vimix_state["sources"] = saved_sources


def test_monitor_refresh_arm_only_while_playing():
    saved_players = list(viseq.monitor_players)
    saved_sources = dict(viseq.global_vimix_state["sources"])
    viseq.monitor_players.clear()
    try:
        player = _seed_monitor(play=1)
        dpg.calls.clear()
        viseq.refresh_monitor_display(player["id"])
        assert any("mon_arm" in a[0] for n, a, kw in dpg.calls if n == "configure_item"), (
            "a playing video must spin the disc arm"
        )
        # paused: arm must not be configured again
        viseq.global_vimix_state["sources"]["0"]["play"] = 0
        player["prev_seek"] = 0.8  # mimic the just-updated seek so it reads as paused
        dpg.calls.clear()
        viseq.refresh_monitor_display(player["id"])
        assert not any("mon_arm" in a[0] for n, a, kw in dpg.calls if n == "configure_item"), (
            "a paused video must not re-configure the arm"
        )
    finally:
        viseq.monitor_players = saved_players
        viseq.global_vimix_state["sources"] = saved_sources


# ---------- e08: Help menubar + centered About window ----------
def test_help_logo_constant_exact_art():
    logo = viseq.HELP_ASCII_LOGO
    lines = logo.split("\n")
    assert len(lines) == 8, "the logo must keep all 8 supplied lines"
    assert max(len(line) for line in lines) == 53, "the logo must keep its 53-char width"
    assert lines[0].lstrip().startswith("___"), "the logo must start with the art, not a label"
    assert lines[-1].strip().startswith("\\|_________|"), "the logo must end with the art"


def test_centered_window_pos_math():
    # even viewport/window: exact center
    assert viseq.centered_window_pos(1700, 1080, 540, 260) == (580, 410)
    # odd viewport: floor the offset
    assert viseq.centered_window_pos(1701, 1081, 540, 260) == (580, 410)
    # window wider than the viewport -> clamp at 0, never negative
    assert viseq.centered_window_pos(500, 400, 540, 260) == (0, 70)
    assert viseq.centered_window_pos(500, 400, 540, 500) == (0, 0)
    # exact fit -> (0, 0)
    assert viseq.centered_window_pos(540, 260, 540, 260) == (0, 0)


def test_help_window_hidden_closable_not_in_layout():
    w = import_time_windows.get("help_window")
    assert w, "help window must be tagged help_window"
    assert w.get("show") is False, "help window must be hidden by default"
    assert w.get("label") == "Help"
    assert not w.get("no_close"), "help window must be closable with X"
    assert "help_window" not in viseq.LAYOUT_WINDOW_TAGS, (
        "the About dialog must not join the layout save/restore tracking"
    )


def test_menubar_help_entry_wired():
    items = {kw.get("label"): kw.get("callback") for kw in import_time_menu_items}
    assert items.get("Help") == viseq.show_help_window, (
        "Help menubar entry must open the help window"
    )


def test_help_window_content():
    texts = [t for t in import_time_texts if isinstance(t, str)]
    assert viseq.HELP_ASCII_LOGO in texts, "the About window must embed the ASCII logo"
    assert any("GPL-3.0" in t for t in texts), "license line must mention GPL-3.0"
    assert any("Luca Franceschini" in t for t in texts), "author line must name the creator"
    assert any("Lupin3rd" in t for t in texts), "author line must include the alias"


def test_show_help_window_centers_and_shows(monkeypatch):
    monkeypatch.setattr(dpg, "get_viewport_width", lambda: 1700)
    monkeypatch.setattr(dpg, "get_viewport_height", lambda: 1080)
    monkeypatch.setattr(dpg, "get_item_width", lambda tag: 540)
    monkeypatch.setattr(dpg, "get_item_height", lambda tag: 260)
    dpg.calls.clear()
    viseq.show_help_window()
    assert any(
        n == "set_item_pos" and a == ("help_window", (580, 410)) for n, a, kw in dpg.calls
    ), "the callback must re-center the window on the viewport"
    assert any(n == "show_item" and a == ("help_window",) for n, a, kw in dpg.calls), (
        "the callback must show the window"
    )


# ---------- e08s02: version line + full English UI pass ----------
def test_app_version_constant():
    assert viseq.APP_VERSION == "1.1.0", "APP_VERSION must match the release-plan target"
    assert isinstance(viseq.APP_VERSION, str)


def test_help_window_version_line():
    texts = [t for t in import_time_texts if isinstance(t, str)]
    assert f"Version: {viseq.APP_VERSION}" in texts, "the About window must show the version"


def test_help_window_lines_english():
    texts = [t for t in import_time_texts if isinstance(t, str)]
    assert any("for Vimix" in t for t in texts), "the About title must be English"
    assert "License: GPL-3.0" in texts, "the license line must be English"
    assert "Created by: Luca Franceschini aka Lupin3rd" in texts, "the author line must be English"
    assert not any("per Vimix" in t or "Licenza" in t or "Creato da" in t for t in texts), (
        "no Italian About-window lines may remain"
    )


ITALIAN_UI_TERMS = (
    "Scuro",
    "Chiaro",
    "Personalizzato",
    "Rilevazione",
    "Battito",
    "BPM Manuale",
    "Finestre",
    "Salva layout",
    "Ripristina",
    "Licenza",
    "Creato da",
    "per Vimix",
)


def test_no_italian_ui_strings_remain():
    labels = [t for t in import_time_ui_labels if isinstance(t, str)]
    labels += [t for t in import_time_texts if isinstance(t, str)]
    labels += list(viseq.THEME_PRESET_LABELS.values())
    labels += list(viseq.BEAT_SOURCE_LABELS.values())
    joined = "\n".join(labels)
    for term in ITALIAN_UI_TERMS:
        assert term not in joined, f"Italian UI string still present: {term!r}"


def test_beat_source_labels_english():
    assert viseq.BEAT_SOURCE_LABELS[viseq.BEAT_SOURCE_ANALYSIS] == "BPM Detection"
    assert viseq.BEAT_SOURCE_LABELS[viseq.BEAT_SOURCE_MANUAL] == "Manual BPM"
    band_keys = {1: viseq.BEAT_SOURCE_BAND1, 2: viseq.BEAT_SOURCE_BAND2, 3: viseq.BEAT_SOURCE_BAND3}
    for band, key in band_keys.items():
        assert viseq.BEAT_SOURCE_LABELS[key] == f"Beat Band {band}"


def test_theme_preset_labels_english():
    assert viseq.THEME_PRESET_LABELS == {"scuro": "Dark", "chiaro": "Light", "custom": "Custom"}
    # the label -> key mapping must still resolve after the English rename
    assert viseq._preset_key("Dark") == "scuro"
    assert viseq._preset_key("Light") == "chiaro"
    assert viseq._preset_key("Custom") == "custom"


# ---------- e09s01: MIDI mapping engine ----------
def _mk_binding(device="", channel=0, type_="note", number=36, action="seq_toggle", params=None):
    return {
        "device": device,
        "channel": channel,
        "type": type_,
        "number": number,
        "action": action,
        "params": params or {},
    }


def test_midi_config_defaults_merge(monkeypatch, tmp_path):
    # a config with no midi section (pre-e09 file) must load with midi defaults
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"layout": {"restore_on_boot": False, "windows": []}}))
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    cfg = viseq.load_config()
    assert cfg["midi"]["enabled"] is False
    assert cfg["midi"]["input_port"] is None
    assert cfg["midi"]["bindings"] == []


def test_midi_config_round_trip(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    cfg = viseq.load_config()
    cfg["midi"]["enabled"] = True
    cfg["midi"]["input_port"] = "Launchpad MK2 MIDI 1"
    cfg["midi"]["bindings"] = [_mk_binding(device="Launchpad MK2 MIDI 1", number=0)]
    viseq.save_config(cfg)
    loaded = viseq.load_config()
    assert loaded["midi"]["enabled"] is True
    assert loaded["midi"]["input_port"] == "Launchpad MK2 MIDI 1"
    assert loaded["midi"]["bindings"][0]["number"] == 0


def test_binding_matches_exact():
    b = _mk_binding(channel=0, type_="note", number=36)
    assert viseq.binding_matches(b, "note", 36, 0)
    assert not viseq.binding_matches(b, "note", 37, 0), "wrong number must not match"
    assert not viseq.binding_matches(b, "note", 36, 1), "wrong channel must not match"
    assert not viseq.binding_matches(b, "cc", 36, 0), "wrong type must not match"


def test_resolve_note_on_edge_semantics():
    dpg.calls.clear()
    saved = viseq.midi_bindings[:]
    viseq.midi_bindings[:] = [_mk_binding(device="Port A", number=36, params={"row": 0, "col": 0})]
    try:
        import mido

        on = mido.Message("note_on", note=36, velocity=100, channel=0)
        off = mido.Message("note_on", note=36, velocity=0, channel=0)
        note_off = mido.Message("note_off", note=36, channel=0)
        wrong_port = mido.Message("note_on", note=36, velocity=100, channel=0)
        wrong_note = mido.Message("note_on", note=37, velocity=100, channel=0)
        result = [("seq_toggle", {"row": 0, "col": 0}, 100)]
        assert viseq.resolve_midi_message(on, "Port A") == result
        assert viseq.resolve_midi_message(off, "Port A") == [], "release edge must not fire"
        assert viseq.resolve_midi_message(note_off, "Port A") == [], "note_off must not fire"
        assert viseq.resolve_midi_message(wrong_port, "Port B") == [], "device filter"
        assert viseq.resolve_midi_message(wrong_note, "Port A") == [], "wrong note"
        wildcard = _mk_binding(device="", number=40, action="transport_play")
        viseq.midi_bindings[:] = [wildcard]
        any_port = mido.Message("note_on", note=40, velocity=1, channel=0)
        assert viseq.resolve_midi_message(any_port, "Anything") == [("transport_play", {}, 1)]
    finally:
        viseq.midi_bindings[:] = saved


def test_resolve_cc_carries_value():
    saved = viseq.midi_bindings[:]
    viseq.midi_bindings[:] = [_mk_binding(type_="cc", number=7, action="volume")]
    try:
        import mido

        msg = mido.Message("control_change", control=7, value=64, channel=0)
        assert viseq.resolve_midi_message(msg, "Knob") == [("volume", {}, 64)]
    finally:
        viseq.midi_bindings[:] = saved


def test_midi_action_seq_toggle():
    saved = [t["steps"][0]["active"] for t in viseq.tracks_data]
    viseq.tracks_data[0]["steps"][0]["active"] = False
    dpg.calls.clear()
    viseq.midi_action_seq_toggle(0, 0)
    assert viseq.tracks_data[0]["steps"][0]["active"] is True, "MIDI toggle must flip the step"
    assert dpg.values.get("seq_cb_0_0") is True, "the cell checkbox must stay in sync"
    viseq.midi_action_seq_toggle(0, 0)
    assert viseq.tracks_data[0]["steps"][0]["active"] is False
    for t, a in zip(viseq.tracks_data, saved, strict=True):
        t["steps"][0]["active"] = a


def test_midi_action_transport_and_nudge():
    saved_playing = viseq.is_playing
    viseq.is_playing = False
    viseq.midi_execute(viseq.MIDI_ACTION_TRANSPORT_PLAY, {}, 0)
    assert viseq.is_playing is True, "play action must toggle the transport"
    viseq.is_playing = saved_playing

    saved_step = viseq.current_step
    viseq.current_step = 3
    viseq.midi_execute(viseq.MIDI_ACTION_TRANSPORT_RESYNC, {}, 0)
    assert viseq.current_step == -1, "resync must reset the playhead"
    viseq.current_step = saved_step

    saved_bpm = viseq.current_bpm
    viseq.current_bpm = 120.0
    viseq.midi_action_transport_tap()
    viseq.midi_action_transport_tap()
    assert viseq.current_bpm != 120.0, "two taps must compute a BPM"
    viseq.current_bpm = saved_bpm


def test_midi_action_beat_source_and_track_assign():
    saved_beat = viseq.beat_source
    viseq.midi_action_beat_source(viseq.BEAT_SOURCE_MANUAL)
    assert viseq.beat_source == viseq.BEAT_SOURCE_MANUAL
    viseq.beat_source = saved_beat

    saved_state = dict(viseq.global_vimix_state)
    saved_target = viseq.tracks_data[0].get("target_id")
    viseq.global_vimix_state["current_source"] = "0"
    viseq.global_vimix_state["sources"] = {"0": {"name": "clipA", "uri": "u"}}
    viseq.midi_execute(viseq.MIDI_ACTION_TRACK_ASSIGN, {"row": 0}, 0)
    assert viseq.tracks_data[0]["target_id"] == "clipA"
    viseq.global_vimix_state.clear()
    viseq.global_vimix_state.update(saved_state)
    viseq.tracks_data[0]["target_id"] = saved_target


def test_midi_learn_complete_merges_source_and_action():
    viseq.midi_learn_pending = ("seq_toggle", {"row": 2, "col": 3})
    viseq.midi_bindings.clear()
    dpg.calls.clear()
    source = {"device": "Launchpad", "channel": 0, "type": "note", "number": 23}
    viseq.midi_learn_complete(source)
    assert len(viseq.midi_bindings) == 1
    b = viseq.midi_bindings[0]
    assert b["action"] == "seq_toggle" and b["params"] == {"row": 2, "col": 3}
    assert b["number"] == 23 and b["device"] == "Launchpad"
    assert viseq.midi_learn_pending is None, "the pending slot must clear after a capture"
    viseq.midi_bindings.clear()


def test_midi_init_from_config_and_enable_persist(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    cfg = viseq.load_config()
    cfg["midi"]["enabled"] = True
    cfg["midi"]["input_port"] = "Device1"
    cfg["midi"]["bindings"] = [_mk_binding(device="Device1", number=1)]
    viseq.save_config(cfg)
    viseq.midi_init_from_config(viseq.load_config())
    assert viseq.midi_enabled is True
    assert viseq.midi_input_port == "Device1"
    assert len(viseq.midi_bindings) == 1
    # disable via the public toggle persists
    viseq.set_midi_enabled(False)
    assert viseq.midi_enabled is False
    assert viseq.load_config()["midi"]["enabled"] is False
    # restore the shared mirrors for other tests
    viseq.midi_init_from_config(viseq.load_config())


def test_midi_first_input_discovery(monkeypatch):
    import mido

    monkeypatch.setattr(mido, "get_input_names", lambda: ["Launchpad X MIDI 1"])
    assert mido.get_input_names() == ["Launchpad X MIDI 1"], "discovery must list devices"


# ---------- e09s02: MIDI Learn + menu + Mappings window ----------
def test_midi_menubar_item_opens_window():
    items = {kw.get("label"): kw.get("callback") for kw in import_time_menu_items}
    assert items.get("MIDI") == viseq.show_midi_window, (
        "a single MIDI menubar item opens the window"
    )
    assert "Learn mapping..." not in items, "no scattered learn item in the menubar"
    assert "Mappings..." not in items, "no scattered mappings item in the menubar"
    assert "Save" not in items, "no scattered save item in the menubar"
    assert not any(kw.get("label") == "MIDI" for kw in import_time_menus), (
        "the MIDI features are not a menu anymore"
    )


def test_midi_window_hidden_with_all_controls():
    w = import_time_windows.get("midi_window")
    assert w, "the MIDI window must exist"
    assert w.get("show") is False, "the MIDI window must be hidden by default"
    assert import_time_midi_enable_cb, "Enable MIDI checkbox must live in the window"
    assert import_time_midi_enable_cb[0].get("callback") == viseq.on_midi_enable
    assert import_time_midi_enable_cb[0].get("default_value") is False, "MIDI starts disabled"
    assert import_time_midi_learn_btn, "Learn mapping... button must live in the window"
    assert import_time_midi_learn_btn[0].get("callback") == viseq.toggle_midi_learn
    assert import_time_midi_save_btn, "Save button must live in the window"
    assert import_time_midi_refresh_btn, "Refresh button must live in the window"
    assert import_time_midi_group, "the mappings list group must live in the window"
    assert import_time_midi_status, "the learn status text must live in the window"


def test_midi_window_hidden():
    w = import_time_windows.get("midi_window")
    assert w, "the MIDI window must exist"
    assert w.get("show") is False, "the MIDI window must be hidden by default"


def test_learnable_delegates_when_off_and_captures_when_on():
    executed = []

    def fake_cb(sender, app_data, user_data):
        executed.append(user_data)

    wrapped = viseq.learnable(fake_cb, lambda ud: ("seq_toggle", {"row": ud[0], "col": ud[1]}))
    saved_mode, saved_pending = viseq.midi_learn_mode, viseq.midi_learn_pending
    try:
        viseq.midi_learn_mode = False
        wrapped(None, True, (1, 2))
        assert executed == [(1, 2)], "learn off must delegate to the real callback"

        viseq.midi_learn_mode = True
        wrapped(None, True, (3, 4))
        assert viseq.midi_learn_pending == ("seq_toggle", {"row": 3, "col": 4}), (
            "learn on must capture the action instead of executing"
        )
        assert executed == [(1, 2)], "the real callback must not run in learn mode"
    finally:
        viseq.midi_learn_mode, viseq.midi_learn_pending = saved_mode, saved_pending


def test_learnable_wired_on_step_cell_and_transport():
    saved_mode, saved_pending = viseq.midi_learn_mode, viseq.midi_learn_pending
    try:
        assert import_time_seq_cb_0_0, "step cell checkbox must be captured at import"
        cb = import_time_seq_cb_0_0[0].get("callback")
        viseq.midi_learn_mode = True
        cb(None, True, (0, 0))
        assert viseq.midi_learn_pending == ("seq_toggle", {"row": 0, "col": 0})
    finally:
        viseq.midi_learn_mode, viseq.midi_learn_pending = saved_mode, saved_pending


def test_toggle_midi_learn_requires_enabled_and_cycles():
    saved_mode, saved_enabled = viseq.midi_learn_mode, viseq.midi_enabled
    viseq.midi_learn_mode = False
    viseq.midi_enabled = False
    dpg.calls.clear()
    dpg.values.pop("midi_learn_status", None)
    viseq.toggle_midi_learn()
    assert viseq.midi_learn_mode is False, "learn must be refused while MIDI is disabled"
    assert "Enable MIDI first" in dpg.values.get("midi_learn_status", ""), (
        "the status must explain why learn was refused"
    )
    viseq.midi_enabled = True
    viseq.toggle_midi_learn()
    assert viseq.midi_learn_mode is True
    assert viseq.midi_learn_pending is None, "entering learn mode clears any stale pending"
    viseq.toggle_midi_learn()
    assert viseq.midi_learn_mode is False, "the button doubles as Cancel"
    viseq.midi_learn_mode, viseq.midi_enabled = saved_mode, saved_enabled


def test_midi_learn_end_to_end():
    import mido

    saved_mode, saved_pending = viseq.midi_learn_mode, viseq.midi_learn_pending
    saved_bindings = list(viseq.midi_bindings)
    viseq.midi_bindings.clear()
    try:
        viseq.midi_learn_mode = True
        # 1) click a viseq control -> pending action
        import_time_seq_cb_0_0[0]["callback"](None, True, (0, 0))
        assert viseq.midi_learn_pending == ("seq_toggle", {"row": 0, "col": 0})
        # 2) press a MIDI button -> worker captures, main thread stores the binding
        viseq.handle_midi_message(
            mido.Message("note_on", note=36, velocity=100, channel=0), "Launchpad"
        )
        while not viseq.ui_task_queue.empty():
            viseq.ui_task_queue.get()()
        assert len(viseq.midi_bindings) == 1
        b = viseq.midi_bindings[0]
        assert b["action"] == "seq_toggle" and b["params"] == {"row": 0, "col": 0}
        assert b["device"] == "Launchpad" and b["number"] == 36 and b["channel"] == 0
        assert viseq.midi_learn_pending is None
    finally:
        viseq.midi_learn_mode, viseq.midi_learn_pending = saved_mode, saved_pending
        viseq.midi_bindings[:] = saved_bindings


def test_midi_binding_label_readable():
    b = _mk_binding(device="Launchpad", number=36, action="seq_toggle", params={"row": 1, "col": 2})
    assert viseq._midi_binding_label(b) == "Launchpad note 36 -> seq_toggle {'row': 1, 'col': 2}"


def test_midi_mappings_list_and_delete():
    saved = list(viseq.midi_bindings)
    viseq.midi_bindings[:] = [
        _mk_binding(device="D", number=1, action="seq_toggle", params={"row": 0, "col": 0}),
        _mk_binding(device="D", type_="cc", number=7, action="volume"),
    ]
    try:
        dpg.calls.clear()
        viseq.refresh_midi_mappings_ui()
        texts = [a[0] for n, a, kw in dpg.calls if n == "add_text" and a]
        dels = [kw for n, a, kw in dpg.calls if n == "add_button" and kw.get("label") == "Delete"]
        assert any("seq_toggle" in str(t) for t in texts), "each binding must get a row label"
        assert len(dels) == 2, "each binding must get a Delete button"
        dpg.calls.clear()
        viseq.delete_midi_binding(None, None, 0)
        assert len(viseq.midi_bindings) == 1
        assert viseq.midi_bindings[0]["action"] == "volume", "Delete must remove the right row"
    finally:
        viseq.midi_bindings[:] = saved


def test_save_midi_bindings_persists(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    saved = list(viseq.midi_bindings)
    viseq.midi_bindings[:] = [_mk_binding(device="D", number=5)]
    try:
        viseq.save_midi_bindings()
        assert viseq.load_config()["midi"]["bindings"][0]["number"] == 5
    finally:
        viseq.midi_bindings[:] = saved


def test_midi_enable_callback_persists(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    saved = viseq.midi_enabled
    try:
        viseq.on_midi_enable(None, True, None)
        assert viseq.midi_enabled is True
        assert viseq.load_config()["midi"]["enabled"] is True
        viseq.on_midi_enable(None, False, None)
        assert viseq.midi_enabled is False
    finally:
        viseq.midi_enabled = saved


def test_midi_input_port_callback_persists(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    saved = viseq.midi_input_port
    try:
        viseq.on_midi_input_port(None, "Launchpad MK2 MIDI 1", None)
        assert viseq.midi_input_port == "Launchpad MK2 MIDI 1"
        assert viseq.load_config()["midi"]["input_port"] == "Launchpad MK2 MIDI 1"
    finally:
        viseq.midi_input_port = saved


# ---------- e09s03: Novation Launchpad adapter ----------
class FakeMidiOut:
    """Records mido messages sent through the Launchpad output port."""

    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        self.closed = True


def test_launchpad_model_detection():
    assert viseq.launchpad_model_from_name("Launchpad MIDI 1") == viseq.LAUNCHPAD_MK1
    assert viseq.launchpad_model_from_name("Launchpad S MIDI 1") == viseq.LAUNCHPAD_MK1, (
        "the S is an MK1-generation device"
    )
    assert viseq.launchpad_model_from_name("Launchpad MK2 MIDI 1") == viseq.LAUNCHPAD_NOTE_MODE
    assert viseq.launchpad_model_from_name("Launchpad Mini MK2 MIDI 1") == viseq.LAUNCHPAD_NOTE_MODE
    assert viseq.launchpad_model_from_name("Launchpad Pro MIDI 1") == viseq.LAUNCHPAD_NOTE_MODE
    assert viseq.launchpad_model_from_name("Launchpad X MIDI 1") == viseq.LAUNCHPAD_PROGRAMMER_MODE
    assert (
        viseq.launchpad_model_from_name("Launchpad Mini MK3 MIDI 1")
        == viseq.LAUNCHPAD_PROGRAMMER_MODE
    )
    assert viseq.launchpad_model_from_name("Akai APC mini") is None, "non-Launchpad -> no adapter"


def test_launchpad_grid_note_table_mk2():
    saved = viseq.launchpad_protocol
    viseq.launchpad_protocol = viseq.LAUNCHPAD_NOTE_MODE
    try:
        assert viseq.launchpad_grid_note(0, 0) == 0
        assert viseq.launchpad_grid_note(1, 0) == 10
        assert viseq.launchpad_grid_note(0, 7) == 7
        assert viseq.launchpad_grid_note(7, 7) == 77
        grid = {viseq.launchpad_grid_note(r, c) for r in range(8) for c in range(8)}
        expected = {r * 10 + c for r in range(8) for c in range(8)}
        assert grid == expected, "MK2 grid notes are row*10+col (0-79)"
    finally:
        viseq.launchpad_protocol = saved


def test_launchpad_grid_note_table_mk1():
    saved = viseq.launchpad_protocol
    viseq.launchpad_protocol = viseq.LAUNCHPAD_MK1
    try:
        assert viseq.launchpad_grid_note(0, 0) == 0
        assert viseq.launchpad_grid_note(1, 0) == 16
        assert viseq.launchpad_grid_note(0, 7) == 7
        assert viseq.launchpad_grid_note(7, 7) == 119
        grid = {viseq.launchpad_grid_note(r, c) for r in range(8) for c in range(8)}
        expected = {r * 16 + c for r in range(8) for c in range(8)}
        assert grid == expected, "MK1 grid notes are row*16+col (0-119 grid)"
    finally:
        viseq.launchpad_protocol = saved


def test_launchpad_led_sends_note_on(monkeypatch):
    out = FakeMidiOut()
    saved_out = viseq.launchpad_out
    saved_proto = viseq.launchpad_protocol
    viseq.launchpad_out = out
    viseq.launchpad_protocol = viseq.LAUNCHPAD_NOTE_MODE
    try:
        viseq.launchpad_led(0, 0, viseq.LAUNCHPAD_LED_GREEN)
        assert out.sent and out.sent[0].type == "note_on"
        assert out.sent[0].note == 0 and out.sent[0].velocity == 60, "MK2 green = 60"
        out.sent.clear()
        viseq.launchpad_led(7, 7, viseq.LAUNCHPAD_LED_OFF)
        assert out.sent[0].note == 77 and out.sent[0].velocity == 0
        # MK1: row*16 grid and the official palette (green full = 60, amber full = 63)
        viseq.launchpad_protocol = viseq.LAUNCHPAD_MK1
        out.sent.clear()
        viseq.launchpad_led(1, 0, viseq.LAUNCHPAD_LED_GREEN)
        assert out.sent[0].note == 16 and out.sent[0].velocity == 60, "MK1 green = 60"
        out.sent.clear()
        viseq.launchpad_led(7, 7, viseq.LAUNCHPAD_LED_AMBER)
        assert out.sent[0].note == 119 and out.sent[0].velocity == 63, "MK1 amber = 63"
        out.sent.clear()
        viseq.launchpad_led(0, 0, viseq.LAUNCHPAD_LED_OFF)
        assert out.sent[0].velocity == 12, "MK1 off = 12"
    finally:
        viseq.launchpad_out = saved_out
        viseq.launchpad_protocol = saved_proto


def test_launchpad_mirror_step_colors():
    out = FakeMidiOut()
    saved_out = viseq.launchpad_out
    saved_proto = viseq.launchpad_protocol
    viseq.launchpad_out = out
    viseq.launchpad_protocol = viseq.LAUNCHPAD_NOTE_MODE
    try:
        viseq.launchpad_mirror_step(2, 3, is_active=True, is_head=False)
        assert out.sent[0].velocity == 60, "active step = green"
        out.sent.clear()
        viseq.launchpad_mirror_step(2, 3, is_active=False, is_head=True)
        assert out.sent[0].velocity == 12, "playhead = amber"
        out.sent.clear()
        viseq.launchpad_mirror_step(2, 3, is_active=False, is_head=False)
        assert out.sent[0].velocity == 0, "empty step = off"
    finally:
        viseq.launchpad_out = saved_out
        viseq.launchpad_protocol = saved_proto


def test_launchpad_mk1_connect_uses_mk1_grid_no_sysex(monkeypatch):
    import mido

    out = FakeMidiOut()
    monkeypatch.setattr(mido, "get_output_names", lambda: ["Launchpad MIDI 1"])
    monkeypatch.setattr(mido, "open_output", lambda name: out)
    saved_out = viseq.launchpad_out
    saved_auto = list(viseq.midi_auto_bindings)
    viseq.launchpad_out = None
    try:
        viseq.launchpad_connect("Launchpad MIDI 1", mido)
        assert not out.sent, "MK1 needs no programmer-mode SysEx"
        assert viseq.launchpad_protocol == viseq.LAUNCHPAD_MK1
        notes = {b["number"] for b in viseq.midi_auto_bindings}
        assert notes == {r * 16 + c for r in range(8) for c in range(8)}, (
            "MK1 grid bindings use row*16+col"
        )
        assert viseq.launchpad_grid_note(1, 0) == 16
    finally:
        viseq.launchpad_disconnect()
        viseq.launchpad_out = saved_out
        viseq.midi_auto_bindings[:] = saved_auto


def test_launchpad_connect_programmer_mode_sends_sysex(monkeypatch):
    import mido

    out = FakeMidiOut()
    monkeypatch.setattr(mido, "get_output_names", lambda: ["Launchpad X MIDI 1"])
    monkeypatch.setattr(mido, "open_output", lambda name: out)
    saved = viseq.launchpad_out
    saved_auto = list(viseq.midi_auto_bindings)
    viseq.launchpad_out = None
    try:
        viseq.launchpad_connect("Launchpad X MIDI 1", mido)
        assert out.sent[0].type == "sysex", "programmer mode must be enabled via SysEx"
        assert list(out.sent[0].data) == viseq.LAUNCHPAD_PROGRAMMER_SYSEX
        assert len(viseq.midi_auto_bindings) == 64, "the 8x8 grid bindings must be registered"
    finally:
        viseq.launchpad_disconnect()
        viseq.launchpad_out = saved
        viseq.midi_auto_bindings[:] = saved_auto


def test_launchpad_grid_binding_resolves_to_seq_toggle():
    saved = list(viseq.midi_auto_bindings)
    viseq.midi_auto_bindings[:] = [
        {
            "device": "LP",
            "channel": 0,
            "type": "note",
            "number": 10,
            "action": "seq_toggle",
            "params": {"row": 1, "col": 0},
            "auto": True,
        }
    ]
    try:
        import mido

        msg = mido.Message("note_on", note=10, velocity=100, channel=0)
        assert viseq.resolve_midi_message(msg, "LP") == [("seq_toggle", {"row": 1, "col": 0}, 100)]
    finally:
        viseq.midi_auto_bindings[:] = saved


def test_launchpad_auto_bindings_not_persisted(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    saved_user = list(viseq.midi_bindings)
    saved_auto = list(viseq.midi_auto_bindings)
    viseq.midi_bindings[:] = [_mk_binding(device="D", number=5)]
    viseq.midi_auto_bindings[:] = [
        {
            "device": "LP",
            "channel": 0,
            "type": "note",
            "number": 0,
            "action": "seq_toggle",
            "params": {"row": 0, "col": 0},
            "auto": True,
        }
    ]
    try:
        viseq.save_midi_bindings()
        cfg = viseq.load_config()
        assert len(cfg["midi"]["bindings"]) == 1, "auto grid bindings must never persist"
        assert cfg["midi"]["bindings"][0]["number"] == 5
    finally:
        viseq.midi_bindings[:] = saved_user
        viseq.midi_auto_bindings[:] = saved_auto


def test_launchpad_flash_playhead_restores(monkeypatch):
    out = FakeMidiOut()
    saved_out = viseq.launchpad_out
    saved_proto = viseq.launchpad_protocol
    saved_step = viseq.current_step
    viseq.launchpad_out = out
    viseq.launchpad_protocol = viseq.LAUNCHPAD_NOTE_MODE
    viseq.current_step = 4
    try:
        viseq.launchpad_flash_playhead()
        assert out.sent and all(m.velocity == 3 for m in out.sent), (
            "the playhead column must flash white (MK2 velocity 3)"
        )
        assert all(m.note in (4, 14, 24, 34, 44, 54, 64, 74) for m in out.sent), (
            "flash must hit the playhead column"
        )
    finally:
        viseq.launchpad_out = saved_out
        viseq.launchpad_protocol = saved_proto
        viseq.current_step = saved_step
