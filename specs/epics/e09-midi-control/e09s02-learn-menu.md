# e09s02 — MIDI Learn flow + MIDI menu (Enable, Learn, Mappings window with Delete, Save)

**type:** feat
**risk:** P1
**context:** midi/ui

**Context:** The user's requested learn flow: first click the viseq control, then press the
MIDI button. Learn mode wraps the sequencer widgets: while active, the next click on a
learnable control records its action (and does not execute it); the next incoming MIDI
message is captured by the worker and turned into a binding. The menubar gains a "MIDI"
menu: Enable, Learn (toggle + status hint), a Mappings window (list + per-binding Delete)
and Save.

## Requirements

#### ADDED: Learn mode plumbing
`midi_learn_mode: bool`, `midi_learn_pending: tuple[action, params] | None`, and a
`learnable(callback, action_builder)` wrapper: when learn mode is on, the wrapper records
the widget's action into `midi_learn_pending` (status text "Now press your MIDI button")
and skips the real callback; otherwise it delegates unchanged. The worker thread, when
`midi_learn_pending` is set, captures the next message and pushes a binding creation
(device from the port, channel/type/number from the message) to `ui_task_queue`, which
appends it to `midi_bindings` and clears the pending slot. Binding order: the user can
learn a second message to overwrite the pending one; a Cancel (menu toggle or Esc) clears
it.

#### ADDED: Learnable sequencer widgets
Step-cell checkbox → `seq_toggle(row, col)`; PLAY → `transport_play`; RESYNC →
`transport_resync`; TAP → `transport_tap`; nudge `<`/`>` → `nudge_back`/`nudge_forward`;
beat-source checkboxes → `beat_source(mode)`; ASSIGN CLIP → `track_assign(row)`. Each
wrapper preserves the existing callback signature and `user_data`.

#### ADDED: MIDI menu + Mappings window
Menubar "MIDI" menu: "Enable MIDI" checkbox (persisted in the config `midi.enabled`),
"Learn mapping..." item (toggles learn mode; a status line shows the current step:
"Click a viseq control" / "Now press your MIDI button" / "Bound: <action> ← <device> note
<number>"), "Mappings..." opens a hidden-by-default `midi_mappings_window` listing every
binding (device, type, number, action, params) with a per-row "Delete" button, and "Save"
persists the list via the atomic `save_config`. All labels are English.

#### ADDED: All MIDI features live in one MIDI window (user revision)
The menubar has a single "MIDI" item that opens one `midi_window` (hidden by default, like
Settings) containing everything: the "Enable MIDI" checkbox, the Controller combo with a
Refresh button (live device re-scan), the MIDI Learn button (doubles as Cancel; the status
line explains each step and refuses to learn while MIDI is disabled — "Enable MIDI
first"), and the Mappings list with per-row Delete and a Save button. No scattered
menubar items remain. Switching the Controller combo while listening reconnects the
worker to the new port live.

#### MODIFIED: MIDI menu vs window
**Before:** a menubar "MIDI" menu with Enable/Learn/Mappings/Save items plus a separate
Mappings window; learn had no guard and no visible feedback outside the closed menu.
**After:** a single menubar "MIDI" item opens the consolidated window; learn is guarded by
"Enable MIDI first" and the status line is always visible in the window.

## Steps

1. Learn plumbing (flag, pending slot, worker capture, binding creation, cancel)
   → verify: `.venv/bin/python -m pytest tests/ -q -k 'midi_learn'`
2. Wrap the sequencer widgets with `learnable()` → verify:
   `.venv/bin/python -m pytest tests/ -q -k 'learnable'`
3. MIDI menu + Mappings window + Save → verify:
   `.venv/bin/python -m pytest tests/ -q -k 'midi_menu or midi_mappings'`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig (manual): MIDI > Learn mapping… → click a step cell → press a Launchpad pad →
   the Mappings window shows the binding; delete it; learn it again; MIDI > Save; restart
   the app → the binding is still there and works.

## Out of scope

- Learn for monitor/analyzer widgets (later epic).
- Preset profiles / multiple named mapping sets (Save is a single list).

## Risks

- Learn clicking the wrong control — the status line and Cancel make it recoverable; the
  Mappings Delete cleans up mistakes (user requirement).
- Wrapping callbacks changes `sender` semantics — the wrapper forwards the original
  `(sender, app_data, user_data)` untouched when not learning.
