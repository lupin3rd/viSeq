"""Palette + theme application for viseq (REFACTOR_LATEST.md commit 6/13).

Color math (DPG scale conversions, blends, palette derivation) and the
theme application that pushes a palette into the global theme, per-item
themes and the Settings widgets. Main-thread only (touches dpg); the
theme state objects live in ``state``.
"""

import copy
from typing import Any

import dearpygui.dearpygui as dpg

from viseqapp import state
from viseqapp.config import _recover_channel, load_config, save_config
from viseqapp.constants import (
    DEFAULT_PALETTE,
    DPG_COLOR_SCALE,
    LIGHT_PALETTE,
    THEME_PRESET_LABELS,
    THEME_PRIMARY_SLOTS,
)
from viseqapp.state import (
    _draw_color_bindings,
    _media_cell_cache,
    _text_color_bindings,
    _theme_color_bindings,
)

GLOBAL_THEME_COMPONENTS: list[tuple[int, str]] = [
    (dpg.mvThemeCol_WindowBg, "window_bg"),
    (dpg.mvThemeCol_ChildBg, "panel_bg"),
    (dpg.mvThemeCol_Border, "border"),
    (dpg.mvThemeCol_BorderShadow, "window_bg"),
    (dpg.mvThemeCol_Text, "text"),
    (dpg.mvThemeCol_TextDisabled, "text_dim"),
    (dpg.mvThemeCol_FrameBg, "panel_bg"),
    (dpg.mvThemeCol_FrameBgHovered, "panel_bg"),
    (dpg.mvThemeCol_FrameBgActive, "panel_bg"),
    (dpg.mvThemeCol_Button, "badge_bg"),
    (dpg.mvThemeCol_ButtonHovered, "badge_bg"),
    (dpg.mvThemeCol_ButtonActive, "badge_bg"),
    (dpg.mvThemeCol_Header, "badge_bg"),
    (dpg.mvThemeCol_HeaderHovered, "badge_bg"),
    (dpg.mvThemeCol_HeaderActive, "badge_bg"),
    (dpg.mvThemeCol_TitleBg, "window_bg"),
    (dpg.mvThemeCol_TitleBgActive, "window_bg"),
    (dpg.mvThemeCol_PopupBg, "window_bg"),
    (dpg.mvThemeCol_ScrollbarBg, "window_bg"),
    (dpg.mvThemeCol_ScrollbarGrab, "badge_bg"),
    (dpg.mvThemeCol_ScrollbarGrabHovered, "badge_bg"),
    (dpg.mvThemeCol_ScrollbarGrabActive, "badge_bg"),
    (dpg.mvThemeCol_TableHeaderBg, "panel_bg"),
    (dpg.mvThemeCol_TableBorderStrong, "border"),
    (dpg.mvThemeCol_TableBorderLight, "border"),
    (dpg.mvThemeCol_Separator, "border"),
    (dpg.mvThemeCol_CheckMark, "accent"),
    (dpg.mvThemeCol_SliderGrab, "accent"),
    (dpg.mvThemeCol_SliderGrabActive, "accent"),
    (dpg.mvThemeCol_InputTextCursor, "accent"),
    (dpg.mvThemeCol_TextSelectedBg, "accent"),
]


def dpg_color_value(rgb: list[float]) -> list[float]:
    """Convert normalized 0..1 RGB to DPG's color API scale (0..255).

    DPG 2.x parses color lists by dividing every channel by 255, so colors
    pushed programmatically (default_value / set_value) must arrive on the
    0..255 scale; user-picked colors arrive via callbacks already 0..1.
    """
    return [round(c * DPG_COLOR_SCALE, 2) for c in rgb]


def dpg_color_rgba(rgb: list[float]) -> list[float]:
    """DPG-scale RGB plus an opaque alpha (color_button needs 4 components)."""
    return [*dpg_color_value(rgb), DPG_COLOR_SCALE]


def palette_rgba(color: list[int]) -> list[int]:
    """Palette RGB (0..255) plus an opaque alpha, the DPG color shape for themes/widgets."""
    return [color[0], color[1], color[2], 255]


def _blend(a: list[int], b: list[int], t: float) -> list[int]:
    """Per-channel linear blend: t of a over (1 - t) of b."""
    return [round(a[i] * t + b[i] * (1.0 - t)) for i in range(3)]


def derive_palette(primaries: dict[str, list[int]]) -> dict[str, list[int]]:
    """Complete a 5-slot primary palette with the derived slots (deterministic, no mutation)."""
    pbg = primaries["panel_bg"]
    border = primaries["border"]
    text = primaries["text"]
    accent = primaries["accent"]
    return {
        **primaries,
        "border_active": accent,
        "text_dim": _blend(text, [128, 128, 128], 0.55),
        "text_bright": _blend(text, [255, 255, 255], 0.35),
        "accent_bg": _blend(accent, pbg, 0.45),
        "badge_bg": _blend(border, pbg, 0.55),
        "warning": [255, 220, 80],
        "play_bg": _blend(border, [255, 255, 255], 0.25),
        "play_on_bg": _blend(accent, [255, 255, 255], 0.35),
        "spectrum": _blend(accent, [255, 255, 255], 0.25),
    }


def theme_color(component: int, slot: str, category: int = dpg.mvThemeCat_Core) -> None:
    """Add a palette-driven theme color; the (tag, slot) pair is recorded for re-theming."""
    item_tag = dpg.add_theme_color(
        component, palette_rgba(state.active_palette[slot]), category=category
    )
    _theme_color_bindings[item_tag] = slot


def themed_text(*args: Any, slot: str, **kwargs: Any) -> Any:
    """Add text colored from the active palette; the (tag, slot) pair is recorded for re-theming."""
    kwargs.pop("color", None)
    tag = dpg.add_text(*args, color=palette_rgba(state.active_palette[slot]), **kwargs)
    _text_color_bindings[tag] = slot
    return tag


def _set_media_cell(tag: str, value: str | float) -> None:
    """set_value for a media-grid cell, skipping writes whose displayed value is unchanged.

    Text cells pass strings; the tile alpha slider passes a float. The cache
    stores whichever type the cell uses, so an identical push writes nothing.
    """
    if _media_cell_cache.get(tag) != value and dpg.does_item_exist(tag):
        dpg.set_value(tag, value)
    _media_cell_cache[tag] = value


def themed_draw_rectangle(*args: Any, slot: str, color_kwarg: str = "fill", **kwargs: Any) -> Any:
    """draw_rectangle with a palette-driven color; the (tag, slot) pair is recorded."""
    kwargs[color_kwarg] = palette_rgba(state.active_palette[slot])
    tag = dpg.draw_rectangle(*args, **kwargs)
    _draw_color_bindings[tag] = (slot, color_kwarg)
    return tag


def ensure_global_theme() -> None:
    """Build (once) and bind the global chrome theme from the active palette."""
    if state.theme_global is None:
        with dpg.theme() as t, dpg.theme_component(dpg.mvAll):
            for component, slot in GLOBAL_THEME_COMPONENTS:
                theme_color(component, slot)
        state.theme_global = t
    dpg.bind_theme(state.theme_global)


def apply_palette(palette: dict[str, list[int]]) -> None:
    """Push every recorded color binding; runs on the main thread only (boot + user changes)."""
    for item_tag, slot in _theme_color_bindings.items():
        dpg.set_value(item_tag, palette_rgba(palette[slot]))
    for item_tag, slot in _text_color_bindings.items():
        if dpg.does_item_exist(item_tag):
            dpg.configure_item(item_tag, color=palette_rgba(palette[slot]))
    for item_tag, (slot, kwarg) in _draw_color_bindings.items():
        if dpg.does_item_exist(item_tag):
            dpg.configure_item(item_tag, **{kwarg: palette_rgba(palette[slot])})
    state.active_palette = palette


def _to_palette_color(raw: Any) -> list[int]:
    """Normalized 0..1 RGBA (color_edit callback payload) -> palette RGB on the 0..255 scale."""
    return [round(float(c) * DPG_COLOR_SCALE) for c in raw[:3]]


def _read_primary_colors_from_edits() -> dict[str, list[int]]:
    """Read the five Settings color edits as palette RGB.

    DPG 2.3.1 color_edit get_value returns the stored 0..255 scale (verified headless),
    unlike the callback payload which arrives normalized 0..1 (see _to_palette_color).
    """
    colors: dict[str, list[int]] = {}
    for slot in THEME_PRIMARY_SLOTS:
        raw = dpg.get_value(f"theme_color_{slot}")
        colors[slot] = [_recover_channel(c) for c in raw[:3]]
    return colors


def _preset_key(label: str) -> str:
    """Combo label -> config key ('Custom' -> 'custom')."""
    for key, lbl in THEME_PRESET_LABELS.items():
        if lbl == label:
            return key
    return "scuro"


def _sync_theme_widgets(preset: str, palette: dict[str, list[int]]) -> None:
    """Push a palette into the Settings theme widgets (combo + five color edits)."""
    if dpg.does_item_exist("theme_preset"):
        dpg.set_value("theme_preset", THEME_PRESET_LABELS[preset])
    for slot in THEME_PRIMARY_SLOTS:
        tag = f"theme_color_{slot}"
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, palette_rgba(palette[slot]))


def _apply_theme_config(theme: dict[str, Any]) -> None:
    """Make a stored theme (preset + colors) the live look: palette, global theme, widgets."""
    preset = str(theme.get("preset", "scuro"))
    palette = theme["colors"]
    state.active_palette = palette
    if preset == "scuro":
        dpg.bind_theme(0)  # unbind: DPG's dark defaults reproduce the legacy look
    else:
        ensure_global_theme()
    apply_palette(palette)
    _sync_theme_widgets(preset, palette)


def on_theme_preset(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Settings Tema combo: load a preset palette (or re-derive custom), apply live, persist."""
    label = str(app_data)
    if label == THEME_PRESET_LABELS["custom"]:
        palette = derive_palette(_read_primary_colors_from_edits())
    elif label == THEME_PRESET_LABELS["chiaro"]:
        palette = copy.deepcopy(LIGHT_PALETTE)
    else:
        palette = copy.deepcopy(DEFAULT_PALETTE)
    cfg = load_config()
    cfg["theme"] = {"preset": _preset_key(label), "colors": palette}
    _apply_theme_config(cfg["theme"])
    save_config(cfg)


def on_theme_color(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Settings color edit: derive the palette from the five edits, apply live, persist."""
    primaries = _read_primary_colors_from_edits()
    primaries[user_data] = _to_palette_color(app_data)  # sender payload wins over get_value
    palette = derive_palette(primaries)
    cfg = load_config()
    cfg["theme"] = {"preset": "custom", "colors": palette}
    _apply_theme_config(cfg["theme"])
    save_config(cfg)
