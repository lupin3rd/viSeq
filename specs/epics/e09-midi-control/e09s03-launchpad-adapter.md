# e09s03 — Novation Launchpad adapter: model detection, 8x8 grid bindings, sequencer LED mirror

**type:** feat
**risk:** P0
**context:** midi/hardware

**Context:** The headline hardware integration. The user's device is a "Novation Launchpad"
(family). Per the official references: MK2-family grids are MIDI notes `row*10+col`
(0-79) with the LED color set by the note-on velocity; the SysEx header is
`F0 00 20 29 02 0C`; Launchpad X / Mini MK3 must first be switched to programmer mode via
SysEx and then behave as a note grid. Model auto-detection from the port name
(`mido.get_input_names()`), with the user able to override in the config, covers the whole
family without asking. The 8x8 pad grid maps 1:1 to the 8x8 step sequencer; the pad LEDs
mirror step state in real time.

## Requirements

#### ADDED: Model detection + connect
`launchpad_model_from_name(port_name) -> str|None` classifies port names ("Launchpad",
"Launchpad MK2", "Launchpad Mini", "Launchpad X", "Launchpad Pro" …) into protocol
classes: `note_mode` (MK2 family) and `programmer_mode` (X/Mini MK3). On connect, a
programmer-mode device receives the setup SysEx once; afterwards both classes use the same
note grid. The adapter opens a mido output port (same name as the input when available,
else the first output whose name matches) and stays inert (no-op) when no port exists —
headless CI is unaffected. When MIDI is enabled and a Launchpad input is chosen, the
adapter binds its grid.

#### ADDED: Note table + LED addressing
`LAUNCHPAD_GRID_NOTE(r, c)` per the MK2 table (`r*10+c`), plus the top/right control
addresses (right column notes 80-89, top row CCs 104-111 for MK2; MK3-family uses
104-119) — exact per-model values from the spike/implementation probes, validated on the
real device. `launchpad_led(r, c, velocity)` sends the note-on with the color velocity
through a lock-guarded output port (MK2 palette: 0 = off, green = 60, amber = 12,
white = 3, red = 5 — values from the manual).

#### ADDED: Grid bindings (internal)
Pressing pad (r,c) toggles step (r,c): the adapter registers internal bindings
`note row*10+col -> seq_toggle(r, col)` on connect. They are engine bindings (same
resolution path as learned ones) but flagged `auto` and never stored in the user config —
the user's mapping list stays clean.

#### ADDED: Sequencer LED mirror
Every step-state change on the main thread (toggle, type change, playhead advance in the
sequencer tick, beat flash) calls `launchpad_mirror_step(r, c)`: active step = green,
empty = off, current playhead = amber, beat flash = brighter pulse. Guarded by
`launchpad is connected and midi enabled`; a no-op otherwise.

#### ADDED: Three protocol classes (MK1 support — user's device novlpd01)
The adapter now distinguishes three classes by port name: `mk1` (plain "Launchpad"/
"Launchpad S" — the original 2009 model, product code novlpd01), `note` (MK2/Mini MK2/Pro)
and `programmer` (X/Mini MK3/Pro MK3). MK1 uses grid notes `16*row+col` and the official
velocity palette (16*Green + Red + 12): 12 off, 15 red, 63 amber, 62 yellow, 60 green.
MK2-family keeps `row*10+col` and the 128-color palette; MK3-family keeps programmer-mode
SysEx. Verified against the official Launchpad Programmers Reference PDF.

#### MODIFIED: Model detection
**Before:** two classes (note/programmer); the original MK1 was misdetected as MK2 and its
grid/colors were wrong (notes row*10+col and MK2 palette).
**After:** three classes; MK1 detection (plain "Launchpad" name) switches grid notes and
colors to the MK1 protocol; connect logs the detected class ("Launchpad (mk1) output on …").

## Steps

1. Model detection + connect + `launchpad_led` → verify:
   `.venv/bin/python -m pytest tests/ -q -k 'launchpad_model or launchpad_led'`
2. Grid auto-bindings through the engine → verify:
   `.venv/bin/python -m pytest tests/ -q -k 'launchpad_grid'`
3. LED mirror hooks in the step-state paths → verify:
   `.venv/bin/python -m pytest tests/ -q -k 'launchpad_mirror'`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig (manual, the user's Launchpad): enable MIDI, pick the Launchpad input; the pad
   grid mirrors the sequencer (empty = off, toggled = green, playhead = amber); pressing a
   pad toggles its step; during playback the playhead sweeps the pads with a beat flash.

## Out of scope

- RGB SysEx colors for MK3-family (v1 uses the velocity palette; RGB via SysEx
  `F0 00 20 29 02 0C 03 ...` is a later enhancement).
- Launchpad top/right control mappings (the 8 round buttons) — v1 leaves them free for
  user-learned bindings.
- Non-Launchpad grid controllers (APC etc.) — they go through the generic learned
  bindings only.

## Risks

- Exact note/color tables differ between MK2, Pro and X/MK3 — model detection + probe
  logs + manual acceptance on the real device close this; wrong-table risk is isolated in
  the pure `LAUNCHPAD_*` constants/table functions (unit-tested).
- Programmer-mode SysEx on X/MK3 must be sent before note mode works — done once at
  connect, idempotent.
- Sending MIDI from the main thread (LED mirror) is fast but must never block the loop —
  the send is lock-guarded and best-effort (exceptions logged, never raised).
