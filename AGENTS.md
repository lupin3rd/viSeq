# viseq — AI Agents

Read CONVENTIONS.md before any GitHub or git operation.

<!-- BEGIN bigpowers:project -->
## Project

Audio-reactive VJ controller for Vimix via the viOSC daemon (8x8 sequencer,
thumbnail grid, monitor players, VU/BPM analysis, OSC). Single-file app.
Stack: Python 3.13, DearPyGui 2.3.1, essentia, numpy, python-osc,
sounddevice, Pillow.

## Commands

| Action | Command |
|--------|---------|
| Run | `.venv/bin/python viseq.py` |
| Test | `.venv/bin/python -m pytest tests/ -q` |
| Lint | `.venv/bin/ruff check .` |
| Format | `.venv/bin/ruff format .` |
| Typecheck | `.venv/bin/mypy` |
| Preflight | `.venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/python -m pytest tests/ -q` |

## Architecture

Single-file DearPyGui app. The main thread owns all UI access. Worker threads
(audio, OSC, thumbnail decode, sequencer/fade) MUST route UI mutations through
`ui_task_queue` (HIGH-1 invariant). The OSC contract with viOSC is frozen.

## Conventions

- Conventional Commits (`feat`/`fix`/`chore`/`refactor`/`style`/`test`) — already in use.
- Type hints on public functions. Comments in English. UI labels stay English (e08s02: full English pass).
- Named constants for magic values (audit L-6).

## Never

- Never change the frozen OSC contract with viOSC/Vimix.
- Never call dpg APIs from worker threads — always via `ui_task_queue`.
- Never call any dpg API before `create_context()` (segfault landmine).
- Never split viseq.py without explicit user agreement.
- Never dismiss reproducible gate failures as pre-existing or out of scope.
- Never proceed on red Preflight — invoke quick-fix or fix-bug first.
<!-- END bigpowers:project -->

<!-- BEGIN bigpowers:context-routing -->
## Context Routing

No sub-agent routing tables in this single-file project.
<!-- END bigpowers:context-routing -->

<!-- BEGIN bigpowers:learned-preferences -->
## Learned Preferences

- (empty)

## Workspace Facts

- Runtime target: Python 3.13 + dearpygui 2.3.1 + essentia 2.1b6.dev1389 (cp313 wheels).
- Pre-commit hooks installed. Skipping hooks (`--no-verify`) is forbidden.
<!-- END bigpowers:learned-preferences -->

<!-- BEGIN bigpowers:tooling -->
## Tooling

- Pre-commit hooks (G6): `.pre-commit-config.yaml` — ruff, ruff-format, mypy, pytest.
- Dev dependencies: `requirements-dev.txt`.
<!-- END bigpowers:tooling -->

## Agent Rules

- **Workflow Mandate:** You MUST use the bigpowers skills (e.g. `plan-work`,
  `develop-tdd`, `orchestrate-project`) to perform tasks. DO NOT write code
  directly in response to a user prompt like "build this feature".
- **Always Green:** Preflight must be green before forward work. Reproducible
  gate failures require fix-or-log (quick-fix → fix-bug) per CONVENTIONS
  § Discovered Defects.
- Read `specs/` before writing code.
- All planning and specifications MUST be written to `specs/`
  (`product/SCOPE_LATEST.yaml`, `release-plan.yaml`, `epics/`) before any code
  is generated.
- Write the minimum code that solves the stated problem. Nothing extra.
- Run tests after every change. Show evidence before declaring done.
- One clarifying question beats a wrong assumption baked into 200 lines.
