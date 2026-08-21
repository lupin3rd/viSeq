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
dispatcher.Dispatcher = object
osc_server = types.ModuleType("pythonosc.osc_server")
osc_server.ThreadingOSCUDPServer = object
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


# ---------- MED-3: ColorR cell passes DPG-scale (0..255) 3-component value ----------
def test_med3_colorr_dpg_scale_value():
    viseq.tracks_data[0]["steps"][0]["type"] = "ColorR"
    viseq.tracks_data[0]["steps"][0]["last_rand_color"] = [0.5, 0.25, 0.75]
    dpg.calls.clear()
    viseq.update_step_ui(0, 0)
    color_edits = [kw for n, a, kw in dpg.calls if n == "add_color_edit"]
    assert color_edits, "no add_color_edit call issued for ColorR step"
    assert color_edits[-1].get("default_value") == [127.5, 63.75, 191.25], (
        "ColorR default_value must be on DPG's 0..255 API scale (ToColor divides by 255)"
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
    color_edits = [kw for n, a, kw in dpg.calls if n == "add_color_edit"]
    assert color_edits and color_edits[-1].get("default_value") == [127.5, 63.75, 191.25], (
        "ColorV square must open on DPG's 0..255 API scale (ToColor divides by 255)"
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
    assert dpg.values.get("rand_color_0_0") == [107.1, 107.1, 107.1], (
        "the step's little square must show the sent color on DPG's 0..255 API scale"
    )


def test_dpg_color_scale_boundaries():
    assert viseq.dpg_color_value([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]
    assert viseq.dpg_color_value([1.0, 1.0, 1.0]) == [255.0, 255.0, 255.0]


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
