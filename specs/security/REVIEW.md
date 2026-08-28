# specs/security/REVIEW.md — security review, branch feat/e11-project-save-load (e11-e13)

**Reviewed at:** 2026-08-28T13:30:00Z
**Diff:** `git diff main...HEAD` — epics e11 (project save/load), e12 (menubar
shell), e13 (window polish + dialogs + version 0.1.0).

## Threat model

Desktop GUI (DearPyGui), local-only: reads/writes `viseq_config.json` and
`.viseq` project files chosen via local file dialogs; talks OSC to viOSC/Vimix
(frozen contract, untouched by this branch). No network server added, no
authentication boundary, no multi-tenant data.

## Findings

| # | Severity | Confidence | Finding | Status |
|---|----------|------------|---------|--------|
| 1 | LOW | 6 | `load_project_file` reads an arbitrary user-chosen file fully into memory before validating format/version — a very large file costs memory. Same trust model as the pre-existing config loader (local user, own files). | Accepted (documented in e11-verify.yaml) |
| 2 | LOW | 4 | Project files can carry any `target_id`/`base_address` strings; they are used only to build OSC addresses (`/vimix/<id>/...`) against the local viOSC daemon — no injection surface outside the already-frozen OSC contract. | Accepted |

No HIGH findings (confidence ≥ 8): **merge gate clear**.

## Controls verified

- `load_project_file`: `json.load` wrapped in `except (OSError, ValueError)`;
  format + version validated; `_sanitize_project_state` coerces types via
  `_to_float`, heals defaults, drops unknown keys — no eval, no code execution.
- `load_config`: unknown top-level keys dropped; palette sanitized.
- `save_project_to_file` / `save_config`: atomic tmp + `os.replace`.
- All new code runs on the main thread; worker threads and the `ui_task_queue`
  routing are untouched.
- File-dialog callbacks read `app_data.get("file_path_name")` defensively and
  the paths are user-chosen local paths (no traversal vector).
