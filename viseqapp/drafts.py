"""Session Drafts (e43s05) — named, ordered file sets owned by viseq.

A draft is a list of absolute file paths that live on machine A; it is edited
here and only written as a vimix `.mix` when the user commits it (e43s06/s07).
The library is an **application-level** asset under ``~/.config/viseq`` (user
decision 7), NOT project content, so it survives project changes and is never
saved by the `.viseq` project.

DPG-FREE and network-free by construction (HIGH-1): the composition root owns the
UI, this module owns the model and the store.
"""

import json
import os
from typing import Any

from viseqapp.config import user_config_dir

DRAFTS_FORMAT = "viseq-drafts"
DRAFTS_VERSION = 1
DRAFTS_FILENAME = "drafts.json"
MAX_DRAFTS = 200
MAX_FILES_PER_DRAFT = 2000


def default_path() -> str:
    """The application-level library path (``~/.config/viseq/drafts.json``)."""
    return os.path.join(user_config_dir(), DRAFTS_FILENAME)


def empty_library() -> dict[str, Any]:
    """A valid, empty library."""
    return {"format": DRAFTS_FORMAT, "version": DRAFTS_VERSION, "counter": 0, "drafts": []}


def _clean_files(raw: Any) -> list[str]:
    """Valid, unique, non-empty paths, bounded per draft."""
    if not isinstance(raw, list):
        return []
    files: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and item not in files:
            files.append(item)
        if len(files) >= MAX_FILES_PER_DRAFT:
            break
    return files


def sanitize_library(raw: Any) -> dict[str, Any]:
    """A valid library from arbitrary input (drops junk, heals ids/names).

    A corrupt or hand-edited file must never block boot: everything invalid is
    skipped, ids are made unique, names are made unique and non-empty, and the
    id counter is raised above the largest id seen.
    """
    library = empty_library()
    if not isinstance(raw, dict):
        return library
    drafts_raw = raw.get("drafts")
    if not isinstance(drafts_raw, list):
        return library
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    next_id = 1
    drafts: list[dict[str, Any]] = []
    for entry in drafts_raw:
        if not isinstance(entry, dict) or len(drafts) >= MAX_DRAFTS:
            continue
        raw_id: Any = entry.get("id")
        try:
            draft_id = int(raw_id)
        except (TypeError, ValueError):
            draft_id = 0
        if draft_id < 1 or draft_id in seen_ids:
            draft_id = next_id
        while draft_id in seen_ids:
            draft_id += 1
        seen_ids.add(draft_id)
        next_id = max(next_id, draft_id + 1)

        name = str(entry.get("name") or "").strip() or f"Session {draft_id}"
        unique = name
        suffix = 2
        while unique in seen_names:
            unique = f"{name} ({suffix})"
            suffix += 1
        seen_names.add(unique)
        drafts.append({"id": draft_id, "name": unique, "files": _clean_files(entry.get("files"))})
    library["drafts"] = drafts
    raw_counter: Any = raw.get("counter")
    try:
        counter = int(raw_counter)
    except (TypeError, ValueError):
        counter = 0
    library["counter"] = max(counter, max(seen_ids) if seen_ids else 0)
    return library


def find_draft(library: dict[str, Any], draft_id: int) -> dict[str, Any] | None:
    """The draft with ``draft_id``, or None."""
    for draft in library.get("drafts", []):
        raw_id: Any = draft.get("id", -1)
        if int(raw_id) == int(draft_id):
            return draft
    return None


def create_draft(library: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    """Append a new, uniquely named draft and return it."""
    drafts = library.setdefault("drafts", [])
    counter = int(library.get("counter") or 0) + 1
    library["counter"] = counter
    base = str(name or "").strip() or f"Session {counter}"
    existing = {str(d.get("name")) for d in drafts}
    unique = base
    suffix = 2
    while unique in existing:
        unique = f"{base} ({suffix})"
        suffix += 1
    draft = {"id": counter, "name": unique, "files": []}
    drafts.append(draft)
    return draft


def rename_draft(library: dict[str, Any], draft_id: int, name: str) -> bool:
    """Rename a draft; False for an empty or already-used name (or unknown id)."""
    draft = find_draft(library, draft_id)
    clean = str(name or "").strip()
    if draft is None or not clean:
        return False
    if any(str(d.get("name")) == clean and d is not draft for d in library.get("drafts", [])):
        return False
    draft["name"] = clean
    return True


def delete_draft(library: dict[str, Any], draft_id: int) -> bool:
    """Remove a draft; False when the id is unknown."""
    drafts = library.get("drafts", [])
    for index, draft in enumerate(drafts):
        raw_id: Any = draft.get("id", -1)
        if int(raw_id) == int(draft_id):
            drafts.pop(index)
            return True
    return False


def add_files(library: dict[str, Any], draft_id: int, paths: list[Any]) -> int:
    """Append valid paths to a draft, de-duplicated; returns how many were added."""
    draft = find_draft(library, draft_id)
    if draft is None:
        return 0
    files = draft.setdefault("files", [])
    added = 0
    for path in paths:
        if not isinstance(path, str) or not path.strip() or path in files:
            continue
        if len(files) >= MAX_FILES_PER_DRAFT:
            break
        files.append(path)
        added += 1
    return added


def remove_file(library: dict[str, Any], draft_id: int, path: str) -> bool:
    """Remove one path from a draft; False when absent."""
    draft = find_draft(library, draft_id)
    if draft is None:
        return False
    files = draft.get("files", [])
    if path not in files:
        return False
    files.remove(path)
    return True


def move_file(library: dict[str, Any], draft_id: int, path: str, delta: int) -> bool:
    """Move one path up/down by ``delta`` positions; False at the ends."""
    draft = find_draft(library, draft_id)
    if draft is None:
        return False
    files = draft.get("files", [])
    if path not in files:
        return False
    index = files.index(path)
    target = index + int(delta)
    if target < 0 or target >= len(files):
        return False
    files.insert(target, files.pop(index))
    return True


def save_library(path: str, library: dict[str, Any]) -> bool:
    """Atomically write the library (temp + rename); False + no raise on failure."""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(library, handle, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def load_library(path: str | None = None) -> dict[str, Any]:
    """The sanitized library at ``path``; an empty one when missing/corrupt."""
    target = path or default_path()
    try:
        with open(target, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return empty_library()
    return sanitize_library(raw)
