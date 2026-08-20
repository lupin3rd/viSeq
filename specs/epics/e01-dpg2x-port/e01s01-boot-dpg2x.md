# e01s01 — Boot on DearPyGui 2.3.1 (tracer bullet)

**type:** refactor
**risk:** P0
**context:** domain

**Context:** The app cannot run on the user's Python 3.13 machine (dearpygui 1.x and
compatible essentia have no 3.13 wheels). This tracer-bullet story establishes the target
runtime end-to-end: pinned manifest, clean venv install, app boots to a stable window on
dearpygui 2.3.1 (proven API-compatible by SPIKE-dpg2x-api.md), and the regression harness
stays green. Also folds in audit items L-4 (clean exit) and L-5 (main-loop sleep).

## Requirements

#### MODIFIED: viseq runs on the target runtime
**Before:** viseq runs on Python <= 3.11 with dearpygui 1.x (uninstallable on 3.13).
**After:** viseq runs on Python 3.13 with dearpygui 2.3.1 (via the preserved
`dearpygui.dearpygui` namespace) and essentia 2.1b6.dev1389 (the cp313 wheel).

#### ADDED: Clean exit on shutdown (L-4)
The app stops the audio stream, shuts down the local OSC server, and destroys the dpg context
on exit via try/finally around the main loop (no orphaned stream/threads).

#### ADDED: Bounded render rate (L-5)
The main loop sleeps at least 0.016 s per frame (~60 fps cap) instead of 0.005 s (~200 fps).

## Steps

1. Create `.venv` with the system Python 3.13 and bump `requirements.txt` to the spike-verified pins (dearpygui==2.3.1, essentia==2.1b6.dev1389, numpy>=2,<3, python-osc==1.10.2, sounddevice==0.5.6, Pillow>=11,<13) → verify: `.venv/bin/pip install -r requirements.txt && .venv/bin/pip show dearpygui | grep -q 2.3.1 && .venv/bin/pip show essentia | grep -q 2.1b6.dev1389`
2. Boot viseq unmodified on 2.x (import, create_context, viewport, main loop) → verify: `timeout 8 .venv/bin/python viseq.py; test $? -eq 124` (exit 124 = still running when the timeout fired = booted with no traceback; any other exit = crash)
3. Wrap the main loop in try/finally: on exit stop `audio_stream`, shutdown `local_osc_server`, join threads, then `destroy_context` → verify: `python3 -m py_compile viseq.py && timeout 8 .venv/bin/python viseq.py; test $? -eq 124`
4. Change the main-loop `time.sleep(0.005)` to `time.sleep(0.016)` → verify: `grep -n 'time.sleep(0.016)' viseq.py | head -1`
5. Update the stub harness for the 2.x era (dearpygui package layout note) and keep it green → verify: `python3 tests/test_fixes.py`

## Verification Script (Step-by-Step)

1. `.venv/bin/pip install -r requirements.txt` — completes without errors.
2. `.venv/bin/python viseq.py` — a window appears (sequencer layout), stays up, no traceback.
3. Close the app (Ctrl+C) — process exits cleanly, no "stream not closed" errors, no orphaned processes (`pgrep -f viseq.py` empty).
4. `python3 tests/test_fixes.py` — 19/19 checks pass.

## Out of scope

- Feature behavior (covered by e01s02–e01s04)
- venv/pip automation scripts, CI wiring (see SCOPE_LATEST out_of_scope)

## Risks

- dearpygui 2.3.1 may render differently than 1.x on the user's display — detected early by this boot story; fidelity is judged by the user at acceptance (e01s05).
- The spike landmine (dpg API calls before `create_context()`) must not be reintroduced — no new module-level dpg calls.
