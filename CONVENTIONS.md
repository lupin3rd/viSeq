# Conventions — viseq

Shared rules for all AI agents working in this repository. Read before any git
operation. See `AGENTS.md` (symlinked as `CLAUDE.md`) for the session brief.

## Conventional Commits

All commits MUST follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/).

### Commit Message Format
`<type>(<scope>): <description>` — space after the colon is MANDATORY.

### Types
- `feat`: new feature (minor)
- `fix`: bug fix (patch)
- `perf`: performance improvement (patch)
- `docs`, `chore`, `style`, `refactor`, `test`: no version bump
- `BREAKING CHANGE:` (or `!` after type): major

### Git Attribution
NEVER include `Co-authored-by` or any other footer that attributes code to an
AI agent. All commits appear authored solely by the human user.

## Always Green / Shift Left

**Always Green** means Preflight is green before any forward work — not "green
enough for this task".

**Shift Left (1-10-100):** defects cost roughly 1× to fix in development, 10×
in integration, 100× in production. Fixing a red gate now is cheaper than
shipping and debugging later.

**Preflight** is the project's full local verification stack:

```bash
.venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/python -m pytest tests/ -q
```

Preflight MUST pass before kickoff, develop, or verify phases advance.

**Pre-commit hooks** (`.pre-commit-config.yaml`) run ruff, ruff-format, mypy,
and pytest before every commit. Skipping hooks (`--no-verify`) is forbidden
unless explicitly authorized for a specific commit and documented.

## Discovered Defects

Any **reproducible gate failure** encountered during unrelated work is a
discovered defect — not optional background noise.

**fix-or-log ladder (mandatory):**
1. **quick-fix** — trivial, data-only, or single-file fixes within guardrails.
2. **fix-bug** — when quick-fix guardrails abort, or the failure needs
   investigation (`specs/bugs/BUG-*.md` + TDD).
3. **Log** — only when reproduction is blocked after good-faith attempt; write
   a BUG spec and stop forward work on the original task until triaged.

**Hard block:** red Preflight blocks forward progress until fix-or-log
produces green.

### Banned dismissive phrases

Agents MUST NOT use these phrases (or close paraphrases) to ignore reproducible
failures:

| Banned phrase | Required behavior instead |
|---------------|---------------------------|
| Pre-existing / pre-existing issues | Run fix-or-log; if truly unrelated, prove with a passing repro after revert |
| unrelated to this session | Same — session boundaries do not waive green gates |
| not introduced by my changes | Bisect or fix anyway; solo ownership covers the whole tree |
| out of scope (ignoring a red gate) | Invoke quick-fix or fix-bug; scope-minimization never overrides Always Green |

## specs/ — All Planning Output Goes Here

Every skill that produces written output writes to `specs/` at the project root.

| Question | File | Format |
|----------|------|--------|
| What should the product do? | `specs/product/SCOPE_LATEST.yaml` | YAML |
| North star / initiative | `specs/product/VISION_LATEST.yaml` | YAML |
| Glossary | `specs/product/GLOSSARY_LATEST.yaml` | YAML |
| What ships in this release, in what order? | `specs/release-plan.yaml` | YAML |
| Epic manifest + story specs | `specs/epics/eNN-slug/` | YAML + MD |
| Session state / handoff | `specs/state.yaml` | YAML |
| Progress (sole SoT for story state) | `specs/execution-status.yaml` | YAML |
| Stack / architecture | `specs/tech-architecture/tech-stack.md` | MD |
| Architectural decisions | `specs/adr/ADR-*.md` | MD |
| Bug investigation | `specs/bugs/BUG-*.md` + `specs/bugs/registry.yaml` | MD + YAML |
| Verify evidence, audit reports | `specs/verifications/` | MD |

Do not put story status in `release-plan.yaml`. Do not duplicate the release
plan inside `state.yaml`.

## Code Style

- Python formatter: **ruff format** (`.venv/bin/ruff format .`). No style
  debates beyond the formatter.
- Lint: **ruff** with the curated rule set in `pyproject.toml`
  (E/F/I/UP/B/SIM/C4/RUF). Typecheck: **mypy** (`pyproject.toml` config).
- Functions: 4–20 lines. Split if longer.
- One thing per function, one responsibility per module (SRP).
- Names: specific and unique. Avoid `data`, `handler`, `Manager`, `Service`.
  Prefer names whose grep returns < 5 hits in this codebase.
- Types: explicit. No untyped public functions. No `Any` leaks on purpose —
  module-level data structures carry explicit annotations.
- No code duplication. Extract shared logic into a function.
- Early returns over nested ifs. Max 2 levels of indentation.
- Conditionals expressed as positives. Avoid negative flags.
- No magic strings or numbers: every bare literal used in logic is a named
  constant (audit L-6).
- Complex boolean expressions live in named predicate functions.
- Prefer exceptions over error codes.
- Remove dead code (G9/F4): delete, never comment out.
- Boy Scout Rule: leave every file you touch at least as clean as you found it.
- Exception messages include the offending value, expected shape, and an
  actionable remediation hint.

## Comments

- Keep your own comments. They carry intent and provenance.
- Write WHY, not WHAT.
- Comments in **English**; UI labels stay **Italian** (user-facing).
- No obvious comments that restate the code.
- No commented-out code (C5): dead code is deleted, not commented.

## Tests (F.I.R.S.T)

- Headless single command: `.venv/bin/python -m pytest tests/ -q`.
- The harness (`tests/test_fixes.py`) stubs dearpygui/sounddevice/essentia/
  pythonosc and imports the real `viseq.py` (main loop skipped because
  `is_dearpygui_running()` is False).
- Every new function gets a test. Every bug fix gets a regression test.
- Mocks for external I/O are named fake classes, not inline stubs.
- Tests are **F**ast, **I**ndependent, **R**epeatable, **S**elf-Validating,
  **T**imely.
- Never skip or ignore a test without an explicit ambiguity note (T4).
- Test boundary conditions (T5): empty input, maximum, minimum, off-by-one.
- Test through public interfaces only (T8): assert on observable outcomes.
- GUI rendering cannot be verified headless — the user manually accepts on the
  real rig.

## Defensive Code

The agent implements defensive code only for these explicitly agreed
categories (from the seed-conventions interview):

- **Rate limit** — caps on network-fed data: thumbnail blob size, state JSON
  size, image pixels (audit MED-6).
- **Graceful degradation** — worker threads must never die from unexpected
  exceptions; the GUI keeps running when OSC/audio are absent.
- **Timeout** — bounded waits (decode loops, join timeouts).

**Deliberate exceptions on `viseq.py`** (per-file ruff ignores, documented):
- `BLE001` / `S110` — blind `except Exception` in worker threads and shutdown
  paths is the intended defensive posture for a single-file GUI: any exception
  must be caught and logged, never allowed to kill a worker thread.
- `PLW0602` — module-global shared state (`tracks_data`, `global_vimix_state`,
  ...) is the intended single-file design; annotations make it explicit.

## Never-Do List

- Never change the frozen OSC contract with viOSC/Vimix (addresses and
  payloads in `specs/planning-context.yaml`).
- Never call dpg APIs from worker threads — always via `ui_task_queue`
  (audit HIGH-1).
- Never call any dpg API before `create_context()` — segfault landmine
  (see `specs/archive/spikes/SPIKE-dpg2x-api.md`).
- Never split `viseq.py` into modules without explicit user agreement
  (deferred decision from epic e01).
- Never modify viOSC or Vimix — external, contract frozen.
- Never skip pre-commit hooks (`--no-verify`) without explicit authorization.

## Workflow

- Solo-git mode (`specs/state.yaml` → `workflow_mode: solo-git`): work
  directly on `main` with conventional commits, or on a feature branch when a
  story warrants isolation (kickoff-branch). PR flow is optional.
- Use the bigpowers skills for feature work: `survey-context` → `plan-work` →
  `develop-tdd`/`execute-plan` → `verify-work`. DO NOT write feature code
  directly in response to a prompt.
- Read `specs/` before writing code.
- Run tests after every change. Show evidence before declaring done.
- One clarifying question beats a wrong assumption baked into 200 lines.
