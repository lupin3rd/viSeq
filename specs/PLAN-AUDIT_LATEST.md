# Plan Audit — viseq (viOSC VJ sequencer)
**Date:** 2026-02-07 · **Verdict:** NOT READY

> **Context:** This audit ran inside `viseq/`, which is not a bigpowers project. There is **no
> plan document** in the repo (no PRD, scope, stories, or epics) and no conventions scaffolding
> (no CLAUDE.md/AGENTS.md/CONVENTIONS.md, no git repo). The only plan-like material is the
> defect list produced by the prior `audit-code` run (`specs/verifications/AUDIT-viseq.md`) and
> the fixes applied since. The lens scores below therefore assess the de facto situation; the
> single most important gap is that **there is no plan to audit**.

## Principles Alignment

| Check | Status | Note |
|-------|--------|------|
| Vertical slices | ❌ | No stories exist. The only candidate work items are a defect list (LOW items L-1..L-6, style items) plus one architectural decision — none of it sliced or sequenced. |
| Scope bounded | ❌ | Nothing declares in_scope/out_of_scope. Even the follow-ups are fuzzy: e.g. "DearPyGui 2.x migration *if* Python 3.13 support is required" is an open fork, not a boundary. |
| Success criteria | ⚠️ | The audit defined verifiable outcomes for the fixes (19/19 regression checks, zero direct `dpg.` calls in worker threads, fade-cancel invariant). No plan-level done-criteria exist for any future work. |
| HARD GATE candidates | ⚠️ | One real decision gate: **DearPyGui 1.x (Python ≤3.11) vs 2.x migration (Python 3.13)** — blocks environment choice, requirements.txt, and any UI work. A second: whether `viseq.py` stays a single file or is split into modules (affects everything downstream). |
| Domain language | ⚠️ | Consistent terms in code (step, track, `active_fade`, AlphaF/AlphaV/AlphaR/ColorV/ColorR, viOSC, vimix source, monitor player) but undocumented and mixed IT/EN; no glossary exists. |

## Conventions Completeness

| Check | Status | Note |
|-------|--------|------|
| CLAUDE.md / AGENTS.md | ❌ | Absent. |
| CONVENTIONS.md | ❌ | Absent. |
| specs/ layout | ❌ | Only `specs/verifications/AUDIT-viseq.md` exists. No `state.yaml`, `release-plan.yaml`, `epics/`, `adr/`, `product/`. |
| Commit conventions | ❌ | Not a git repo — no commits, no Conventional Commits. |
| Git workflow mode | ❌ | `solo-git` / `team-pr` both inapplicable: **no git repository at all**. |

## Pre-flight Answers

| Question | Answer | Status |
|----------|--------|--------|
| Test command | `python3 tests/test_fixes.py` (stub harness, 19 checks) — exists but is not a test framework; no unit tests | ⚠️ |
| Build command | None — interpreted Python, no packaging | ❌ |
| Lint command | None configured | ❌ |
| Typecheck command | None — zero type hints in ~1,100 lines | ❌ |
| CI platform | None | ❌ |
| Solo or team | Undeterminable — no git | ❌ |
| Language + framework | Python 3.11 target, DearPyGui 1.x, python-osc, numpy, sounddevice, essentia; companion daemon `viosc.py` (OSC contract verified) | ✅ |
| Greenfield or existing | Existing codebase (working app) but **zero bigpowers scaffolding** → would need `seed-conventions` (adapted) — no foreign spec format to migrate | ✅ |

## Open Gaps (close conversationally, one at a time)

- [ ] **G1 (blocking):** There is no plan document. What is the plan being audited — the audit follow-up work (LOW items + style + the DPG 2.x decision), a plan you will paste, or something else?
- [ ] **G2:** Decide the DearPyGui fork: lock Python 3.11 + dpg 1.9.1, or plan the 2.x migration. This is the first HARD GATE.
- [ ] **G3:** Define in_scope/out_of_scope for the accepted work (via `scope-work`).
- [ ] **G4:** Define success criteria per work item (verifiable, test-backed).
- [ ] **G5:** Bootstrap project conventions: CLAUDE.md + CONVENTIONS.md (`seed-conventions`) and initialize git (enables `solo-git`, commits, churn-based review).
- [ ] **G6:** Choose and pin the mechanical gates: lint (ruff), typecheck (mypy, at least on new code), test runner (pytest) — needed before `develop-tdd`/`verify-work`/`audit-code` can gate anything.

## Verdict

**NOT READY** — G1 is blocking: there is no plan to assess. Even accepting the audit follow-up
as the plan, it lacks scope boundaries, slicing, and success criteria, and the project lacks all
conventions and mechanical gates. Close G1 first, then the remaining gaps.

## Next Skill (depends on G1)

- Follow-up work on existing code → `elaborate-spec` (turn the defect list into a real spec), then `scope-work` → `slice-tasks`
- Bootstrap conventions first → `seed-conventions` (adapted; see note above)
- A plan document exists elsewhere → paste it and re-run this audit
