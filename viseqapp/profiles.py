"""Controller profile system (REFACTOR_LATEST.md commit 4/13).

External JSON profiles in CONTROLLERS_DIR describe MIDI controller models
(port-name matching, grid geometry + note formula, LED color palette,
setup SysEx); dropping a file adds a model with no code changes. Pure
module: no dpg, no app state.
"""

import copy
import json
import os
from typing import Any

from viseqapp.queues import log_error

CONTROLLERS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "controllers"
)

# Built-in profiles: the three Launchpad families (e09s03 behavior reproduced
# exactly). They double as the fallback when the controllers/ folder is missing
# and as the reference for the matcher tests.
DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "launchpad_mk1": {
        "id": "launchpad_mk1",
        "name": "Novation Launchpad MK1 / S / Mini",
        "match": ["launchpad"],
        "priority": 10,
        "protocol": "mk1",
        "grid": {"rows": 8, "cols": 8, "note": "row*16+col"},
        # Official Programmers Reference (normal use, Flags=12): 16*Green+Red+12.
        "colors": {"off": 12, "red": 15, "amber": 63, "white": 62, "green": 60},
        "setup_sysex": None,
        "features": {"grid": True, "leds": True, "clock": False},
    },
    "launchpad_mk2": {
        "id": "launchpad_mk2",
        "name": "Novation Launchpad MK2 / Mini MK2 / Pro",
        "match": ["mk2", "pro"],
        "priority": 20,
        "protocol": "note",
        "grid": {"rows": 8, "cols": 8, "note": "row*10+col"},
        # 128-color palette (manual): 0 off, 3 white, 5 red, 12 amber, 60 green.
        "colors": {"off": 0, "red": 5, "amber": 12, "white": 3, "green": 60},
        "setup_sysex": None,
        "features": {"grid": True, "leds": True, "clock": False},
    },
    "launchpad_mk3": {
        "id": "launchpad_mk3",
        "name": "Novation Launchpad X / Mini MK3 / Pro MK3",
        "match": ["mk3", "launchpad x"],
        "priority": 30,
        "protocol": "programmer",
        "grid": {"rows": 8, "cols": 8, "note": "row*10+col"},
        # Programmer mode: same 128-color palette as the MK2 family.
        "colors": {"off": 0, "red": 5, "amber": 12, "white": 3, "green": 60},
        "setup_sysex": [0x00, 0x20, 0x29, 0x02, 0x0C, 0x03, 0x01],
        "features": {"grid": True, "leds": True, "clock": False},
    },
}


PROFILE_PROTOCOLS: tuple[str, ...] = ("mk1", "note", "programmer")


PROFILE_SEMANTIC_COLORS: tuple[str, ...] = ("off", "red", "amber", "white", "green")


_DEFAULT_PROFILE_COLORS: dict[str, int] = {"off": 0, "red": 5, "amber": 12, "white": 3, "green": 60}


_DEFAULT_GRID_NOTE_FORMULA = "row*16+col"


def _sanitize_profile(raw: Any) -> dict[str, Any]:
    """Coerce a loaded profile file into a valid profile dict (e14s01)."""
    if not isinstance(raw, dict):
        raw = {}
    profile: dict[str, Any] = {
        "id": str(raw.get("id", "controller")).strip() or "controller",
        "name": str(raw.get("name", "MIDI Controller")),
        "match": [],
        "priority": _to_int(raw.get("priority"), 0),
        "protocol": str(raw.get("protocol", "note")),
        "grid": {"rows": 8, "cols": 8, "note": _DEFAULT_GRID_NOTE_FORMULA},
        "colors": dict(_DEFAULT_PROFILE_COLORS),
        "setup_sysex": None,
        "features": {"grid": False, "leds": False, "clock": False},
    }
    match = raw.get("match")
    if isinstance(match, list):
        profile["match"] = [str(m).lower().strip() for m in match if str(m).strip()]
    if profile["protocol"] not in PROFILE_PROTOCOLS:
        profile["protocol"] = "note"
    grid = raw.get("grid")
    if isinstance(grid, dict):
        rows = _to_int(grid.get("rows"), 8)
        cols = _to_int(grid.get("cols"), 8)
        note = str(grid.get("note", _DEFAULT_GRID_NOTE_FORMULA))
        if _formula_is_valid(note):
            profile["grid"] = {"rows": max(1, rows), "cols": max(1, cols), "note": note}
    colors = raw.get("colors")
    if isinstance(colors, dict):
        for slot in PROFILE_SEMANTIC_COLORS:
            value = _to_int(colors.get(slot), _DEFAULT_PROFILE_COLORS[slot])
            profile["colors"][slot] = value
    sysex = raw.get("setup_sysex")
    if isinstance(sysex, list) and all(isinstance(b, int) and 0 <= b <= 127 for b in sysex):
        profile["setup_sysex"] = [int(b) for b in sysex]
    features = raw.get("features")
    if isinstance(features, dict):
        for flag in profile["features"]:
            profile["features"][flag] = bool(features.get(flag, False))
    return profile


def _to_int(value: Any, default: int) -> int:
    """Coerce a stored value to int, falling back on garbage (e14s01)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _formula_is_valid(formula: str) -> bool:
    """A grid-note formula must evaluate to a number for row/col 0..7 (e14s01)."""
    try:
        value = eval(formula, {"__builtins__": {}}, {"row": 7, "col": 7, "int": int})
        return isinstance(value, (int, float))
    except Exception:
        return False


def _grid_note(profile: dict[str, Any], row: int, col: int) -> int:
    """Grid MIDI note for pad (row, col) under a profile's note formula (e14s01)."""
    formula = profile["grid"]["note"]
    return int(eval(formula, {"__builtins__": {}}, {"row": row, "col": col, "int": int}))


def load_controller_profiles() -> dict[str, dict[str, Any]]:
    """Load every controllers/*.json profile over the built-ins; corrupt files skip (e14s01)."""
    profiles = {pid: copy.deepcopy(profile) for pid, profile in DEFAULT_PROFILES.items()}
    try:
        names = sorted(os.listdir(CONTROLLERS_DIR))
    except OSError:
        return profiles
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(CONTROLLERS_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                profile = _sanitize_profile(json.load(f))
            profiles[profile["id"]] = profile
        except (OSError, ValueError) as e:
            log_error("Profiles", f"skip {name}: {e}")
    return profiles


def match_controller_profile(
    port_name: str, profiles: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Highest-priority profile whose any match substring is in the port name (e14s01)."""
    name = (port_name or "").lower()
    best: dict[str, Any] | None = None
    for profile in profiles.values():
        if any(m in name for m in profile["match"]) and (
            best is None or profile["priority"] > best["priority"]
        ):
            best = profile
    return best
