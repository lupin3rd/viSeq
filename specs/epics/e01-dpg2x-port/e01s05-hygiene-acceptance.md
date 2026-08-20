# e01s05 — Hygiene (type hints, EN comments, constants) + manual acceptance

**type:** refactor
**risk:** P2
**context:** domain

**Context:** Final story of the epic: behavior-preserving hygiene across the single file (type
hints on public functions, Italian code comments -> English, audit item L-6 named constants)
and the epic's closing gate — the user's manual acceptance run on the real rig with a
checklist, per the success criteria agreed in elaborate-spec ("I'll try it and report
problems back").

## Requirements

#### ADDED: Type hints on public functions
Every module-level public function carries parameter/return type hints. Runtime behavior
unchanged (hints are informational; no mypy gate exists in this project — see
SCOPE_LATEST out_of_scope).

#### ADDED: English code comments
All code comments are English; Italian remains only in user-facing UI strings (the UI is
Italian by design).

#### ADDED: Named constants for magic numbers (L-6)
The remaining naked numbers in behavioral paths (3.0 s thumbnail request interval, 25-entry
log cap, layout widths) are named constants.

#### ADDED: Manual acceptance gate
The user runs viseq on the real rig against the acceptance checklist below and reports
problems; all reported regressions are resolved before the story is done.

## Steps

1. Add type hints to public functions (callbacks, loop workers, helpers, update_* UI builders) → verify: `python3 -m py_compile viseq.py && python3 tests/test_fixes.py`
2. Replace Italian code comments with English (keep UI strings Italian) → verify: `python3 -c "import re,io; src=open('viseq.py',encoding='utf-8').read(); it=[l for l in src.splitlines() if re.match(r'\s*#',l) and re.search(r'\b(struttura|selezionato|nuovo|usando|della|rigenero)\b', l, re.I)]; assert not it, f'IT comments left: {it}'; print('OK')"`
3. L-6: extract named constants for the remaining behavioral magic numbers (thumb request interval 3.0, log cap 25, monitor layout 280/260) → verify: `grep -cE '^THUMB_REQUEST_INTERVAL|^LOG_HISTORY_LIMIT|^MONITOR_OFFSET' viseq.py | grep -q 3`
4. Run the full regression harness + boot check → verify: `python3 tests/test_fixes.py && timeout 8 .venv/bin/python viseq.py; test $? -eq 124`
5. Manual acceptance (verify-script) — user runs the checklist below on the real rig and reports problems; each reported problem is triaged (reopen e01sNN or new bug) → verify-script: see "Manual Acceptance Checklist" below.

## Manual Acceptance Checklist (verify-script)

Run on the real rig with viOSC + Vimix running (Python 3.13 venv):

1. Start viseq — window opens, no traceback.
2. viOSC window: Connect Client (127.0.0.1:6666) — status "Ready"; Start Server (127.0.0.1:6667) — status "Listening".
3. Media Library populates with Vimix sources; thumbnails appear within seconds.
4. Assign a clip to a sequencer slot; play a pattern with each step type (AlphaV, AlphaR, AlphaF with frames>1, ColorV, ColorR) — OSC log shows the messages; Vimix responds (alpha/color changes).
5. During an AlphaF fade (frames>1), trigger a ColorV/AlphaV step on the same track — the new step's value wins (HIGH-2 regression check).
6. Monitor: create a Monitor Player, assign the current source, pick properties — values refresh.
7. Audio: enable Level Analysis — VU moves; enable BPM — BPM/Confidence updates; toggle low-pass.
8. Play/STOP/RESYNC/< > nudge — LED and step highlighting follow.
9. Remove a source in Vimix — its tile disappears, no crash (L-1).
10. Close the app — clean exit, no orphaned processes.
11. Report any problem to the agent with repro steps.

## Out of scope

- UI string translation (UI stays Italian).
- mypy/ruff configuration, CI wiring.
- Module split (deferred).

## Risks

- Type-hint edits touching callback signatures could change behavior if annotations are
  incorrect — mitigate by running the harness after every hint batch (step 1 verifies).
- Comment translation is cosmetic but touches many lines — harness + boot check catch any
  accidental code change.
