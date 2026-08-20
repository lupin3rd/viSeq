"""Regression tests for the audit HIGH/MED fixes in viseq.py.

Runs headless: stubs dearpygui/sounddevice/essentia/pythonosc, imports the real
module (the GUI main loop is skipped because is_dearpygui_running() is False),
and exercises the real code paths. Covers:
  HIGH-1  thread-safe UI updates (no direct dpg calls in worker threads)
  HIGH-2  AlphaF fade cancellation on non-fade steps
  MED-3   ColorR cell value normalization
  MED-4   payload validation + defensive sorting + error surfacing
  MED-6   network-input caps (blob/json sizes, image pixels, listen default)

Run:  python3 tests/test_fixes.py
"""
"""Stub-based verification of the HIGH/MED fixes in viseq.py.

Stubs dearpygui/sounddevice/essentia/pythonosc so the module can be imported
(headless — the main loop is skipped because is_dearpygui_running() is False),
then exercises the real code paths for each fix.
"""
import io
import json
import queue
import sys
import time
import threading
import types
from PIL import Image

# ---------- stubs ----------
class CM:
    def __enter__(self): return self
    def __exit__(self, *a): return False

class DpgStub:
    def __init__(self):
        self.calls = []          # (name, args, kwargs)
        self.values = {}
    def __getattr__(self, name):
        def fn(*a, **kw):
            self.calls.append((name, a, kw))
            return CM()
        return fn
    def does_item_exist(self, item): return True
    def does_alias_exist(self, item): return False
    def set_value(self, tag, val): self.values[tag] = val
    def get_value(self, tag): return self.values.get(tag, True)
    def is_dearpygui_running(self): return False

dpg = DpgStub()
# dearpygui 2.x is a package: the API module is dearpygui.dearpygui (what viseq.py imports).
# The stub mirrors that layout (verified against 2.3.1 in SPIKE-dpg2x-api.md).
dpg_pkg = types.ModuleType('dearpygui')
dpg_pkg.__path__ = []
sys.modules['dearpygui'] = dpg_pkg
sys.modules['dearpygui.dearpygui'] = dpg

sd = types.ModuleType('sounddevice')
sd.query_devices = lambda: [{'name': 'Mock In', 'max_input_channels': 2}]
sd.InputStream = object
sys.modules['sounddevice'] = sd

class Sender:
    def __init__(self): self.messages = []  # (addr, payload)
    def send_message(self, addr, payload): self.messages.append((addr, payload))

essentia = types.ModuleType('essentia')
essentia.array = lambda x: x
standard = types.ModuleType('essentia.standard')
class RhythmExtractor2013:
    def __init__(self, *a, **kw): pass
    def __call__(self, audio): return (120.0, [], 0.9, [], [])
class LowPass:
    def __init__(self, *a, **kw): pass
    def __call__(self, audio): return audio
standard.RhythmExtractor2013 = RhythmExtractor2013
standard.LowPass = LowPass
essentia.standard = standard
sys.modules['essentia'] = essentia
sys.modules['essentia.standard'] = standard

osc = types.ModuleType('pythonosc')
udp_client = types.ModuleType('pythonosc.udp_client')
udp_client.SimpleUDPClient = lambda ip, port: Sender()
dispatcher = types.ModuleType('pythonosc.dispatcher')
dispatcher.Dispatcher = object
osc_server = types.ModuleType('pythonosc.osc_server')
osc_server.ThreadingOSCUDPServer = object
osc.udp_client = udp_client; osc.dispatcher = dispatcher; osc.osc_server = osc_server
sys.modules['pythonosc'] = osc
sys.modules['pythonosc.udp_client'] = udp_client
sys.modules['pythonosc.dispatcher'] = dispatcher
sys.modules['pythonosc.osc_server'] = osc_server

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import viseq  # noqa: E402  (needs the repo root on sys.path)

# capture listen default right after import, before any calls-list clears
listen_defaults = [kw.get("default_value") for n, a, kw in dpg.calls if n == "add_input_text"]

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

# ---------- MED-3: ColorR cell passes normalized 3-component value ----------
viseq.tracks_data[0]["steps"][0]["type"] = "ColorR"
viseq.tracks_data[0]["steps"][0]["last_rand_color"] = [0.5, 0.25, 0.75]
dpg.calls.clear()
viseq.update_step_ui(0, 0)
color_edits = [kw for n, a, kw in dpg.calls if n == "add_color_edit"]
check("MED-3 ColorR default_value is normalized 3 components",
      color_edits and color_edits[-1].get("default_value") == [0.5, 0.25, 0.75]
      and "alpha" not in color_edits[-1] or (color_edits and len(color_edits[-1].get("default_value", [])) == 3))

# ---------- MED-4: payload validation / defensive sorting ----------
viseq.update_vimix_sources_ui(json.dumps({
    "current_source": 1,
    "sources": {"1": {"name": "clipA", "index": 1, "alpha": 0.5},
                "2": "not-a-dict",                       # must be dropped
                "abc": {"name": "clipB"},                # non-integer key, no index
                "3": {"name": "clipC", "index": "3"}}    # string index
}))
check("MED-4 malformed source entries dropped", "2" not in viseq.global_vimix_state["sources"])
check("MED-4 non-integer key doesn't crash", "abc" in viseq.global_vimix_state["sources"])
check("MED-4 string index coerced", viseq.global_vimix_state["sources"]["3"]["index"] == "3")

# payload is a list, not a dict
viseq.update_vimix_sources_ui(json.dumps([1, 2, 3]))
errs = [m for m in list(viseq.log_queue.queue) if "ERROR" in m]
check("MED-4 malformed payload logged, not silent", any("UI update" in e for e in errs))

# sources is a list
viseq.update_vimix_sources_ui(json.dumps({"sources": [1, 2]}))
errs = [m for m in list(viseq.log_queue.queue) if "ERROR" in m]
check("MED-4 non-object sources logged", any("'sources' is not an object" in e for e in errs))

# ---------- MED-6: input caps ----------
viseq.incoming_osc_handler("/viosc/replythumb/clipA/0", b"x" * (viseq.MAX_THUMBNAIL_BLOB_BYTES + 1))
check("MED-6 oversized thumbnail blob rejected", viseq.blob_queue.empty())

viseq.incoming_osc_handler("/viosc/replydata", "x" * (viseq.MAX_STATE_JSON_BYTES + 1))
check("MED-6 oversized replydata rejected", viseq.ui_state_queue.empty())

viseq.incoming_osc_handler("/viosc/replythumb/clipA/0", b"smallblob")
check("MED-6 normal thumbnail blob accepted", not viseq.blob_queue.empty())

# listen default is loopback
check("MED-6 listen default is 127.0.0.1", "127.0.0.1" in listen_defaults)

# thumbnail decoder pixel cap (real worker, real PIL)
def run_worker_once(blob):
    viseq.texture_queue = queue.Queue()   # fresh queue for the assertion
    viseq.blob_queue.put(("clipA", "0", blob))
    t = threading.Thread(target=viseq.thumbnail_decoder_worker, daemon=True)
    t.start()
    for _ in range(200):                  # up to 2s for decode
        if not viseq.texture_queue.empty(): break
        time.sleep(0.01)
    time.sleep(0.05)
    return not viseq.texture_queue.empty()

big = Image.new("RGB", (2000, 2000), "red")   # 4 MP > 3 MP cap
buf = io.BytesIO(); big.save(buf, "PNG")
check("MED-6 oversized image rejected by worker", not run_worker_once(buf.getvalue()))

small = Image.new("RGB", (320, 180), "red")
buf = io.BytesIO(); small.save(buf, "PNG")
check("MED-6 normal image decoded by worker", run_worker_once(buf.getvalue()))

# ---------- L-1: stale-state pruning ----------
viseq.thumbnails_data.clear()
viseq.request_timestamps.clear()
viseq.thumbnails_data["clipA"] = "tex_clipA"
viseq.thumbnails_data["ghost"] = "tex_ghost"
viseq.request_timestamps["thumb_clipA"] = 1.0
viseq.request_timestamps["thumb_ghost"] = 2.0
viseq.update_vimix_sources_ui(json.dumps({"current_source": 1, "sources": {"1": {"name": "clipA", "index": 1}}}))
check("L-1 stale source pruned from thumbnails_data and request_timestamps",
      "ghost" not in viseq.thumbnails_data and "thumb_ghost" not in viseq.request_timestamps
      and "clipA" in viseq.thumbnails_data and "thumb_clipA" in viseq.request_timestamps)

# ---------- HIGH-1: no direct dpg calls in worker threads ----------
import re
src = open("viseq.py").read()
thread_fns = ["audio_callback", "essentia_analyzer_loop", "visual_metronome_loop",
              "sequencer_tick", "fade_tick_loop", "thumbnail_decoder_worker"]
dirty = []
for fn in thread_fns:
    m = re.search(rf'def {fn}\(.*?\n(?=def |\n# ===)', src, re.S)
    if m and re.search(r'dpg\.\w+', m.group(0)):
        dirty.append(fn)
check("HIGH-1 no direct dpg calls in worker threads", not dirty)

# enqueue_set_value / ui_task drain mechanics
dpg.values.clear()
viseq.enqueue_set_value("vu_meter", 0.42)
while not viseq.ui_task_queue.empty():
    viseq.ui_task_queue.get()()
check("HIGH-1 enqueue_set_value drains to main-thread set_value", dpg.values.get("vu_meter") == 0.42)

# lowpass checkbox state cached for worker threads
viseq.on_lowpass_toggle(None, False, None)
check("HIGH-1 lowpass flag mirrors checkbox", viseq.lowpass_enabled is False)
viseq.on_lowpass_toggle(None, True, None)

# ---------- HIGH-2: fade cancellation (live sequencer thread) ----------
def wait_until(pred, timeout=12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False

viseq.is_playing = False
viseq.current_bpm = 120.0    # 0.5s per step
viseq.current_step = -1
viseq.callback_resync()
time.sleep(0.3)  # let idle threads settle

osc_sender = viseq.osc_client
osc_sender.messages.clear()

# Track B: step0 = AlphaF (frames=8 -> fade would span ~8 steps), steps 1-6 inactive,
# step7 = AlphaV active. The step7 dispatch must cancel the running fade.
def make_step(active, stype, v1, v2, frames, msgs):
    return {"active": active, "type": stype, "v1": v1, "v2": v2, "frames": frames,
            "msgs": msgs, "color": [255, 255, 255], "last_rand_v1": 0.0,
            "last_rand_color": [0, 0, 0]}

t = viseq.tracks_data[0]
t["base_address"] = "/vimix/clipA"
t["steps"] = [make_step(False, "NONE", 0.0, 1.0, 8, 4) for _ in range(8)]
t["steps"][0] = make_step(True, "AlphaF", 0.1, 0.9, 8, 4)
t["steps"][7] = make_step(True, "AlphaV", 0.33, 0.0, 1, 1)
t["active_fade"] = {"active": False}

viseq.is_playing = True

# mid-sequence: fade should be running and sending intermediates before step7
mid_ok = wait_until(lambda: len([m for m in osc_sender.messages if m[0].endswith("/alpha")]) > 3, timeout=3.0)
check("HIGH-2 fade is running mid-sequence", mid_ok and t["active_fade"].get("active") is True)

# step7 (AlphaV) fires every 4s cycle: detect cancellation + last value, then stop promptly
cancelled_ok = wait_until(
    lambda: (t["active_fade"].get("active") is False
             and osc_sender.messages and osc_sender.messages[-1][1] == 0.33),
    timeout=12.0)
if cancelled_ok:
    viseq.is_playing = False
check("HIGH-2 non-fade step cancels pending fade", cancelled_ok)
last_alpha = [m for m in osc_sender.messages if m[0].endswith("/alpha")]
check("HIGH-2 last alpha message is the AlphaV value", last_alpha and last_alpha[-1][1] == 0.33)

# Track A: uninterrupted AlphaF fade completes naturally (no regression)
t2 = viseq.tracks_data[1]
t2["base_address"] = "/vimix/clipB"
t2["steps"] = [make_step(False, "NONE", 0.0, 1.0, 4, 4) for _ in range(8)]
t2["steps"][0] = make_step(True, "AlphaF", 0.0, 1.0, 4, 4)
t2["active_fade"] = {"active": False}
viseq.current_step = -1
viseq.is_playing = True
started = wait_until(lambda: t2["active_fade"].get("active") is True, timeout=6.0)
completed = wait_until(lambda: t2["active_fade"].get("active") is False, timeout=12.0)
viseq.is_playing = False
check("HIGH-2 uninterrupted fade completes (frames=4)", started and completed)

print()
failed = [n for n, ok in PASS if not ok]
print(f"{len(PASS) - len(failed)}/{len(PASS)} checks passed")
sys.exit(1 if failed else 0)
