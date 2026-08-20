# Audit — viseq.py

- **Date:** 2026-02-07 (session)
- **Artifact:** `viseq/viseq.py` (1080 lines, single file)
- **Mode:** full checklist, adapted — the bigpowers scaffolding this skill assumes
  (`CONVENTIONS.md`, `scripts/bp-churn-rank.sh`, git repo, `specs/`) does **not** exist in
  `viseq/`. Verify gate `test -f CONVENTIONS.md && test -d skills/enforce-first && ...` fails by
  construction; items that reference that infrastructure are marked N/A with a reason.
- **Verdict: FAIL** — 2 high-severity bugs, 4 medium, 8 low + style failures.

---

## Checklist

### Supply Chain & Security — FAIL

- [x] Secrets scan: no `sk-`, `ghp_`, `AKIA`, key/token/password patterns in the file.
- [x] No `[SLOP]`/`[SUS]` packages — no dependency diff exists to tag (nothing tagged in a plan).
- [x] No direct GitHub API / `gh` usage.
- [ ] **OWASP spot-check — FAIL.** Three findings:
  1. **Injection-ish / unvalidated network input:** the OSC server binds `0.0.0.0` by default
     (`listen_ip` default in Window 3). Any LAN host can push `/viosc/replydata` (arbitrary JSON,
     no schema/shape validation — feeds `update_vimix_sources_ui`) and `/viosc/replythumb/*`
     (arbitrary byte blobs → PIL decode). Untrusted-data path with no validation layer.
  2. **Decompression-bomb risk:** `thumbnail_decoder_worker` calls
     `Image.open(io.BytesIO(blob_bytes)).convert('RGBA')` with no `Image.MAX_IMAGE_PIXELS` /
     dimension cap. A hostile or corrupted blob can force a multi-GB allocation → OOM. Blobs
     originate from the network-facing OSC port.
  3. **Sensitive data exposure / misconfiguration:** unauthenticated UDP listener on all
     interfaces + no length cap on incoming datagrams. Recommend default `127.0.0.1` and a
     documented option to bind externally only when the LAN is trusted.
- [ ] Security diff scan: no baseline to scan; the findings above stand as the unaddressed
      HIGH/MEDIUM items. No `specs/security/EXCEPTIONS.md` exists.

### Provenance & Metadata — N/A

No `specs/` planning artefacts, epics, or ADRs exist for this file. Nothing to tag.

### Law of Demeter — PASS

- [x] No method chains through unrelated objects. The `track["steps"][col]["type"]` chains are
      plain data-structure access on local state, not collaborator chaining.

### CONVENTIONS.md Compliance — N/A (no CONVENTIONS.md in this tree)

- [x] No `gh issue create`, no GitHub REST calls anywhere.
- [x] Output report written under `specs/verifications/` per the skill convention.

### Scope — N/A

No git history/diff exists (`viseq/` is not a repo) — nothing to scope against, no churn ranking
possible (`scripts/bp-churn-rank.sh` absent). Reviewed the entire artifact.

### Boy Scout Rule — FAIL

- [ ] `__pycache__/viseq.cpython-313.pyc` left in the working tree.
- [ ] Comments mixed Italian/English ("La struttura dati del sequencer", "Rigenero") — pick one
      language (English) for maintainability.
- [x] No dead functions found (`turn_off_led`, themes, all reachable).
- [x] No commented-out code blocks.

### Types and Safety — FAIL

- [ ] **No type hints anywhere.** All public functions are untyped (`def update_vimix_sources_ui(json_string)` etc.) — Python-side equivalent of the "no `any`" rule. This is a 1080-line module; annotate at least the data-model accessors and callbacks.
- [x] No `@ts-ignore`/`eslint-disable` (n/a in Python) — no suppression comments found.

### Test Coverage — FAIL

- [ ] **Zero tests** in the tree (no pytest/unittest files). The sequencer tick, fade state
      machine, and JSON→UI mapping are pure-ish logic that could be extracted and tested without
      a GUI. At minimum: fade progress math, step-type dispatch, `update_vimix_sources_ui` payload
      mapping, and source-resolution helpers need unit tests.
- [ ] No F.I.R.S.T review possible — nothing to review.

### SOLID and Heuristics — FAIL

- [ ] **Single Responsibility:** `update_vimix_sources_ui` (~90 lines) does three jobs — build raw
      property table, build thumbnail grid, refresh live values. Split.
- [x] Open/Closed, Dependency Inversion: n/a at this granularity (no interfaces to extend).
- [ ] **G5 Duplication:** (a) source-name resolution logic duplicated in `get_current_target_id`,
      `assign_clip_to_track`, and the grid-build loop of `update_vimix_sources_ui`; (b) the
      float/None→string formatting block is copy-pasted twice in `update_vimix_sources_ui`.
- [ ] **G25 Magic numbers:** `3.0` (thumbnail request interval), `25` (log cap), `0.005` (main
      loop sleep), `145`/`135`/`110`/`90` (layout), `0.1` (LED reset). Extract named constants.
- [ ] **G31 Hidden temporal coupling:** `phase_nudge` is written by the UI thread and zeroed by
      whichever of `sequencer_tick`/`visual_metronome_loop` reads it first. They are mutually
      exclusive today (`is_playing` gate), but the coupling is implicit — a lock or single owner
      would make it robust.
- [ ] **C1/C2/C3:** comments mostly explain WHY (good), but several are WHAT-comments
      (`# NEW: number of messages...` adds value; `# 100 FPS check loop` is fine) — the Italian
      header comment (C1-adjacent) should move out of code.
- [x] G29: no negative conditionals of note.

### Refactoring Smells (Fowler) — present

- **Mysterious Name:** step params `v1`/`v2`/`frames`/`msgs` — `v1`/`v2` are fade start/end; the
  UI labels them ambiguously (`%ds`/`%dm`). Name them `fade_start`, `fade_end`, `fade_steps`,
  `msgs_per_step`.
- **Duplicated Code:** see G5 above.
- **Primitive Obsession:** steps/tracks/fades are bare dicts of primitives with string keys;
  a `Step`/`Track`/`Fade` dataclass would make the state machine testable and self-documenting.
- **Long Function / Long Parameter List:** `user_data=(row, col, param)` tuples are a mild
  primitive-obsession variant; `update_step_ui`/`update_vimix_sources_ui`/`sequencer_tick` are
  long.
- **Message Chains:** `track["steps"][col]["type"]` — data access, acceptable, but class
  attributes would read better.

### Code Style (CONVENTIONS.md-style) — FAIL

- [ ] **Functions 4–20 lines:** violated broadly — `update_vimix_sources_ui` ~90, `sequencer_tick`
      ~70, main loop ~70, `update_step_ui` ~50, `new_monitor_player` ~35.
- [ ] **Files under 300 lines:** 1080. (This one file is the whole app; still split into modules:
      `osc.py`, `sequencer.py`, `audio.py`, `ui.py`.)
- [ ] **Early returns / indentation:** mostly OK (2 levels max in most paths).
- [x] Names specific and unique (grep hits < 5 for `tracks_data` etc.).
- [ ] **`except Exception: pass` in `essentia_analyzer_loop`** silently swallows all BPM-path
      failures — at minimum log once to `log_queue`.
- [ ] No `if __name__ == "__main__":` guard — importing the module launches the whole GUI.
- [ ] Mixed-language comments (see Boy Scout).

### Red Flags — rationalizations caught

- Skipped the **churn ranking** — no git repo in `viseq/`; entire file reviewed instead.
- Skipped **slopcheck** — no manifest/diff; flagged missing `requirements.txt` as a supply-chain gap instead.
- Skipped **CONVENTIONS.md items** — file does not exist in this tree; verified the *spirit*
  (no `gh` abuse, no root-level docs) manually.
- Did **not** run the `--gate` exit-code contract — this is an ad-hoc single-file audit, not a CI gate.

---

## Bugs (severity-ordered)

### HIGH-1 — Thread-unsafe DearPyGui calls from non-main threads
`dpg.set_value` / `dpg.configure_item` / `dpg.bind_item_theme` / `dpg.get_value` are called from
`audio_callback` (sounddevice audio thread), `essentia_analyzer_loop`, `sequencer_tick`, and
`visual_metronome_loop`. DearPyGui requires all UI work on the main thread or inside
`with dpg.mutex():`. The code already uses `dpg.mutex()` in `regen_thumb_callback` — the
awareness exists but is not applied. This is the classic DPG cross-thread crash source.
**Fix:** route every off-thread UI mutation through a queue consumed in the main loop (the
`ui_state_queue` pattern already exists), or wrap each off-thread call site in
`with dpg.mutex():`.

### HIGH-2 — AlphaF fade (frames>1) overrides subsequent steps
`sequencer_tick` never cancels `track["active_fade"]` when a non-AlphaF step fires (verified:
0 references to `active_fade` in the AlphaV/AlphaR/ColorV/ColorR branches). With `frames > 1`,
the fade spans `frames × base_sleep` seconds (e.g., frames=4 at 120 BPM = 3.8 step-durations),
so the `fade_tick_loop` keeps sending OSC alpha values over the *next* steps, stomping their
AlphaV/Color*/AlphaR values.
**Fix:** at the top of `if step_data["active"]:`, set
`track["active_fade"]["active"] = False` unless the step type is AlphaF (which re-initialises it).

### MED-3 — ColorR cell shows garbage swatch
`update_step_ui` passes `[r, g, b, 255]` floats in 0–255 range to a `no_alpha` color_edit
(expects 3 components in 0–1). The stored `last_rand_color` is already 0–255; the widget needs
`[c/255 for c in last_rand_color]` (3 components, no trailing alpha).

### MED-4 — Silent failure of the main state path
`get_sort_index` calls `int(k)` on source keys; a non-integer key raises, and the bare
`except Exception` in `update_vimix_sources_ui` swallows the whole update → UI freezes on stale
data, console print only. Also the `except` should surface into the log window, not stdout.

### MED-5 — No requirements.txt, deps unpinned
`dearpygui`, `sounddevice`, `essentia`, `pythonosc` are not installed in this environment and no
manifest exists to install them. Add `requirements.txt` (or `pyproject.toml`) with pins.

### MED-6 — Network-input hardening (see OWASP above)
Default listen `127.0.0.1`; cap `Image.MAX_IMAGE_PIXELS`; validate the `replydata` JSON shape
before mapping; optionally cap datagram size.

### LOW
- **L-1** Stale resources never pruned: `thumbnails_data` textures and `request_timestamps`
  entries for sources that vimix pruned are never removed → registry/dict growth over churn.
- **L-2** `np.roll(audio_buffer, ...)` allocates a fresh 6 s buffer every audio callback
  (~43×/s, ~1 MB each). Use a ring buffer / `collections.deque` or preallocated buffer with
  modulo indexing.
- **L-3** `tracks_data` / `phase_nudge` shared between the UI thread and `sequencer_tick` with
  no lock — GIL makes individual ops atomic, but read-modify-write sequences are racy.
- **L-4** Audio stream never closed on exit (no `try/finally` around the main loop).
- **L-5** Main loop sleeps 5 ms → ~200 fps render; `refresh_monitor_player_values` calls
  `dpg.set_value` for every prop every frame even when unchanged.
- **L-6** Magic numbers (MED-adjacent; see G25).

---

## What's good (kept in mind for the reviewer)

- Queue-based thread communication (`ui_state_queue`, `blob_queue`, `texture_queue`,
  `log_queue`) is the right pattern — extend it to UI updates (HIGH-1 fix) rather than
  replacing it.
- OSC contract with `viosc.py` verified end-to-end: `/viosc/replythumb/<name>/<idx>` blob
  parsing (`parts[-2]`, `parts[-1]`), `/viosc/monitor/<id>` props/stop semantics, 6666/6667
  roles all match.
- Pervasive `dpg.does_item_exist` guards make the code resilient to item churn.
- Thumbnail requests are rate-limited (3 s) and the fade loop correctly catches up on missed
  ticks (`expected_msg_index` catch-up loop).

---

## Fix Log (2026-02-07) — HIGH + MED items closed

### HIGH-1 — Thread-unsafe DearPyGui calls — FIXED
Added `ui_task_queue` drained on the main thread each frame; `ui_task()`, `enqueue_set_value()`,
and `log_error()` helpers. All off-main-thread UI access now routes through the queue:

- `audio_callback` → `enqueue_set_value("vu_meter", ...)`
- `essentia_analyzer_loop` → `enqueue_set_value("testo_bpm", ...)`; the `cb_lowpass` read is
  replaced by a cached `lowpass_enabled` flag synced via `on_lowpass_toggle` (checkbox now has
  a callback)
- `sequencer_tick` → theme updates via `update_step_theme` → `_apply_step_theme` on main thread;
  rand-value displays via `enqueue_set_value`; beat LED via `flash_beat_led()`
- `visual_metronome_loop` → `flash_beat_led()`
- `turn_off_led` (runs on a `threading.Timer` thread) → queues its own work

Verified: zero direct `dpg.` calls remain in any worker thread function.

### HIGH-2 — AlphaF fade stomping later steps — FIXED
`sequencer_tick` now sets `track["active_fade"]["active"] = False` at the top of every active
step dispatch (before the type branch), so a pending multi-step fade is cancelled the moment a
non-AlphaF step fires; the AlphaF branch still replaces the dict with a fresh active fade.

### MED-3 — ColorR cell value — FIXED
`last_rand_color` is now stored normalized (0..1, matching what the sequencer already computes);
`update_step_ui` and the tick both pass/`set_value` 3 components in 0..1 to the `no_alpha`
color_edit.

### MED-4 — Silent main-path failure — FIXED
`update_vimix_sources_ui` now: validates the payload is a JSON object, validates `sources` is an
object, drops non-dict source entries, coerces string/non-numeric `index`/keys defensively
(`get_sort_index` try/except with fallback), and logs failures to the OSC log window via
`log_error` instead of stdout-only prints.

### MED-5 — Dependencies — FIXED
Added `requirements.txt` pinned to the known-good intersection. PyPI today serves only
DearPyGui 2.x (API-incompatible with this code — it uses the 1.x `create_viewport`/
`setup_dearpygui` flow) and essentia wheels for few Pythons; the intersection that installs
cleanly is **Python 3.11**: `dearpygui==1.9.1`, `essentia==2.1b6.dev1177`, `numpy>=1.26,<2`,
`python-osc==1.10.2`, `sounddevice==0.5.6`, `Pillow>=11,<13`. NOTE: the app cannot currently
run in this environment (Python 3.13 has no compatible dearpygui 1.x/essentia wheels); a
DearPyGui 2.x migration is the follow-up if 3.13 support is required.

### MED-6 — Network-input hardening — FIXED
- Default listen IP changed `0.0.0.0` → `127.0.0.1`
- `Image.MAX_IMAGE_PIXELS` set to 25 MP; explicit `MAX_THUMBNAIL_PIXELS` (3 MP) check in
  `thumbnail_decoder_worker` before `convert('RGBA')`
- `incoming_osc_handler` caps `replydata` (1 MB) and thumbnail blobs (8 MB); handler wrapped in
  try/except → `log_error`

### Regression coverage
`tests/test_fixes.py` — stub-based harness (dearpygui/sounddevice/essentia/pythonosc stubbed,
real module imported headless) exercising all six fixes through the real code paths, including a
live sequencer-thread test of the fade cancellation. **19/19 checks pass.**

### Remaining (LOW / style — not addressed)
L-1 stale thumbnail pruning, L-2 `np.roll` ring buffer, L-3 `tracks_data` lock, L-4 stream
close on exit, L-5 200 fps render + per-frame `set_value`, L-6 magic numbers; file length
(1080 lines), function length, missing type hints, mixed IT/EN comments.
