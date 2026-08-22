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

    def __getattr__(self, name):
        def fn(*a, **kw):
            self.calls.append((name, a, kw))
            return CM()

        return fn

    def does_item_exist(self, item):
        return True

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
    assert alpha_cfg and alpha_cfg[-1].get("pmin") == [0, 23.0], "alpha bar at 50%"
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
