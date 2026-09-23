"""Controller profile system (REFACTOR_LATEST.md commit 4/13).

External JSON profiles in CONTROLLERS_DIR (bundled, read-only — AppImage-safe)
and in USER_CONTROLLERS_DIR (per-user, $XDG_CONFIG_HOME/viseq/controllers)
describe MIDI controller models (port-name matching, grid geometry + note
formula, LED color palette, setup SysEx); dropping a file adds a model with no
code changes. User-dir profiles override bundled ones by id (e21s01). Pure
module: no dpg, no app state.
"""

import copy
import json
import os
from typing import Any

from viseqapp.config import user_config_dir
from viseqapp.constants import (
    MIDI_LED_BEHAVIOR_PRESS,
    MIDI_LED_BEHAVIORS,
    MIDI_LED_MIRROR_MODES,
    MIDI_MODE_LED_MAX_VALUE,
    MIDI_MODE_LED_MIN_VALUE,
    MIDI_MODE_LED_OFF_DEFAULT,
    MIDI_MODE_LED_ON_DEFAULT,
    MIDI_STEP_MAX_PER_SECOND,
    MIDI_STEP_MAX_PER_SECOND_MAX,
    MIDI_STEP_SIZE,
    MIDI_STEP_SIZE_MAX,
)
from viseqapp.queues import log_error

CONTROLLERS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "controllers"
)

# User-dropped profiles live per-user so they survive app updates (e21s01).
USER_CONTROLLERS_DIR = os.path.join(user_config_dir(), "controllers")

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


# e53s03: bound for a profile's `feedback.relative_cc` list (a control-number hint).
MIDI_FEEDBACK_MAX_RELATIVE_CC = 64


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
        "feedback": None,
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
    profile["feedback"] = _sanitize_feedback(raw.get("feedback"))
    return profile


def _sanitize_feedback(raw: Any) -> dict[str, Any] | None:
    """Coerce a profile's e53 `feedback` block; None when absent or unusable (e53s03).

    `led_mirror` derives a Binding's LED from its input control (same kind,
    channel and number); on/off values are clamped to the MIDI range and
    `relative_cc` is the e54 rotation-encoding hint (deduped, bounded, sorted).
    """
    if not isinstance(raw, dict):
        return None
    mirror = str(raw.get("led_mirror") or "").strip().lower()
    behavior = str(raw.get("default_behavior") or "").strip().lower()
    relative: list[int] = []
    raw_relative = raw.get("relative_cc")
    if isinstance(raw_relative, list):
        for item in raw_relative[:MIDI_FEEDBACK_MAX_RELATIVE_CC]:
            value = _to_int(item, -1)
            if value in range(0, MIDI_MODE_LED_MAX_VALUE + 1) and value not in relative:
                relative.append(value)
    return {
        "led_mirror": mirror if mirror in MIDI_LED_MIRROR_MODES else None,
        "on_value": _clamp_midi(raw.get("on_value"), MIDI_MODE_LED_ON_DEFAULT),
        "off_value": _clamp_midi(raw.get("off_value"), MIDI_MODE_LED_OFF_DEFAULT),
        "default_behavior": behavior if behavior in MIDI_LED_BEHAVIORS else MIDI_LED_BEHAVIOR_PRESS,
        "relative_cc": sorted(relative),
        "step_size": _clamp_step_size(raw.get("step_size")),
        "max_steps_per_second": _clamp_step_rate(raw.get("max_steps_per_second")),
    }


def _clamp_step_rate(raw: Any) -> int:
    """A profile `feedback.max_steps_per_second` (e54s01); a non-positive value falls back."""
    value = _to_int(raw, MIDI_STEP_MAX_PER_SECOND)
    if value < 1:
        return MIDI_STEP_MAX_PER_SECOND
    return min(value, MIDI_STEP_MAX_PER_SECOND_MAX)


def _clamp_step_size(raw: Any) -> int:
    """A profile `feedback.step_size`: CC units per detent (e54s01).

    A missing/non-int or non-positive value falls back to the default; a value
    above the maximum is clamped down, so a too-slow typo never disables stepping.
    """
    value = _to_int(raw, MIDI_STEP_SIZE)
    if value < 1:
        return MIDI_STEP_SIZE
    return min(value, MIDI_STEP_SIZE_MAX)


def _clamp_midi(raw: Any, default: int) -> int:
    """Coerce a stored MIDI value into [0, 127], falling back on the default (e53s03)."""
    value = _to_int(raw, default)
    return max(MIDI_MODE_LED_MIN_VALUE, min(MIDI_MODE_LED_MAX_VALUE, value))


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
    """Load every profile over the built-ins; corrupt files skip (e14s01).

    Bundled dir loads first, then user-dropped profiles override by id (e21s01);
    missing dirs are tolerated.
    """
    profiles = {pid: copy.deepcopy(profile) for pid, profile in DEFAULT_PROFILES.items()}
    profiles.update(_load_profiles_from(CONTROLLERS_DIR))
    profiles.update(_load_profiles_from(USER_CONTROLLERS_DIR))
    return profiles


def _load_profiles_from(directory: str) -> dict[str, dict[str, Any]]:
    """Load sanitized *.json profiles from one directory; missing dirs/corrupt skip (e21s01)."""
    loaded: dict[str, dict[str, Any]] = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return loaded
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as f:
                profile = _sanitize_profile(json.load(f))
            loaded[profile["id"]] = profile
        except (OSError, ValueError) as e:
            log_error("Profiles", f"skip {name}: {e}")
    return loaded


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
