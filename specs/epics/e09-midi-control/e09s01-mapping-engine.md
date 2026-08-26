# e09s01 — MIDI mapping engine: binding schema + pure matching + action dispatch + worker + config

**type:** feat
**risk:** P1
**context:** midi/control

**Context:** The engine is the foundation: bindings stored in the config, a pure matcher,
an action vocabulary the existing GUI callbacks and the MIDI worker both go through, and a
worker thread that never touches dpg (HIGH-1 — executions go through `ui_task_queue`, the
same queue the other workers use). mido 1.3.3 + python-rtmidi are already dependencies.

## Requirements

#### ADDED: MIDI binding schema + config persistence
`viseq_config.json` gains a `midi` section: `{enabled: bool, input_port: str|null,
bindings: [{device, channel, type ("note"|"cc"), number, action, params}]}`. The section
merges into `DEFAULT_CONFIG` with defaults (enabled False, bindings []), so existing
configs (layout + theme only) load unchanged — the existing deep-merge in `load_config`
provides this. Save via the existing atomic `save_config`.

#### ADDED: Pure binding matching
`binding_matches(binding, msg_type, number, channel) -> bool` compares the binding's
`type` (note on/off vs cc), `number` (note or control) and `channel` against a parsed
message. `resolve_midi_message(msg, port_name) -> list[tuple[str, dict, int]]` returns
`(action, params, value)` tuples for every matching binding: note-on with velocity>0 fires
once per edge (note-on velocity 0 and note-off are release edges, not triggers); CC fires
with the controller value. Device filtering uses the port name.

#### ADDED: Action vocabulary (sequencer scope, main-thread imperative)
`midi_action_seq_toggle(row, col)`, `midi_action_transport_play()`,
`midi_action_transport_resync()`, `midi_action_transport_tap()`,
`midi_action_nudge_back()`, `midi_action_nudge_forward()`,
`midi_action_beat_source(mode)`, `midi_action_track_assign(row)` — imperative helpers
that mutate `tracks_data`/state and update the UI. The existing GUI callbacks
(`toggle_step_active`, `toggle_play`, `callback_resync`, `tap_bpm`, `on_beat_source`,
`assign_clip_to_track`) delegate to the same helpers, so a MIDI trigger and a mouse click
are literally the same code path.

#### ADDED: midi_control_loop worker + enable/disable
A daemon worker thread (same pattern as `midi_clock_loop`): when enabled, opens the
configured input port (falling back to device-name discovery for the Launchpad), loops
over `iter_pending()`, resolves messages against the bindings, and pushes each resolved
execution to `ui_task_queue`. Without ports (headless CI), it logs one line and idles.
Disabling stops dispatching; the thread survives with a wakeup event.

## Steps

1. Extend `DEFAULT_CONFIG` with the midi section (deep-merge keeps old configs valid)
   → verify: `.venv/bin/python -m pytest tests/ -q -k 'midi_config or config'`
2. `binding_matches` + `resolve_midi_message` (pure, note/CC edge semantics)
   → verify: `.venv/bin/python -m pytest tests/ -q -k 'midi_resolve or binding_matches'`
3. Action helpers + delegate the existing GUI callbacks to them
   → verify: `.venv/bin/python -m pytest tests/ -q -k 'midi_action'`
4. `midi_control_loop` + enable/disable wiring → verify:
   `.venv/bin/python -m pytest tests/ -q -k 'midi_loop or midi_enable'`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Headless engine check: a binding (note 36 → seq_toggle(0,0)) resolves a note-on 36 into
   one seq_toggle execution; a CC 7 binding resolves with its value (unit tests).
3. Real rig (manual): enable MIDI in the app; with any MIDI controller connected, a
   learned binding triggers the action. (Full acceptance with the Launchpad comes in e09s03.)

## Out of scope

- Learn UI and the MIDI menu (e09s02) and the Launchpad adapter (e09s03).
- CC-consuming continuous actions (engine dispatches the value; no v1 action consumes it).

## Risks

- mido backend differences (rtmidi) — existing clock loop already proves the stack works.
- A binding storm (e.g. pitch-bend-like CC spam) — the worker coalesces: only the latest
  value per (device, type, number) is dispatched per main-loop drain (same pattern as
  ui_state_queue's latest-wins).
