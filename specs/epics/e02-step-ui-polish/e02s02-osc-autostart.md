# e02s02 — Auto-start OSC client + listening server on launch

**type:** feature
**risk:** P1
**context:** osc/boot

**Context:** At boot the app requires two manual clicks before any OSC flows: "Connect
Client" (`connect_to_viosc`, reads `viosc_ip`/`viosc_port` widgets) and "Start Server"
(`toggle_local_server`, reads `listen_ip`/`listen_port`, starts `ThreadingOSCUDPServer` on
the default 6667 and flips the button label). The user wants both up automatically at launch
with the default addresses (client → viOSC 127.0.0.1:6666, server listen 127.0.0.1:6667),
keeping the manual buttons as override/fallback. The start/connect logic must be extracted
into testable core functions so autostart can be verified headless; graceful degradation
applies (port busy → status shows the error, button still usable). The frozen OSC contract
and HIGH-1 (no dpg calls from worker threads) are untouched — autostart runs on the main
thread after the UI is built.

## Requirements

#### ENHANCED: OSC client + server start automatically at boot
**Before:** user clicks Connect Client and Start Server on every launch.
**After:** `autostart_osc()` connects the client (default 127.0.0.1:6666) and starts the
listening server (default 127.0.0.1:6667) automatically once the UI exists; status labels
reflect the state; manual buttons still toggle/stop.

## Steps

1. Extract `start_osc_server(ip, port) -> bool` from `toggle_local_server` (idempotent:
   already running → True; failure → False + status ERROR).
   → verify: `.venv/bin/python -m pytest tests/ -q -k server`
2. Extract `connect_osc_client(ip, port) -> bool` from `connect_to_viosc`.
   → verify: `.venv/bin/python -m pytest tests/ -q -k client`
3. Add `autostart_osc()` calling both with the default constants; call it in the boot
   sequence right after the viewport is shown (main thread).
   → verify: `.venv/bin/python -m pytest tests/ -q -k autostart`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: `.venv/bin/python viseq.py` — without any click the client status reads
   "Ready on 127.0.0.1:6666" and the server status "Listening on 127.0.0.1:6667"; the
   buttons still toggle/stop; with viOSC up, OSC traffic flows immediately.
