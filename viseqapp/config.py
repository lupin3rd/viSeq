"""User-config I/O for viseq (REFACTOR_LATEST.md commit 5/13).

load_config/save_config read and write the JSON config under the per-user XDG
config dir (CONFIG_PATH is owned here so the test suite can point it at a temp
file); missing/corrupt files fall back to defaults. A legacy app-directory
config migrates once to the XDG path (e21s01). No dpg, no app state.
"""

import copy
import json
import os
from typing import Any

from viseqapp.constants import DEFAULT_CONFIG, DEFAULT_PALETTE, DPG_COLOR_SCALE, PALETTE_SLOTS
from viseqapp.queues import log_error


def user_config_dir() -> str:
    """Per-user viseq config dir: $XDG_CONFIG_HOME/viseq, else ~/.config/viseq (e21s01)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "viseq")


CONFIG_PATH = os.path.join(user_config_dir(), "viseq_config.json")

# The pre-e21 location (next to the app); the source of a one-time migration.
LEGACY_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viseq_config.json"
)


def _migrate_legacy_config() -> None:
    """One-time move of a legacy app-directory config to the XDG path (e21s01).

    Copy first (atomic tmp + replace), remove the legacy file only after the new
    copy is in place, so a failed migration never loses user settings. Tests
    point both paths at temp files; the harness redirects LEGACY_CONFIG_PATH so
    the suite never touches the developer's live repo-root config.
    """
    if os.path.exists(CONFIG_PATH) or not os.path.exists(LEGACY_CONFIG_PATH):
        return
    try:
        with open(LEGACY_CONFIG_PATH, encoding="utf-8") as f:
            data = f.read()
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = f"{CONFIG_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, CONFIG_PATH)
        os.remove(LEGACY_CONFIG_PATH)
    except OSError:
        pass  # keep the legacy file; load_config falls back to defaults


def _recover_channel(c: float) -> int:
    """Coerce one color channel to the valid 0..255 range.

    Channels above 255 are treated as legacy double-scaled values (the pre-fix color_edit
    bug multiplied the 0..255 value by 255, e.g. 24 -> 6120) and divided back before
    clamping, so a broken config self-heals to its intended color instead of pure white.
    """
    v = round(float(c))
    if v > 255:
        v = round(v / DPG_COLOR_SCALE)
    return min(255, max(0, v))


def _sanitize_palette(colors: Any) -> dict[str, list[int]]:
    """Coerce a stored palette to valid 0..255 RGB slots.

    Heals configs written by the pre-fix color_edit scale bug (e06 regression): channels
    like 6120 (= 24 * 255) are divided back to 24 (see _recover_channel), so the intended
    color is restored instead of blowing out to pure white after DPG's /255 normalization.
    """
    clean = {slot: list(DEFAULT_PALETTE[slot]) for slot in PALETTE_SLOTS}
    if isinstance(colors, dict):
        for slot in PALETTE_SLOTS:
            raw = colors.get(slot)
            if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                clean[slot] = [_recover_channel(c) for c in raw[:3]]
    return clean


def load_config() -> dict[str, Any]:
    """Read the user config; missing/corrupt file falls back to defaults (never crashes)."""
    _migrate_legacy_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(loaded, dict):
        return copy.deepcopy(DEFAULT_CONFIG)
    merged = copy.deepcopy(DEFAULT_CONFIG)

    def _merge(base: Any, over: Any) -> Any:
        if isinstance(base, dict) and isinstance(over, dict):
            return {k: _merge(base.get(k), v) for k, v in over.items()}
        return over

    for key in merged:
        if key in loaded:
            merged[key] = _merge(merged[key], loaded[key])
    merged["theme"]["colors"] = _sanitize_palette(merged["theme"].get("colors"))
    # e11s02: drop unknown top-level keys (the legacy "layout" block) so a stale
    # config file self-cleans on the next save instead of carrying dead state.
    return {key: merged[key] for key in DEFAULT_CONFIG}


def save_config(cfg: dict[str, Any]) -> None:
    """Atomically write the user config; failures are logged, never fatal."""
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = f"{CONFIG_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        log_error("Config", f"cannot write {CONFIG_PATH}: {e}")
