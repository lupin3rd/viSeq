"""User-config I/O for viseq (REFACTOR_LATEST.md commit 5/13).

load_config/save_config read and write the JSON config next to the
composition root (CONFIG_PATH is owned here so the test suite can point it
at a temp file); missing/corrupt files fall back to defaults. No dpg, no
app state.
"""

import copy
import json
import os
from typing import Any

from viseqapp.constants import DEFAULT_CONFIG, DEFAULT_PALETTE, DPG_COLOR_SCALE, PALETTE_SLOTS
from viseqapp.queues import log_error

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viseq_config.json"
)


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
        tmp = f"{CONFIG_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        log_error("Config", f"cannot write {CONFIG_PATH}: {e}")
