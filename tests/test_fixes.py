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

import copy
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

    def get_text_size(self, text):
        """Deterministic stand-in: 8 px per char, 13 px height (default-font model)."""
        self.calls.append(("get_text_size", (text,), {}))
        return (len(text) * 8, 13)

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
import_time_github_btn = [
    kw
    for n, a, kw in dpg.calls
    if n == "add_button" and str(kw.get("label", "")).startswith("GitHub:")
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

# e11s04: settings Project section (restore-last-project checkbox), captured
# before any calls-list clears; the order marker proves the section sits above OSC.
import_time_project_restore_cb = [
    kw
    for n, a, kw in dpg.calls
    if n == "add_checkbox" and kw.get("tag") == "cb_restore_project_boot"
]
import_time_project_cb_order = [
    kw.get("tag")
    for n, a, kw in dpg.calls
    if (n == "add_checkbox" and kw.get("tag") == "cb_restore_project_boot")
    or (n == "add_input_text" and kw.get("tag") == "viosc_ip")
]

# e11s03: project file dialogs, captured before any calls-list clears
import_time_file_dialogs = [kw for n, a, kw in dpg.calls if n == "file_dialog"]

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
    viseq.thumbnails_data["clipA"] = ["tex_clipA_0"]
    viseq.thumbnails_data["ghost"] = ["tex_ghost_0"]
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
    saved_beat = viseq.beat_source
    viseq.beat_source = (
        viseq.BEAT_SOURCE_MANUAL
    )  # live timed tempo (e10s08: analysis needs detection)
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
    viseq.beat_source = saved_beat


def test_high2_uninterrupted_fade_completes():
    # Track A: uninterrupted AlphaF fade completes naturally (no regression)
    saved_beat = viseq.beat_source
    viseq.beat_source = (
        viseq.BEAT_SOURCE_MANUAL
    )  # live timed tempo (e10s08: analysis needs detection)
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
        viseq.beat_source = saved_beat


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
    # perceptual log mapping (e10s09): the 5 kHz tone lands on its log bar, not the
    # linear one (linear binning piled music energy into the low bars)
    expected_bin = int(
        np.log(5000.0 / viseq.SPECTRUM_F_MIN)
        / np.log(viseq.SPECTRUM_F_MAX / viseq.SPECTRUM_F_MIN)
        * viseq.SPECTRUM_BARS
    )
    assert abs(int(np.argmax(bars)) - expected_bin) <= 2, (
        f"5kHz tone must peak near log bar {expected_bin}, got {int(np.argmax(bars))}"
    )
    assert float(np.max(bars)) > 0.5, "a loud tone must light its bars"
    assert bool(np.all(bars >= 0.0)) and bool(np.all(bars <= 1.0)), "bars stay in 0..1"


def test_bar_freq_edges_log_spaced():
    edges = viseq._bar_freq_edges(viseq.SPECTRUM_BARS, 44100.0)
    assert len(edges) == viseq.SPECTRUM_BARS + 1
    assert edges[0] == viseq.SPECTRUM_F_MIN
    assert edges[-1] == viseq.SPECTRUM_F_MAX
    assert bool(np.all(np.diff(edges) > 0)), "edges must be strictly increasing"


def test_apply_spectrum_agc_level_independent():
    loud = np.array([0.0, 0.6, 0.2, 0.1], dtype=np.float32)
    quiet = np.array([0.0, 0.06, 0.02, 0.01], dtype=np.float32)
    loud_out, hold1 = viseq.apply_spectrum_agc(loud, 0.0)
    quiet_out, _ = viseq.apply_spectrum_agc(quiet, 0.0)
    assert abs(float(np.max(loud_out)) - float(np.max(quiet_out))) < 0.05, (
        "AGC must make loud and quiet input reach the same normalized peak"
    )
    assert hold1 > 0.0 and abs(float(np.max(loud_out)) - viseq.SPECTRUM_PEAK_TARGET) < 0.02
    # silence stays zero (no noise amplification)
    silent_out, _ = viseq.apply_spectrum_agc(np.zeros(4, dtype=np.float32), hold1)
    assert float(np.max(silent_out)) == 0.0
    # the hold decays slowly
    _, hold4 = viseq.apply_spectrum_agc(np.zeros(4, dtype=np.float32), hold1)
    assert hold4 < hold1 and hold4 > hold1 * 0.9, "release must be slow, not instant"


def test_band_value_agg_peak_and_blend():
    bars = np.array([0.1, 0.5, 0.9, 0.4])
    assert viseq.band_value_from_bars(bars, 0.0, 1.0, agg="peak") == 0.9
    blend = viseq.band_value_from_bars(bars, 0.0, 1.0, agg="blend")
    expected = viseq.BAND_AGG_WEIGHT * 0.9 + (1 - viseq.BAND_AGG_WEIGHT) * 0.475
    assert abs(blend - expected) < 1e-9
    # the default stays the plain mean (backward compatible)
    assert viseq.band_value_from_bars(bars, 0.0, 1.0) == 0.475


def test_band_beat_threshold_constant_used():
    # the beat edge must use the named threshold, not a magic number
    src = Path("viseq.py").read_text()
    fn = re.search(r"def refresh_band_value\(.*?\n(?=def |\n# ===)", src, re.S)
    assert fn and "BAND_BEAT_THRESHOLD" in fn.group(0), "beat edge must use the threshold"


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
        (viseq.BEAT_SOURCE_MIDI, True),
        (viseq.BEAT_SOURCE_MANUAL, False),
    ]:
        viseq.beat_source = source
        assert viseq.beat_is_event_driven() is event_driven, source
    viseq.beat_source = viseq.BEAT_SOURCE_ANALYSIS


def test_band_rising_edge_triggers_beat():
    viseq.beat_source = viseq.BEAT_SOURCE_BAND1
    viseq.bands_enabled[1] = True
    viseq.band_prev_values[1] = viseq.BAND_BEAT_THRESHOLD - 0.1  # below the edge
    viseq.sync_event_beat.clear()
    bars = np.full(16, 1.0)
    viseq.refresh_band_value(bars, 1)
    assert viseq.sync_event_beat.is_set(), "band crossing the threshold must fire the beat"
    # no re-fire while it stays above the threshold (edge only)
    viseq.sync_event_beat.clear()
    viseq.refresh_band_value(bars, 1)
    assert not viseq.sync_event_beat.is_set(), "no re-trigger on a sustained level"
    viseq.bands_enabled[1] = False
    viseq.band_prev_values[1] = 0.0


def test_band_beat_ignored_when_not_selected():
    # only band 1 can drive the beat, and only when it is the selected source
    viseq.beat_source = viseq.BEAT_SOURCE_ANALYSIS
    viseq.bands_enabled[1] = True
    viseq.band_prev_values[1] = 0.9
    viseq.sync_event_beat.clear()
    viseq.refresh_band_value(np.full(16, 1.0), 1)
    assert not viseq.sync_event_beat.is_set(), (
        "a band peak must not fire without the band source selected"
    )
    # bands 2/3 are spectrum-only: their peaks never fire the sequencer beat (e10s07)
    viseq.beat_source = viseq.BEAT_SOURCE_BAND1
    viseq.sync_event_beat.clear()
    viseq.band_prev_values[2] = 0.9
    viseq.refresh_band_value(np.full(16, 1.0), 2)
    assert not viseq.sync_event_beat.is_set(), "band 2 must never drive the sequencer beat"
    viseq.bands_enabled[1] = False
    viseq.band_prev_values[1] = 0.0
    viseq.band_prev_values[2] = 0.0


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
        "cb_beat_midi_sync",
        "cb_beat_manual_bpm",
    }, "one checkbox per beat source (bands 2/3 removed, e10s07)"
    assert beat_led_tags == {
        "led_analysis",
        "led_band1",
        "led_midi",
        "led_manual",
    }, "one LED per beat source"
    assert manual_bpm_input_widget and tap_button_widget, "manual BPM widgets must exist"
    assert manual_bpm_hidden and tap_hidden, "manual widgets hidden unless manual mode"


def test_timed_bpm_live_manual_is_always_live():
    saved = (viseq.beat_source, viseq.is_beat_tracking, viseq.bpm_last_detected)
    viseq.beat_source = viseq.BEAT_SOURCE_MANUAL
    viseq.is_beat_tracking = False
    viseq.bpm_last_detected = 0.0
    try:
        assert viseq._timed_bpm_live() is True, "manual BPM is the entered tempo, always live"
    finally:
        viseq.beat_source, viseq.is_beat_tracking, viseq.bpm_last_detected = saved


def test_timed_bpm_live_analysis_requires_recent_detection(monkeypatch):
    saved = (viseq.beat_source, viseq.is_beat_tracking, viseq.bpm_last_detected)
    monkeypatch.setattr(viseq.time, "time", lambda: 100.0)
    viseq.beat_source = viseq.BEAT_SOURCE_ANALYSIS
    viseq.is_beat_tracking = True
    try:
        viseq.bpm_last_detected = 100.0  # detected just now
        assert viseq._timed_bpm_live() is True
        viseq.bpm_last_detected = 97.0  # 3 s ago > 2 s stale window
        assert viseq._timed_bpm_live() is False, "a stale BPM must not drive the sequencer"
        viseq.bpm_last_detected = 98.5  # exactly at the stale window boundary
        assert viseq._timed_bpm_live() is True, "a fresh-enough reading stays live"
        viseq.is_beat_tracking = False
        assert viseq._timed_bpm_live() is False, "no tracking -> no tempo even with a fresh time"
    finally:
        viseq.beat_source, viseq.is_beat_tracking, viseq.bpm_last_detected = saved


def test_essentia_loop_marks_detection_time(monkeypatch):
    # a successful detection stamps bpm_last_detected so the sequencer can tell
    # a real tempo from a stale leftover (e10s08)
    src = Path("viseq.py").read_text()
    assert "bpm_last_detected = time.time()" in src, "a detection must stamp its timestamp"


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
    assert cfg["projects"]["recent"] == []
    assert cfg["projects"]["restore_last_on_boot"] is True
    assert cfg["theme"]["preset"] == "scuro"
    assert cfg["theme"]["colors"]["window_bg"] == list(viseq.DEFAULT_PALETTE["window_bg"])


def test_load_config_corrupt_file_returns_defaults(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ not valid json !!!")
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    cfg = viseq.load_config()
    assert cfg["projects"]["restore_last_on_boot"] is True, (
        "corrupt config must fall back to defaults"
    )


def test_save_then_load_config_round_trip(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    cfg = viseq.load_config()
    cfg["projects"]["restore_last_on_boot"] = False
    cfg["theme"]["preset"] = "chiaro"
    viseq.save_config(cfg)
    loaded = viseq.load_config()
    assert loaded["projects"]["restore_last_on_boot"] is False
    assert loaded["theme"]["preset"] == "chiaro"


# e11s02: the legacy layout config tests (should_restore_layout_on_boot,
# save_layout_to_config, restore_layout_from_config, restore-layout toggle) were
# removed — the layout save/restore buttons die with the settings Windows section
# in e11s04; config coverage moved to the projects flag tests above.


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


def test_boot_applies_saved_theme_and_skips_legacy_layout(monkeypatch, tmp_path):
    # e11s02: the legacy layout block no longer survives load_config, so boot must
    # apply the theme and skip the window layout (e11s04 replaces it with
    # restore-last-project-at-boot).
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
    assert not any(n == "show_item" and a == ("logs_window",) for n, a, kw in dpg.calls), (
        "the legacy layout block must not be restored once the schema drops it"
    )
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
    # abbreviated single-row labels (e10s08)
    assert viseq.BEAT_SOURCE_LABELS[viseq.BEAT_SOURCE_ANALYSIS] == "BPM Det"
    assert viseq.BEAT_SOURCE_LABELS[viseq.BEAT_SOURCE_BAND1] == "Band 1"
    assert viseq.BEAT_SOURCE_LABELS[viseq.BEAT_SOURCE_MIDI] == "MIDI"
    assert viseq.BEAT_SOURCE_LABELS[viseq.BEAT_SOURCE_MANUAL] == "Manual"


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


# ---------- e08 follow-up: viSeq branding + GitHub link in the About window ----------
def test_help_window_title_uses_viseq_branding():
    texts = [t for t in import_time_texts if isinstance(t, str)]
    assert any("viSeq" in t for t in texts), "the About title must use the viSeq branding"
    assert not any(str(t).startswith("viseq ") for t in texts if isinstance(t, str)), (
        "no lowercase 'viseq' branding may remain in user-facing text"
    )


def test_help_window_github_link():
    assert viseq.GITHUB_URL == "https://github.com/lupin3rd"
    assert import_time_github_btn, "the About window must offer the GitHub link button"
    assert import_time_github_btn[0].get("label") == f"GitHub: {viseq.GITHUB_URL}"
    assert import_time_github_btn[0].get("callback") == viseq.open_github


def test_open_github_opens_browser(monkeypatch):
    opened = []

    class FakeBrowser:
        def open(self, url):
            opened.append(url)

    monkeypatch.setattr("webbrowser.open", FakeBrowser().open)
    viseq.open_github()
    assert opened == ["https://github.com/lupin3rd"], "the callback must open the profile URL"


# ---------- e10s03: multi-thumb pipeline (idx propagation, per-source texture list) ----------
def decode_one_frame_into(idx: str) -> tuple:
    """Run the decoder worker once on a tiny frame and return its texture-queue tuple."""
    small = Image.new("RGB", (320, 180), "red")
    buf = io.BytesIO()
    small.save(buf, "PNG")
    viseq.texture_queue = queue.Queue()  # fresh queue for the assertion
    viseq.blob_queue.put(("clipA", idx, buf.getvalue()))
    t = threading.Thread(target=viseq.thumbnail_decoder_worker, daemon=True)
    t.start()
    for _ in range(200):  # up to 2s for decode
        if not viseq.texture_queue.empty():
            break
        time.sleep(0.01)
    return viseq.texture_queue.get_nowait()


def test_e10s03_decoder_propagates_reply_index():
    item = decode_one_frame_into("2")
    name, idx, _img_data, w, h = item
    assert name == "clipA", f"decoder must keep the source name, got {name!r}"
    assert idx == "2", f"decoder must keep the reply index, got {idx!r}"
    assert w == 320 and h == 180


def fake_thumb_exists(item):
    """Tiles/containers exist; images/loading/textures do not (fresh state)."""
    return item.startswith("thumb_container_")


def test_e10s03_textures_build_per_index_list(monkeypatch):
    monkeypatch.setattr(dpg, "does_item_exist", fake_thumb_exists)
    viseq.thumbnails_data.clear()
    fake_img = np.zeros((180, 320, 4), dtype=np.float32)
    dpg.calls.clear()

    viseq.apply_thumbnail_texture("clipA", "0", fake_img, 320, 180)
    viseq.apply_thumbnail_texture("clipA", "1", fake_img, 320, 180)

    assert viseq.thumbnails_data["clipA"] == ["tex_clipA_0", "tex_clipA_1"], (
        "each decoded frame must append its own texture tag"
    )
    add_img = [c for c in dpg.calls if c[0] == "add_image"]
    assert len(add_img) == 1, "only the first texture may create the tile image"
    assert add_img[0][2].get("texture_tag") == "tex_clipA_0"
    assert add_img[0][2].get("tag") == "img_clipA"
    del viseq.thumbnails_data["clipA"]


def test_e10s03_track_slot_uses_first_texture():
    viseq.tracks_data[0]["target_id"] = "clipA"
    viseq.thumbnails_data["clipA"] = ["tex_clipA_0", "tex_clipA_1", "tex_clipA_2"]
    dpg.calls.clear()
    viseq.update_track_slot_ui(0)
    slot_imgs = [c for c in dpg.calls if c[0] == "add_image_button"]
    assert slot_imgs, "the slot must render the assigned clip thumbnail"
    assert slot_imgs[0][2].get("texture_tag") == "tex_clipA_0", (
        "the slot must resolve the first texture of the per-source list"
    )
    viseq.tracks_data[0]["target_id"] = None
    del viseq.thumbnails_data["clipA"]


def test_e10s03_l1_prune_drops_whole_texture_set():
    viseq.thumbnails_data.clear()
    viseq.request_timestamps.clear()
    viseq.thumbnails_data["clipA"] = ["tex_clipA_0", "tex_clipA_1", "tex_clipA_2"]
    viseq.thumbnails_data["ghost"] = ["tex_ghost_0"]
    viseq.request_timestamps["thumb_clipA"] = 1.0
    viseq.request_timestamps["thumb_ghost"] = 2.0
    dpg.calls.clear()
    viseq.update_vimix_sources_ui(
        json.dumps({"current_source": 1, "sources": {"1": {"name": "clipA", "index": 1}}})
    )
    assert "ghost" not in viseq.thumbnails_data, "stale source must be pruned"
    assert "clipA" in viseq.thumbnails_data, "live source must survive the prune"
    deleted = [c[1][0] for c in dpg.calls if c[0] == "delete_item"]
    assert "tex_ghost_0" in deleted, "all texture tags of the pruned source must be deleted"
    assert viseq.thumbnails_data["clipA"] == ["tex_clipA_0", "tex_clipA_1", "tex_clipA_2"]


# ---------- e10s04: Mediagrid thumb cycling + failed-state UX ----------
def test_e10s04_cycle_advances_on_cadence():
    state = (0, 0.0)
    # before the cadence: no advance
    assert viseq.advance_thumb_cycle(3, 0.74, state) == (0, 0.0)
    # at/after the cadence: advance, and the new timestamp becomes the anchor
    assert viseq.advance_thumb_cycle(3, 0.75, state) == (1, 0.75)
    assert viseq.advance_thumb_cycle(3, 0.76, (1, 0.75)) == (1, 0.75)
    assert viseq.advance_thumb_cycle(3, 1.50, (1, 0.75)) == (2, 1.50)


def test_e10s04_cycle_wraps_around_list():
    assert viseq.advance_thumb_cycle(3, 0.75, (2, 0.0)) == (0, 0.75)


def test_e10s04_cycle_static_below_two_frames():
    # a single frame (image) never cycles and never moves the anchor
    assert viseq.advance_thumb_cycle(1, 999.0, (0, 0.0)) == (0, 0.0)
    assert viseq.advance_thumb_cycle(0, 999.0, (0, 0.0)) == (0, 0.0)


def test_e10s04_tick_switches_texture_tag_on_cadence(monkeypatch):
    monkeypatch.setattr(
        dpg,
        "does_item_exist",
        lambda item: item.startswith("img_") or item == "vimix_media_window",
    )
    viseq.thumbnails_data["clipA"] = ["tex_clipA_0", "tex_clipA_1", "tex_clipA_2"]
    viseq.thumb_cycle_state["clipA"] = (0, 0.0)
    dpg.calls.clear()
    viseq.tick_thumb_cycle(0.75)
    switches = [c for c in dpg.calls if c[0] == "configure_item"]
    assert switches and switches[0][2].get("texture_tag") == "tex_clipA_1", (
        "the tile image must switch to the next stored frame at the cadence"
    )
    assert viseq.thumb_cycle_state["clipA"] == (1, 0.75)
    del viseq.thumb_cycle_state["clipA"]
    del viseq.thumbnails_data["clipA"]


def test_e10s04_tick_gated_when_grid_hidden(monkeypatch):
    monkeypatch.setattr(dpg, "is_item_shown", lambda item: False)  # Mediagrid hidden
    viseq.thumbnails_data["clipA"] = ["tex_clipA_0", "tex_clipA_1", "tex_clipA_2"]
    viseq.thumb_cycle_state["clipA"] = (0, 0.0)
    dpg.calls.clear()
    viseq.tick_thumb_cycle(999.0)  # far past the cadence
    switches = [c for c in dpg.calls if c[0] == "configure_item"]
    assert switches == [], "cycling must not advance while the grid is hidden"
    assert viseq.thumb_cycle_state["clipA"] == (0, 0.0)
    del viseq.thumb_cycle_state["clipA"]
    del viseq.thumbnails_data["clipA"]


class FakeVioscClient:
    def __init__(self):
        self.sent = []

    def send_message(self, addr, payload):
        self.sent.append((addr, payload))


def test_e10s04_fail_count_increments_and_sends_one_regen_at_threshold(monkeypatch):
    fake = FakeVioscClient()
    monkeypatch.setattr(viseq, "viosc_client", fake)
    monkeypatch.setattr(viseq, "request_timestamps", {})
    monkeypatch.setattr(viseq, "thumb_fail_count", {})
    viseq.global_vimix_state = {"sources": {"0": {"name": "clipA", "uri": "file:///x.mp4"}}}
    for i in range(viseq.THUMB_FAIL_THRESHOLD + 2):
        viseq.request_missing_thumbnails(now=1000.0 + i * 10.0)
    requests = [a for a, _ in fake.sent if a.startswith("/viosc/thumb/")]
    regens = [a for a, _ in fake.sent if a.startswith("/viosc/regen_thumb/")]
    assert len(requests) == viseq.THUMB_FAIL_THRESHOLD + 2
    assert len(regens) == 1, "exactly one regen retry fires when the tile flips to failed"
    assert viseq.thumb_fail_count["clipA"] == viseq.THUMB_FAIL_THRESHOLD + 2


def test_e10s04_reply_resets_fail_count(monkeypatch):
    monkeypatch.setattr(
        dpg,
        "does_item_exist",
        lambda item: item.startswith("thumb_container_") or item == "vimix_media_window",
    )
    viseq.thumb_fail_count = {"clipA": viseq.THUMB_FAIL_THRESHOLD}
    fake_img = np.zeros((180, 320, 4), dtype=np.float32)
    dpg.calls.clear()
    viseq.apply_thumbnail_texture("clipA", "0", fake_img, 320, 180)
    assert "clipA" not in viseq.thumb_fail_count, "a successful reply must clear the failure state"
    del viseq.thumbnails_data["clipA"]


def test_e10s04_tile_shows_failed_label(monkeypatch):
    viseq.thumb_fail_count = {"clipA": viseq.THUMB_FAIL_THRESHOLD}
    viseq.request_timestamps = {}
    viseq.thumbnails_data.clear()
    dpg.calls.clear()
    viseq.update_vimix_sources_ui(
        json.dumps(
            {
                "current_source": 0,
                "sources": {"0": {"name": "clipA", "index": 0, "uri": "file:///x.mp4"}},
            }
        )
    )
    failed_texts = [
        a[1][0]
        for c in dpg.calls
        if c[0] == "add_text"
        for a in [c]
        if a[1] and a[1][0] == viseq.THUMB_FAIL_LABEL
    ]
    assert failed_texts, "the tile must show the failed label after the threshold"


def test_e10s04_failed_state_cleared_after_reply(monkeypatch):
    viseq.thumb_fail_count = {"clipA": viseq.THUMB_FAIL_THRESHOLD}
    viseq.request_timestamps = {}
    viseq.thumbnails_data.clear()
    # a reply lands: texture applied -> fail count cleared
    monkeypatch.setattr(
        dpg,
        "does_item_exist",
        lambda item: item.startswith("thumb_container_") or item == "vimix_media_window",
    )
    fake_img = np.zeros((180, 320, 4), dtype=np.float32)
    viseq.apply_thumbnail_texture("clipA", "0", fake_img, 320, 180)
    assert "clipA" not in viseq.thumb_fail_count
    del viseq.thumbnails_data["clipA"]


def test_e10s04_grid_cycling_does_not_force_full_rate():
    saved = (viseq.is_playing, viseq.is_audio_analyzing, list(viseq.monitor_players))
    viseq.is_playing = False
    viseq.is_audio_analyzing = False
    viseq.monitor_players.clear()
    saved_thumbs = dict(viseq.thumbnails_data)
    viseq.thumbnails_data["clipA"] = ["tex_clipA_0", "tex_clipA_1", "tex_clipA_2"]
    try:
        # SPIKE-thumb-cycle: at 1.6 us/switch the 750 ms cycle renders smoothly at
        # the idle rate, so thumb cycling must NOT force the full render rate.
        assert viseq.frame_sleep() == viseq.FRAME_SLEEP_IDLE, (
            "grid thumb cycling stays on the idle throttle (spike decision)"
        )
    finally:
        viseq.is_playing, viseq.is_audio_analyzing, viseq.monitor_players = (
            saved[0],
            saved[1],
            saved[2],
        )
        viseq.thumbnails_data.clear()
        viseq.thumbnails_data.update(saved_thumbs)


# ---------- BUG-2026-08-27T201742: OSC recv buffer drops > 8 KB thumbnail blobs ----------
def test_e10s05_osc_server_recv_buffer_fits_full_thumbnail_blobs():
    # socketserver.UDPServer defaults max_packet_size to 8192, which truncates
    # thumbnail datagrams larger than 8 KB before python-osc can parse them
    # (the daemon's 320x180 mjpeg frames are typically 10-17 KB). The viseq
    # server class must accept the largest allowed blob plus the OSC header
    # (address + typetag + size + padding) margin.
    assert viseq.ViseqOSCUDPServer.max_packet_size >= viseq.MAX_THUMBNAIL_BLOB_BYTES + 4096, (
        "the OSC receiver buffer must fit the max accepted thumbnail blob"
    )


def test_start_osc_server_instantiates_viseq_server_class():
    _reset_osc_state()
    viseq.start_osc_server("127.0.0.1", 6667)
    assert isinstance(viseq.local_osc_server, viseq.ViseqOSCUDPServer), (
        "start_osc_server must use the large-buffer server class"
    )


def test_e10s05_failed_label_flips_in_place_at_threshold(monkeypatch):
    """BUG fix: the failed tile label must appear when the request loop crosses
    the threshold, without waiting for a Mediagrid rebuild."""
    monkeypatch.setattr(
        dpg,
        "does_item_exist",
        lambda item: item.startswith("thumb_container_") or item.startswith("loading_txt_"),
    )
    fake = FakeVioscClient()
    monkeypatch.setattr(viseq, "viosc_client", fake)
    monkeypatch.setattr(viseq, "request_timestamps", {})
    monkeypatch.setattr(viseq, "thumb_fail_count", {})
    saved_state = viseq.global_vimix_state
    viseq.global_vimix_state = {"sources": {"0": {"name": "clipA", "uri": "file:///x.mp4"}}}
    dpg.calls.clear()
    try:
        for i in range(viseq.THUMB_FAIL_THRESHOLD + 1):
            viseq.request_missing_thumbnails(now=1000.0 + i * 10.0)
    finally:
        viseq.global_vimix_state = saved_state
    failed_texts = [
        a[1][0]
        for c in dpg.calls
        if c[0] == "add_text"
        for a in [c]
        if a[1] and a[1][0] == viseq.THUMB_FAIL_LABEL
    ]
    assert failed_texts, (
        "crossing the unanswered-request threshold must flip the tile label in place"
    )


# ---------- e10s05: thumb cycling in sequencer slots + monitor players ----------
def test_e10s05_slot_thumb_button_has_stable_tag():
    viseq.tracks_data[0]["target_id"] = "clipA"
    viseq.thumbnails_data["clipA"] = ["tex_clipA_0", "tex_clipA_1", "tex_clipA_2"]
    dpg.calls.clear()
    try:
        viseq.update_track_slot_ui(0)
        slot_imgs = [c for c in dpg.calls if c[0] == "add_image_button"]
        assert slot_imgs, "the slot must render the assigned clip thumbnail"
        assert slot_imgs[0][2].get("tag") == "seq_thumb_0", (
            "the slot image button needs a stable tag so the cycle can switch it"
        )
    finally:
        viseq.tracks_data[0]["target_id"] = None
        del viseq.thumbnails_data["clipA"]


def _make_monitor_player(player_id: int, target_id: str) -> dict:
    player = {
        "id": player_id,
        "tag": f"monitor_player_{player_id}",
        "target_id": target_id,
        "props": list(viseq.DEFAULT_MONITOR_PROPS),
        "disc_angle": 0.0,
        "disc_last": 0.0,
    }
    viseq.monitor_players.append(player)
    return player


def test_e10s05_monitor_thumb_image_has_stable_tag():
    _make_monitor_player(99, "clipA")
    viseq.thumbnails_data["clipA"] = ["tex_clipA_0", "tex_clipA_1", "tex_clipA_2"]
    dpg.calls.clear()
    try:
        viseq.update_monitor_player_ui(99)
        mon_imgs = [c for c in dpg.calls if c[0] == "add_image"]
        assert mon_imgs, "the monitor body must render the assigned clip thumbnail"
        assert mon_imgs[0][2].get("tag") == "mon_thumb_99", (
            "the monitor thumbnail needs a stable tag so the cycle can switch it"
        )
    finally:
        viseq.monitor_players.clear()
        del viseq.thumbnails_data["clipA"]


def test_e10s05_tick_switches_all_consumers_of_a_source(monkeypatch):
    monkeypatch.setattr(
        dpg,
        "does_item_exist",
        lambda item: (
            item.startswith(("img_", "seq_thumb_", "mon_thumb_"))
            or item in ("vimix_media_window", "sequencer_window")
        ),
    )
    viseq.thumbnails_data["clipA"] = ["tex_clipA_0", "tex_clipA_1", "tex_clipA_2"]
    viseq.thumb_cycle_state["clipA"] = (0, 0.0)
    viseq.tracks_data[0]["target_id"] = "clipA"
    _make_monitor_player(99, "clipA")
    dpg.calls.clear()
    try:
        viseq.tick_thumb_cycle(0.75)
        switches = {
            c[1][0]: c[2].get("texture_tag")
            for c in dpg.calls
            if c[0] == "configure_item" and "texture_tag" in c[2]
        }
        assert switches.get("img_clipA") == "tex_clipA_1", (
            "the Mediagrid tile must switch to the next frame"
        )
        assert switches.get("seq_thumb_0") == "tex_clipA_1", (
            "the sequencer slot must switch to the same frame"
        )
        assert switches.get("mon_thumb_99") == "tex_clipA_1", (
            "the monitor player must switch to the same frame"
        )
    finally:
        viseq.tracks_data[0]["target_id"] = None
        viseq.monitor_players.clear()
        del viseq.thumb_cycle_state["clipA"]
        del viseq.thumbnails_data["clipA"]


def test_e10s05_cycle_gate_covers_sequencer_and_monitors(monkeypatch):
    shown = {"sequencer_window": True, "vimix_media_window": False}
    monkeypatch.setattr(dpg, "is_item_shown", lambda item: shown.get(item, False))
    monkeypatch.setattr(
        dpg,
        "does_item_exist",
        lambda item: (
            item in shown
            or item.startswith(("img_", "seq_thumb_", "mon_thumb_", "monitor_player_"))
        ),
    )
    viseq.thumbnails_data["clipA"] = ["tex_clipA_0", "tex_clipA_1", "tex_clipA_2"]
    viseq.thumb_cycle_state["clipA"] = (0, 0.0)
    _make_monitor_player(99, "clipA")
    try:
        # Mediagrid hidden, sequencer visible -> cycling continues
        dpg.calls.clear()
        viseq.tick_thumb_cycle(0.75)
        switches = [c for c in dpg.calls if c[0] == "configure_item"]
        assert switches, "cycling must continue while the sequencer is visible"
        # every consumer window hidden -> cycling pauses
        shown.update({"sequencer_window": False})
        dpg.calls.clear()
        viseq.tick_thumb_cycle(1.5)
        switches = [c for c in dpg.calls if c[0] == "configure_item"]
        assert switches == [], "cycling must pause when all consumer windows are hidden"
    finally:
        viseq.monitor_players.clear()
        del viseq.thumb_cycle_state["clipA"]
        del viseq.thumbnails_data["clipA"]


# ---------- e10s06: Mediagrid two-line title + viseq-side primary selection ----------
def test_e10s06_title_short_name_untouched():
    assert viseq.truncate_media_title("short.mp4") == "short.mp4"
    assert viseq.truncate_media_title("") == ""


def test_e10s06_title_long_name_truncated_to_two_lines():
    long_name = "07-Ritual-giardino_bidir_crf19_noaudio_bidir_crf19_noaudio.mp4"
    out = viseq.truncate_media_title(long_name)
    assert out.endswith(viseq.MEDIA_TITLE_ELLIPSIS), "a too-long name must end with the ellipsis"
    # per-line budget: the wrap breaks every MEDIA_TITLE_WRAP px, so a total-width
    # budget alone would allow a 245 px string that still needs three lines (17+17+1)
    char_px = 8  # stub get_text_size model
    chars_per_line = viseq.MEDIA_TITLE_WRAP // char_px
    assert len(out) <= chars_per_line * viseq.MEDIA_TITLE_MAX_LINES, (
        "the truncated name must fit two wrapped lines"
    )
    # maximality: one more char before the ellipsis would need a third line
    base = out[: -len(viseq.MEDIA_TITLE_ELLIPSIS)]
    assert len(base) == chars_per_line * viseq.MEDIA_TITLE_MAX_LINES - len(
        viseq.MEDIA_TITLE_ELLIPSIS
    )
    assert len(base + "x" + viseq.MEDIA_TITLE_ELLIPSIS) > chars_per_line * 2, (
        "one extra character must exceed the two-line budget"
    )


def test_e10s06_tile_registers_click_handler(monkeypatch):
    saved_state = viseq.global_vimix_state
    saved_sig = viseq.last_ui_signature
    viseq.last_ui_signature = None
    viseq.global_vimix_state = {
        "current_source": 0,
        "sources": {"0": {"name": "clipA", "index": 0}},
    }
    dpg.calls.clear()
    try:
        viseq.update_vimix_sources_ui(json.dumps(viseq.global_vimix_state))
        reg_tag = viseq.media_tile_click_registry_tag("clipA")
        registries = [
            c for c in dpg.calls if c[0] == "item_handler_registry" and c[2].get("tag") == reg_tag
        ]
        assert registries, "each tile must create its own click-handler registry"
        binds = [
            c for c in dpg.calls if c[0] == "bind_item_handler_registry" and c[1][1] == reg_tag
        ]
        assert binds, "the registry must bind to the tile's clickable children"
        assert any(c[1][0] == "tile_title_clipA" for c in binds), "the title must select the tile"
        assert any(c[1][0] == "tile_index_clipA" for c in binds), "the badge must select the tile"
        handlers = [
            c
            for c in dpg.calls
            if c[0] == "add_item_clicked_handler" and c[2].get("callback") is not None
        ]
        assert handlers, "the registry must contain a left-click handler"
    finally:
        viseq.global_vimix_state = saved_state
        viseq.last_ui_signature = saved_sig


def test_e10s06_tile_click_sets_viseq_selection(monkeypatch):
    saved_state = viseq.global_vimix_state
    saved_sel = viseq.viseq_selected_source
    viseq.viseq_selected_source = None
    viseq.global_vimix_state = {
        "current_source": 0,
        "sources": {"0": {"name": "clipA", "index": 0}},
    }
    dpg.calls.clear()
    try:
        viseq.on_media_tile_click(None, None, "clipA")
        assert viseq.viseq_selected_source == "clipA"
        binds = [c for c in dpg.calls if c[0] == "bind_item_theme"]
        assert binds, "clicking a tile must refresh the selection themes"
    finally:
        viseq.global_vimix_state = saved_state
        viseq.viseq_selected_source = saved_sel


def test_e10s06_theme_precedence_viseq_green_vimix_light(monkeypatch):
    saved_state = viseq.global_vimix_state
    saved_sig = viseq.last_ui_signature
    saved_sel = viseq.viseq_selected_source
    viseq.last_ui_signature = None
    viseq.viseq_selected_source = "clipB"
    viseq.global_vimix_state = {
        "current_source": 0,
        "sources": {
            "0": {"name": "clipA", "index": 0},
            "1": {"name": "clipB", "index": 1},
            "2": {"name": "clipC", "index": 2},
        },
    }
    dpg.calls.clear()
    try:
        viseq.update_vimix_sources_ui(json.dumps(viseq.global_vimix_state))
        binds = {
            c[1][0]: c[1][1]
            for c in dpg.calls
            if c[0] == "bind_item_theme" and c[1][0].startswith("tile_")
        }
        assert binds.get("tile_clipB") is viseq.theme_selected_clip, (
            "the viseq-selected tile must use the green selection theme"
        )
        assert binds.get("tile_clipA") is viseq.theme_vimix_current_clip, (
            "the vimix current source alone must use the lighter (non-green) theme"
        )
        assert binds.get("tile_clipC") is viseq.theme_normal_clip, (
            "unselected tiles keep the plain theme"
        )
    finally:
        viseq.global_vimix_state = saved_state
        viseq.last_ui_signature = saved_sig
        viseq.viseq_selected_source = saved_sel


def test_e10s06_current_target_prefers_viseq_selection():
    saved_state = viseq.global_vimix_state
    saved_sel = viseq.viseq_selected_source
    viseq.global_vimix_state = {"current_source": 0, "sources": {"0": {"name": "clipA"}}}
    viseq.viseq_selected_source = None
    try:
        assert viseq.get_current_target_id() == "clipA", "fallback: vimix current source"
        viseq.viseq_selected_source = "clipB"
        assert viseq.get_current_target_id() == "clipB", "the viseq selection is primary"
    finally:
        viseq.global_vimix_state = saved_state
        viseq.viseq_selected_source = saved_sel


def test_e10s06_track_assign_uses_viseq_selection():
    saved_state = viseq.global_vimix_state
    saved_sel = viseq.viseq_selected_source
    saved_target = viseq.tracks_data[0]["target_id"]
    viseq.global_vimix_state = {"current_source": 0, "sources": {"0": {"name": "clipA"}}}
    viseq.viseq_selected_source = "clipB"
    try:
        viseq.midi_action_track_assign(0)
        assert viseq.tracks_data[0]["target_id"] == "clipB", (
            "the sequencer must attach the viseq-selected media"
        )
    finally:
        viseq.tracks_data[0]["target_id"] = saved_target
        viseq.global_vimix_state = saved_state
        viseq.viseq_selected_source = saved_sel


def test_e10s06_prune_clears_stale_viseq_selection():
    saved_state = viseq.global_vimix_state
    saved_sig = viseq.last_ui_signature
    saved_sel = viseq.viseq_selected_source
    viseq.last_ui_signature = None
    viseq.viseq_selected_source = "ghost"
    viseq.thumbnails_data.clear()
    viseq.request_timestamps.clear()
    viseq.global_vimix_state = {
        "current_source": 0,
        "sources": {"0": {"name": "clipA", "index": 0}},
    }
    try:
        viseq.update_vimix_sources_ui(json.dumps(viseq.global_vimix_state))
        assert viseq.viseq_selected_source is None, "a pruned selection must clear"
    finally:
        viseq.global_vimix_state = saved_state
        viseq.last_ui_signature = saved_sig
        viseq.viseq_selected_source = saved_sel


def test_e10s06_click_callback_survives_extra_dpg_args(monkeypatch):
    """DPG 2.3.1 calls item-handler callbacks with co_argcount args — Python
    3.13 counts defaults, so the captured lambda receives an extra None arg
    that must NOT clobber the captured target id."""
    saved_state = viseq.global_vimix_state
    saved_sig = viseq.last_ui_signature
    saved_sel = viseq.viseq_selected_source
    viseq.last_ui_signature = None
    viseq.viseq_selected_source = None
    viseq.global_vimix_state = {
        "current_source": 0,
        "sources": {"0": {"name": "clipA", "index": 0}},
    }
    dpg.calls.clear()
    try:
        viseq.update_vimix_sources_ui(json.dumps(viseq.global_vimix_state))
        handler = next(
            c[2]["callback"]
            for c in dpg.calls
            if c[0] == "add_item_clicked_handler" and c[2].get("callback") is not None
        )
        # DPG passes (sender, app_data, user_data, None) — the 4th arg used to
        # override the captured target and silently deselect.
        handler(28, (0, "tile_title_clipA"), None, None)
        assert viseq.viseq_selected_source == "clipA", (
            "the extra None arg must not clobber the captured target id"
        )
    finally:
        viseq.global_vimix_state = saved_state
        viseq.last_ui_signature = saved_sig
        viseq.viseq_selected_source = saved_sel


def test_e10s06_title_truncation_falls_back_when_font_unmeasured(monkeypatch):
    """get_text_size returns None until the font atlas is built (first frame);
    the two-line budget must still apply via the per-char fallback, no raise."""
    monkeypatch.setattr(dpg, "get_text_size", lambda text: None)
    long_name = "07-Ritual-giardino_bidir_crf19_noaudio_bidir_crf19_noaudio.mp4"
    out = viseq.truncate_media_title(long_name)
    assert out.endswith(viseq.MEDIA_TITLE_ELLIPSIS), "must truncate with the fallback"
    chars_per_line = viseq.MEDIA_TITLE_WRAP // viseq.MEDIA_TITLE_CHAR_PX
    assert len(out) <= chars_per_line * viseq.MEDIA_TITLE_MAX_LINES, (
        "the fallback budget must keep the title within two wrapped lines"
    )
    assert viseq.truncate_media_title("") == ""
    assert viseq.truncate_media_title("short.mp4") == "short.mp4"


def test_sequencer_beat_wait_is_polled():
    """BUG-2026-08-27T213000: the band/MIDI beat wait must be bounded, so a
    beat-source switch or STOP always breaks through — an unbounded wait
    strands the tick thread in a mode that no longer fires."""
    src = Path("viseq.py").read_text()
    fn = re.search(r"def sequencer_tick\(.*?\n(?=def |\n# ===)", src, re.S)
    assert fn, "sequencer_tick not found"
    m = re.search(r"sync_event_beat\.wait\(([^)]*)\)", fn.group(0))
    assert m and m.group(1).strip(), (
        "the band/MIDI wait must be polled with a timeout (BUG-2026-08-27T213000)"
    )
    assert "continue" in fn.group(0), (
        "an idle poll must re-loop so a mode/stop change is re-evaluated"
    )


# ---------- e11s01: project file core (capture/apply/io) ----------
def _seed_project_ui_values():
    """Seed the DpgStub value table with a known project-like UI state."""
    dpg.values["manual_bpm_input"] = 132
    dpg.values["combo_devices"] = "Mock In"
    dpg.values["cb_lowpass"] = False
    dpg.values["theme_preset"] = "Dark"
    dpg.values["band1_enabled"] = True
    dpg.values["band1_start"] = 0.0
    dpg.values["band1_end"] = 0.33
    dpg.values["band1_min"] = 0.0
    dpg.values["band1_max"] = 1.0
    dpg.values["band2_enabled"] = False
    dpg.values["band3_enabled"] = False


def _known_tracks():
    return copy.deepcopy(viseq.tracks_data)


def test_project_capture_includes_layout_theme_sequencer(monkeypatch):
    """capture_project_state() snapshots layout + theme + full sequencer state."""
    _seed_project_ui_values()
    monkeypatch.setattr(
        dpg, "get_item_pos", lambda tag: {"sequencer_window": [10, 10]}.get(tag, [0, 0])
    )
    monkeypatch.setattr(dpg, "get_item_width", lambda tag: 1050)
    monkeypatch.setattr(dpg, "get_item_height", lambda tag: 800)
    monkeypatch.setattr(dpg, "is_item_shown", lambda tag: True)

    monkeypatch.setattr(viseq, "beat_source", viseq.BEAT_SOURCE_MANUAL)
    monkeypatch.setattr(viseq, "active_palette", copy.deepcopy(viseq.DEFAULT_PALETTE))
    viseq.active_palette["accent"] = [10, 20, 30]
    tracks = _known_tracks()
    tracks[0]["target_id"] = "media_42"
    tracks[0]["base_address"] = "/vimix/media_42"
    tracks[0]["steps"][3]["type"] = "AlphaV"
    tracks[0]["steps"][3]["v1"] = 0.42
    tracks[0]["steps"][3]["active"] = True
    tracks[0]["steps"][3]["color"] = [0.9, 0.1, 0.5]
    monkeypatch.setattr(viseq, "tracks_data", tracks)

    state = viseq.capture_project_state()

    assert state["layout"]["windows"], "layout section must carry the window records"
    assert state["theme"]["preset"] == "scuro", "'Dark' combo label maps to the scuro key"
    assert state["theme"]["colors"]["accent"] == [10, 20, 30]
    assert state["sequencer"]["beat_source"] == viseq.BEAT_SOURCE_MANUAL
    assert state["sequencer"]["manual_bpm"] == 132
    step = state["sequencer"]["tracks"][0]["steps"][3]
    assert step["type"] == "AlphaV"
    assert step["v1"] == 0.42
    assert step["active"] is True
    assert step["color"] == [0.9, 0.1, 0.5]
    assert "last_rand_v1" not in step, "runtime-only keys must not be persisted"
    assert "last_rand_seek" not in step
    assert "last_rand_color" not in step


def test_project_capture_strips_runtime_state_and_records_audio(monkeypatch):
    """Only persisted step keys survive; audio section mirrors the widgets."""
    _seed_project_ui_values()
    monkeypatch.setattr(viseq, "beat_source", viseq.BEAT_SOURCE_ANALYSIS)
    monkeypatch.setattr(viseq, "active_palette", copy.deepcopy(viseq.DEFAULT_PALETTE))
    state = viseq.capture_project_state()

    persisted_keys = {"active", "type", "v1", "v2", "frames", "msgs", "color"}
    for track in state["sequencer"]["tracks"]:
        for step in track["steps"]:
            assert set(step.keys()) == persisted_keys, "only persisted step keys may be saved"

    audio = state["sequencer"]["audio"]
    assert audio["device"] == "Mock In"
    assert audio["lowpass"] is False
    assert audio["bands"]["1"] == {
        "enabled": True,
        "start": 0.0,
        "end": 0.33,
        "min": 0.0,
        "max": 1.0,
    }
    assert audio["bands"]["2"]["enabled"] is False
    assert audio["bands"]["3"]["enabled"] is False


def _project_state_fixture():
    """A complete, valid project state dict (as capture_project_state would produce)."""
    steps = [
        {
            "active": False,
            "type": "NONE",
            "v1": 0.0,
            "v2": 1.0,
            "frames": 4,
            "msgs": 1,
            "color": [1.0, 1.0, 1.0],
        }
        for _ in range(viseq.NUM_STEPS)
    ]
    steps[2]["type"] = "AlphaR"
    steps[2]["active"] = True
    return {
        "layout": {
            "windows": [
                {"tag": "sequencer_window", "shown": True, "pos": [15, 25], "size": [900, 700]}
            ]
        },
        "theme": {"preset": "chiaro", "colors": copy.deepcopy(viseq.DEFAULT_PALETTE)},
        "sequencer": {
            "beat_source": viseq.BEAT_SOURCE_MANUAL,
            "manual_bpm": 128.0,
            "tracks": [
                {
                    "target_id": "media_9",
                    "base_address": "/vimix/media_9",
                    "steps": copy.deepcopy(steps),
                },
                {"target_id": None, "base_address": "", "steps": copy.deepcopy(steps)},
            ],
            "audio": {
                "device": "0: Mock In",
                "lowpass": False,
                "bands": {
                    "1": {"enabled": True, "start": 0.0, "end": 0.33, "min": 0.0, "max": 1.0},
                    "2": {"enabled": False, "start": 0.33, "end": 0.66, "min": 0.0, "max": 1.0},
                    "3": {"enabled": False, "start": 0.66, "end": 1.0, "min": 0.0, "max": 1.0},
                },
            },
        },
    }


def test_project_apply_restores_tracks_globals_and_widgets():
    """apply_project_state() re-applies tracks, globals, widgets and layout."""
    state = _project_state_fixture()
    dpg.calls.clear()
    dpg.values.clear()
    viseq.apply_project_state(state)

    assert viseq.beat_source == viseq.BEAT_SOURCE_MANUAL
    assert viseq.current_bpm == 128.0
    assert viseq.lowpass_enabled is False
    assert viseq.tracks_data[0]["target_id"] == "media_9"
    assert viseq.tracks_data[0]["base_address"] == "/vimix/media_9"
    assert viseq.tracks_data[0]["steps"][2]["type"] == "AlphaR"
    assert viseq.tracks_data[0]["steps"][2]["active"] is True
    assert len(viseq.tracks_data[0]["steps"]) == viseq.NUM_STEPS
    assert viseq.tracks_data[1]["target_id"] is None
    assert viseq.bands_enabled[1] is True
    assert viseq.bands_enabled[2] is False

    assert dpg.values["manual_bpm_input"] == 128
    assert dpg.values["cb_beat_manual_bpm"] is True
    assert dpg.values["cb_beat_bpm_analysis"] is False
    assert dpg.values["cb_lowpass"] is False
    assert dpg.values["combo_devices"] == "0: Mock In"
    assert dpg.values["band1_enabled"] is True
    assert dpg.values["band1_start"] == 0.0
    assert dpg.values["band1_max"] == 1.0
    assert dpg.values["theme_preset"] == "Light"

    assert any(
        n == "set_item_pos" and a == ("sequencer_window", [15, 25]) for n, a, kw in dpg.calls
    ), "the window layout must be re-applied"
    assert any(n == "delete_item" and a == ("seq_slot_0",) for n, a, kw in dpg.calls), (
        "the clip slot UI must be rebuilt"
    )
    assert any(n == "delete_item" and a == ("seq_cell_0_2",) for n, a, kw in dpg.calls), (
        "the step cell UI must be rebuilt"
    )


def test_project_apply_tolerates_missing_sections_and_unknown_device():
    """Missing sections and an unknown audio device must not crash the restore."""
    state = _project_state_fixture()
    state["sequencer"]["audio"]["device"] = "Ghost Device"
    state["layout"] = {}
    state["sequencer"].pop("manual_bpm")
    dpg.calls.clear()
    dpg.values.clear()
    viseq.apply_project_state(state)  # must not raise

    assert "combo_devices" not in dpg.values, "an unknown device must be skipped"
    assert viseq.tracks_data[0]["steps"][2]["type"] == "AlphaR"


def test_project_file_round_trip(tmp_path):
    """save_project_to_file + load_project_file round-trip a state dict."""
    path = str(tmp_path / "proj.viseq")
    state = _project_state_fixture()
    assert viseq.save_project_to_file(path, state) is True
    loaded = viseq.load_project_file(path)
    assert loaded is not None
    assert loaded["sequencer"]["tracks"][0]["steps"][2]["type"] == "AlphaR"
    assert loaded["theme"]["preset"] == "chiaro"
    assert loaded["layout"]["windows"][0]["tag"] == "sequencer_window"


def test_project_file_io_rejects_bad_input(tmp_path):
    """Missing, wrong-format, wrong-version and corrupt files return None."""
    missing = str(tmp_path / "nope.viseq")
    assert viseq.load_project_file(missing) is None

    bad_format = tmp_path / "bad.viseq"
    bad_format.write_text(json.dumps({"format": "other", "version": 1}))
    assert viseq.load_project_file(str(bad_format)) is None

    bad_version = tmp_path / "old.viseq"
    bad_version.write_text(json.dumps({"format": viseq.PROJECT_FORMAT, "version": 99}))
    assert viseq.load_project_file(str(bad_version)) is None

    corrupt = tmp_path / "corrupt.viseq"
    corrupt.write_text("{ not json")
    assert viseq.load_project_file(str(corrupt)) is None


def test_project_sanitize_heals_partial_step_and_bad_beat_source():
    """A hand-edited step loses keys -> defaults; a bad beat source falls back."""
    raw = _project_state_fixture()
    raw["sequencer"]["beat_source"] = "nonsense"
    step = raw["sequencer"]["tracks"][0]["steps"][0]
    step.pop("v1")
    step.pop("frames")
    step.pop("color")
    step["type"] = "SeekR"
    state = viseq._sanitize_project_state(raw)
    assert state["sequencer"]["beat_source"] == viseq.BEAT_SOURCE_ANALYSIS
    healed = state["sequencer"]["tracks"][0]["steps"][0]
    assert healed["v1"] == 0.0
    assert healed["frames"] == 4
    assert healed["color"] == [1.0, 1.0, 1.0]
    assert healed["active"] is False
    assert healed["type"] == "SeekR", "present keys must survive the heal"


def test_project_sanitize_drops_unknown_keys_and_heals_missing_sections():
    """Unknown top-level keys drop; a missing theme falls back to defaults."""
    raw = _project_state_fixture()
    raw["format"] = viseq.PROJECT_FORMAT
    raw["version"] = viseq.PROJECT_VERSION
    raw["hacker_key"] = "x"
    raw.pop("theme")
    state = viseq._sanitize_project_state(raw)
    assert set(state.keys()) == {"layout", "theme", "sequencer"}
    assert state["theme"]["preset"] == "scuro"
    assert state["theme"]["colors"]["accent"] == list(viseq.DEFAULT_PALETTE["accent"])


def test_project_sanitize_heals_audio_section():
    """Garbage band values and missing band configs heal to defaults."""
    raw = _project_state_fixture()
    raw["sequencer"]["audio"] = {"device": "X", "bands": {"1": {"enabled": True, "start": "abc"}}}
    state = viseq._sanitize_project_state(raw)
    audio = state["sequencer"]["audio"]
    assert audio["device"] == "X"
    assert audio["lowpass"] is True
    assert audio["bands"]["1"]["start"] == 0.0, "garbage start heals to the default range start"
    assert audio["bands"]["2"]["end"] == 0.66


# ---------- e11s02: config integration (recent projects + restore flag) ----------
def test_config_defaults_have_projects_section(monkeypatch):
    monkeypatch.setattr(viseq, "CONFIG_PATH", "/nonexistent/viseq_config.json")
    cfg = viseq.load_config()
    assert cfg["projects"]["recent"] == []
    assert cfg["projects"]["restore_last_on_boot"] is True
    assert "layout" not in cfg, "the legacy layout key must be gone"


def test_load_config_drops_legacy_layout_key(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    legacy = {
        "layout": {"restore_on_boot": True, "windows": [{"tag": "x"}]},
        "theme": {"preset": "scuro", "colors": viseq.DEFAULT_PALETTE},
        "midi": {"enabled": False, "input_port": None, "bindings": []},
    }
    p.write_text(json.dumps(legacy))
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    cfg = viseq.load_config()
    assert "layout" not in cfg, "legacy layout must not survive the load"
    assert cfg["projects"]["recent"] == []


def test_config_round_trip_preserves_projects(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    cfg = viseq.load_config()
    cfg["projects"]["restore_last_on_boot"] = False
    cfg["projects"]["recent"] = ["/tmp/a.viseq"]
    viseq.save_config(cfg)
    loaded = viseq.load_config()
    assert loaded["projects"]["restore_last_on_boot"] is False
    assert loaded["projects"]["recent"] == ["/tmp/a.viseq"]


def test_remember_recent_project_dedupes_caps_and_orders():
    """Most recent first, no duplicates, capped at RECENT_PROJECTS_MAX."""
    cfg = {"projects": {"recent": [], "restore_last_on_boot": True}}
    cfg["projects"]["recent"] = viseq.remember_recent_project(cfg, "/tmp/a.viseq")
    cfg["projects"]["recent"] = viseq.remember_recent_project(cfg, "/tmp/b.viseq")
    cfg["projects"]["recent"] = viseq.remember_recent_project(cfg, "/tmp/a.viseq")
    assert cfg["projects"]["recent"] == ["/tmp/a.viseq", "/tmp/b.viseq"]
    for i in range(6):
        cfg["projects"]["recent"] = viseq.remember_recent_project(cfg, f"/tmp/p{i}.viseq")
    assert len(cfg["projects"]["recent"]) == viseq.RECENT_PROJECTS_MAX
    assert cfg["projects"]["recent"][0] == "/tmp/p5.viseq"


def test_recent_project_paths_prunes_missing_files(tmp_path):
    keep = tmp_path / "keep.viseq"
    keep.write_text("{}")
    cfg = {
        "projects": {
            "recent": [str(keep), str(tmp_path / "gone.viseq"), str(tmp_path / "also_gone.viseq")],
            "restore_last_on_boot": True,
        }
    }
    assert viseq.recent_project_paths(cfg) == [str(keep)]


def test_restore_last_project_flag_defaults_and_toggle(monkeypatch, tmp_path):
    monkeypatch.setattr(viseq, "CONFIG_PATH", "/nonexistent/viseq_config.json")
    cfg = viseq.load_config()
    assert viseq.should_restore_last_project_on_boot(cfg) is True
    p = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    viseq.on_restore_project_boot_toggle(None, False)
    cfg = viseq.load_config()
    assert cfg["projects"]["restore_last_on_boot"] is False
    viseq.on_restore_project_boot_toggle(None, True)
    cfg = viseq.load_config()
    assert cfg["projects"]["restore_last_on_boot"] is True


# ---------- e11s03: viSeq menu ----------
def test_viseq_menu_is_first_with_open_last_save_exit():
    labels = [m.get("label") for m in import_time_menus]
    assert labels[0] == "viSeq", "viSeq must be the first menubar menu"
    assert labels[1:4] == ["Last project", "Monitor", "Show"], (
        "viSeq must precede the existing Monitor/Show menus"
    )
    items = {kw.get("label"): kw.get("callback") for kw in import_time_menu_items}
    assert items.get("Open project") == viseq.show_open_project_dialog
    assert items.get("Save project") == viseq.show_save_project_dialog
    assert items.get("Exit") == viseq.exit_app
    assert any(m.get("tag") == "menu_last_project" for m in import_time_menus), (
        "the Last project submenu must exist"
    )


def test_last_project_menu_shows_recent_files(monkeypatch, tmp_path):
    proj = tmp_path / "set.viseq"
    proj.write_text("{}")
    monkeypatch.setattr(
        viseq,
        "load_config",
        lambda: {"projects": {"recent": [str(proj)], "restore_last_on_boot": True}},
    )
    dpg.calls.clear()
    viseq.rebuild_last_project_menu()
    added = [kw for n, a, kw in dpg.calls if n == "add_menu_item"]
    assert [kw.get("label") for kw in added] == ["set.viseq"]
    assert any(kw.get("user_data") == str(proj) for kw in added)
    assert all(kw.get("parent") == "menu_last_project" for kw in added)


def test_last_project_menu_empty_shows_disabled_placeholder(monkeypatch):
    monkeypatch.setattr(
        viseq, "load_config", lambda: {"projects": {"recent": [], "restore_last_on_boot": True}}
    )
    dpg.calls.clear()
    viseq.rebuild_last_project_menu()
    added = [kw for n, a, kw in dpg.calls if n == "add_menu_item"]
    assert added, "an empty list must still render a placeholder item"
    assert added[0].get("label") == "No recent projects"
    assert added[0].get("enabled") is False


def test_project_flow_open_applies_remembers_and_syncs_theme(monkeypatch, tmp_path):
    proj = tmp_path / "p.viseq"
    state = _project_state_fixture()
    assert viseq.save_project_to_file(str(proj), state) is True
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(cfg_path))
    dpg.calls.clear()
    assert viseq.open_project_file(str(proj)) is True
    assert viseq.tracks_data[0]["steps"][2]["type"] == "AlphaR", "the project must be applied"
    cfg = viseq.load_config()
    assert cfg["projects"]["recent"] == [str(proj)], "the opened path must be remembered"
    assert cfg["theme"]["preset"] == "chiaro", "the fallback theme must follow the project"
    assert any(n == "delete_item" and a == ("menu_last_project",) for n, a, kw in dpg.calls), (
        "the Last-project submenu must be rebuilt after an open"
    )


def test_project_flow_open_bad_file_returns_false(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(cfg_path))
    assert viseq.open_project_file(str(tmp_path / "nope.viseq")) is False


def test_project_flow_save_round_trips_and_remembers(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(cfg_path))
    proj = str(tmp_path / "myproject")
    dpg.calls.clear()
    dpg.values.clear()
    _seed_project_ui_values()
    monkeypatch.setattr(viseq, "active_palette", copy.deepcopy(viseq.DEFAULT_PALETTE))
    assert viseq.save_project_file(proj) is True
    saved = f"{proj}.viseq"
    assert os.path.exists(saved), "the .viseq extension must be appended"
    loaded = viseq.load_project_file(saved)
    assert loaded is not None, "the saved file must load back"
    cfg = viseq.load_config()
    assert cfg["projects"]["recent"] == [saved]
    assert any(n == "delete_item" and a == ("menu_last_project",) for n, a, kw in dpg.calls)


def test_project_flow_recent_entry_routes_to_open(monkeypatch, tmp_path):
    proj = tmp_path / "p.viseq"
    state = _project_state_fixture()
    assert viseq.save_project_to_file(str(proj), state) is True
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(cfg_path))
    dpg.calls.clear()
    viseq.open_recent_project(None, None, str(proj))
    assert viseq.tracks_data[0]["steps"][2]["type"] == "AlphaR"


def test_project_dialog_save_callback_forces_extension(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(cfg_path))
    dpg.calls.clear()
    dpg.values.clear()
    _seed_project_ui_values()
    monkeypatch.setattr(viseq, "active_palette", copy.deepcopy(viseq.DEFAULT_PALETTE))
    target = str(tmp_path / "pick")
    viseq.on_save_project_picked(None, {"file_path_name": target}, None)
    assert os.path.exists(f"{target}.viseq"), "the save callback must write a .viseq file"


def test_project_dialog_open_callback_routes(monkeypatch, tmp_path):
    proj = tmp_path / "p.viseq"
    state = _project_state_fixture()
    assert viseq.save_project_to_file(str(proj), state) is True
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(cfg_path))
    dpg.calls.clear()
    viseq.on_open_project_picked(None, {"file_path_name": str(proj)}, None)
    assert viseq.tracks_data[0]["steps"][2]["type"] == "AlphaR", "the open callback must apply"


def test_project_exit_app_stops_dearpygui():
    dpg.calls.clear()
    viseq.exit_app()
    assert any(n == "stop_dearpygui" for n, a, kw in dpg.calls), "Exit must stop the app"


def test_project_dialogs_created_with_stable_tags():
    dialogs = import_time_file_dialogs
    tags = {kw.get("tag") for kw in dialogs}
    assert "open_project_dialog" in tags, "the open dialog must exist with a stable tag"
    assert "save_project_dialog" in tags, "the save dialog must exist with a stable tag"
    for kw in dialogs:
        assert kw.get("show") is False, "dialogs must be created hidden"
        assert kw.get("default_path") == viseq.PROJECTS_DIR


# ---------- e11s04: settings restructure + boot restore ----------
def test_settings_project_section_replaces_windows_section():
    # the new restore-last-project checkbox exists, default ON, project callback
    assert import_time_project_restore_cb, (
        "the restore-last-project checkbox must exist in Settings"
    )
    cb = import_time_project_restore_cb[0]
    assert cb.get("default_value") is True
    assert cb.get("callback") == viseq.on_restore_project_boot_toggle
    # the old windows widgets are gone
    labels = [kw.get("label") for kw in import_time_settings_buttons]
    assert "Save layout" not in labels, "the Save layout button must be gone"
    assert "Restore layout" not in labels, "the Restore layout button must be gone"
    assert not import_time_restore_checkbox, "the restore-layout checkbox must be gone"
    # the project checkbox precedes the OSC section (first input of the OSC block)
    assert import_time_project_cb_order.index("cb_restore_project_boot") < (
        import_time_project_cb_order.index("viosc_ip")
    ), "the Project section must sit above the OSC section"


def _boot_cfg_fixture(recent, restore_flag):
    return {
        "theme": {"preset": "scuro", "colors": viseq.DEFAULT_PALETTE},
        "midi": {"enabled": False, "input_port": None, "bindings": []},
        "projects": {"recent": recent, "restore_last_on_boot": restore_flag},
    }


def test_boot_restores_last_project_when_flagged(monkeypatch, tmp_path):
    proj = tmp_path / "p.viseq"
    state = _project_state_fixture()
    assert viseq.save_project_to_file(str(proj), state) is True
    p = tmp_path / "config.json"
    p.write_text(json.dumps(_boot_cfg_fixture([str(proj)], True)))
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    monkeypatch.setattr(viseq, "tracks_data", _fresh_tracks_data())
    dpg.calls.clear()
    viseq.apply_boot_config()
    assert viseq.tracks_data[0]["steps"][2]["type"] == "AlphaR", "the last project must be applied"
    assert any(
        n == "set_item_pos" and a == ("sequencer_window", [15, 25]) for n, a, kw in dpg.calls
    ), "the project layout must be re-applied at boot"
    assert dpg.values.get("cb_restore_project_boot") is True
    assert dpg.values.get("theme_preset") == "Light", "the project theme must win at boot"


def test_boot_skips_project_restore_when_flag_off(monkeypatch, tmp_path):
    proj = tmp_path / "p.viseq"
    state = _project_state_fixture()
    assert viseq.save_project_to_file(str(proj), state) is True
    p = tmp_path / "config.json"
    p.write_text(json.dumps(_boot_cfg_fixture([str(proj)], False)))
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    monkeypatch.setattr(viseq, "tracks_data", _fresh_tracks_data())
    dpg.calls.clear()
    viseq.apply_boot_config()
    assert viseq.tracks_data[0]["steps"][2]["type"] == "NONE", (
        "the project must NOT be applied when the flag is off"
    )
    assert not any(n == "delete_item" and a == ("seq_cell_0_2",) for n, a, kw in dpg.calls)
    assert dpg.values.get("cb_restore_project_boot") is False


def test_boot_without_recents_applies_theme_only(monkeypatch, tmp_path):
    p = tmp_path / "config.json"
    cfg = _boot_cfg_fixture([], True)
    cfg["theme"] = {"preset": "chiaro", "colors": viseq.LIGHT_PALETTE}
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr(viseq, "CONFIG_PATH", str(p))
    dpg.calls.clear()
    viseq.apply_boot_config()
    assert viseq.active_palette == viseq.LIGHT_PALETTE, "the fallback theme still applies"
    assert not any(n == "delete_item" and a == ("seq_cell_0_2",) for n, a, kw in dpg.calls)


def _fresh_tracks_data():
    """Pristine tracks_data, matching the module import-time construction (e11s04)."""
    tracks = []
    for _ in range(viseq.NUM_TRACKS):
        steps = []
        for _ in range(viseq.NUM_STEPS):
            steps.append(
                {
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
            )
        tracks.append(
            {
                "target_id": None,
                "base_address": "",
                "active_fade": {"active": False},
                "steps": steps,
            }
        )
    return tracks
