import contextlib
import copy
import io
import json
import math
import os
import queue
import random
import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any

import dearpygui.dearpygui as dpg
import essentia
import essentia.standard as es
import numpy as np
import sounddevice as sd
from PIL import Image
from pythonosc import dispatcher, osc_server, udp_client

# --- HARD CAPS ON NETWORK-FED DATA (viOSC replies) ---
# Bound memory use and block PIL decompression bombs (audit MED-6).
Image.MAX_IMAGE_PIXELS = 25_000_000  # PIL's hard ceiling (~25 MP)
MAX_THUMBNAIL_PIXELS = 3_000_000  # explicit cap; real thumbs are ~58k px
MAX_THUMBNAIL_BLOB_BYTES = 8 * 1024 * 1024  # per-blob cap
MAX_STATE_JSON_BYTES = 1 * 1024 * 1024  # per-replydata cap

# --- BEHAVIORAL CONSTANTS (audit L-6) ---
THUMB_REQUEST_INTERVAL = 3.0  # min seconds between thumbnail requests per source
LOG_HISTORY_LIMIT = 25  # max entries kept in the OSC log window
MONITOR_OFFSET = (280, 260)  # grid spacing between monitor player windows
DPG_COLOR_SCALE = 255.0  # DPG ToColor divides color inputs by 255 -> its color API is 0..255

# Main-loop render cadence (perf e07 P1): full rate while something animates, throttled
# while idle (input stays responsive at ~30 fps; SPIKE-perf measured the idle render as
# the biggest fixed cost).
FRAME_SLEEP_ANIMATED = 0.016  # ~60 fps while sequencer/spectrum/monitor video animate
FRAME_SLEEP_IDLE = 0.033  # ~30 fps at rest

# Step-cell layout: a centered square leaves the checkbox/type row on top (audit L-6)
STEP_CELL_SIZE = 90  # px side of each sequencer step cell
STEP_COLOR_SQUARE_SIZE = 40  # px side of the centered color square inside a step cell
# ImGui WindowPadding.x inside child windows (default style; app themes don't override)
STEP_CELL_CONTENT_PADDING = 8
# Measured on DPG 2.3.1: this indent centers the swatch in the 90px cell (padding included)
STEP_COLOR_SQUARE_INDENT = (
    STEP_CELL_SIZE - 2 * STEP_CELL_CONTENT_PADDING - STEP_COLOR_SQUARE_SIZE
) // 2

# Clip-slot layout: a bare centered assign button, no table frame around it (audit L-6).
# The borderless slot has no WindowPadding (content_region_avail == full size, measured on
# 2.3.1), so horizontal centering is pure (width - button_width) / 2. Vertically the button's
# frame rect sits SLOT_BUTTON_FRAME_INSET px below its layout box (measured), hence the -inset.
SLOT_WIDTH = 135  # px width of the clip slot column
SLOT_HEIGHT = 90  # px height of a sequencer row
SLOT_BUTTON_WIDTH = 110  # px width of the assign button/thumbnail
SLOT_BUTTON_HEIGHT = 70  # px height of the assign button/thumbnail
SLOT_BUTTON_FRAME_INSET = 4  # px: ImGui button rect offset below its widget box (2.3.1)
SLOT_BUTTON_INDENT = (SLOT_WIDTH - SLOT_BUTTON_WIDTH) // 2
SLOT_BUTTON_TOP_SPACER = (SLOT_HEIGHT - SLOT_BUTTON_HEIGHT) // 2 - SLOT_BUTTON_FRAME_INSET

# Mediagrid tile: index badge box + compact layout (audit L-6)
MEDIA_BADGE_W = 28  # px width of the media-index badge button
MEDIA_BADGE_H = 20  # px height of the media-index badge button
MEDIA_TILE_H = 146  # px height of a media tile (title + photo + badge row)
# e10s06: the tile title fits at most two wrapped lines, truncated with an ellipsis
MEDIA_TITLE_WRAP = 125  # px wrap width of the media tile title
MEDIA_TITLE_MAX_LINES = 2
MEDIA_TITLE_ELLIPSIS = "..."  # ASCII dots: ProggyClean (default font) has no U+2026 glyph
MEDIA_TITLE_CHAR_PX = 7  # default-font estimate (ProggyClean 13 px) used before the atlas is built

# Monitor player: compact graphical readout (e07)
MONITOR_THUMB_W = 115  # thumbnail width, same as the Mediagrid/sequencer
MONITOR_THUMB_H = 65  # thumbnail height, same as the Mediagrid/sequencer
MONITOR_DISC_SIZE = 64  # px side of the turntable disc
MONITOR_ALPHA_W = 10  # px width of the vertical alpha bar
MONITOR_SEEK_W = 250  # px width of the horizontal seek bar
MONITOR_DISC_R = 26.0  # radius of the rotating turntable arm
MONITOR_DISC_RPM = 33.0  # disc rotations per minute at speed 1.0 (vinyl standard)
MONITOR_SPEED_TEXT_SIZE = 12  # px font size of the speed label inside the disc
DEFAULT_MONITOR_PROPS = ["alpha", "seek", "speed"]  # requested when a monitor starts

# --- OSC CONFIGURATION ---
# viseq talks exclusively to viOSC: /vimix/* messages are forwarded by viOSC
# to Vimix (port 7000), replies come back on viOSC's output port 6667.
VIOSC_IP = "127.0.0.1"
VIOSC_PORT = 6666
VIOSC_LISTEN_PORT = 6667  # the port viOSC sends replies to; viseq's own server listens here
osc_client = udp_client.SimpleUDPClient(VIOSC_IP, VIOSC_PORT)

viosc_client: Any = None
local_osc_server: Any = None
local_server_thread: threading.Thread | None = None
is_server_running: bool = False

# --- USER CONFIG + THEMING (e06) ---
# Single JSON file next to viseq.py stores the window layout and the theme. The storage
# mechanism was delegated to the agent by the user ("puoi decidere tu cosa utilizzare").
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(CONFIG_DIR, "viseq_config.json")

# viseq application version — single source of truth (matches specs/release-plan.yaml, e08s02).
APP_VERSION: str = "1.1.0"

# Author's GitHub profile, shown as a link in the About window (e08s01, user request).
GITHUB_URL: str = "https://github.com/lupin3rd"

# Palette slots drive every chrome color: the global theme, the per-item themes, explicit
# text colors and the main draw items. The five primaries are user-editable in the Settings
# window; the rest derive from them (derive_palette). The "Dark" preset reproduces the
# legacy look exactly, so first launch is visually identical (SCOPE_LATEST criterion).
PALETTE_SLOTS: list[str] = [
    "window_bg",
    "panel_bg",
    "border",
    "border_active",
    "text",
    "text_dim",
    "text_bright",
    "accent",
    "accent_bg",
    "badge_bg",
    "warning",
    "play_bg",
    "play_on_bg",
    "spectrum",
]
THEME_PRIMARY_SLOTS: list[str] = ["window_bg", "panel_bg", "text", "border", "accent"]
THEME_PRIMARY_LABELS: dict[str, str] = {
    "window_bg": "Background",
    "panel_bg": "Panels",
    "text": "Text",
    "border": "Lines",
    "accent": "Accent",
}
THEME_PRESET_LABELS: dict[str, str] = {
    "scuro": "Dark",
    "chiaro": "Light",
    "custom": "Custom",
}

DEFAULT_PALETTE: dict[str, list[int]] = {
    # Scuro: the exact legacy look (e06s02 acceptance criterion 1)
    "window_bg": [24, 24, 24],
    "panel_bg": [40, 40, 40],
    "border": [80, 80, 80],
    "border_active": [50, 255, 50],
    "text": [200, 200, 200],
    "text_dim": [150, 150, 150],
    "text_bright": [255, 255, 255],
    "accent": [50, 255, 50],
    "accent_bg": [30, 80, 30],
    "badge_bg": [45, 55, 75],
    "warning": [255, 220, 80],
    "play_bg": [80, 80, 80],
    "play_on_bg": [80, 220, 80],
    "spectrum": [80, 255, 120],
}

LIGHT_PALETTE: dict[str, list[int]] = {
    # Chiaro: light background, dark text (the user's "sfondo scuro" counterpart)
    "window_bg": [235, 235, 235],
    "panel_bg": [248, 248, 248],
    "border": [130, 130, 130],
    "border_active": [20, 140, 20],
    "text": [30, 30, 30],
    "text_dim": [105, 105, 105],
    "text_bright": [25, 25, 25],
    "accent": [20, 140, 20],
    "accent_bg": [205, 235, 205],
    "badge_bg": [210, 216, 226],
    "warning": [190, 150, 20],
    "play_bg": [185, 185, 185],
    "play_on_bg": [140, 210, 140],
    "spectrum": [30, 150, 60],
}

# Fixed windows tracked by the layout save/restore; monitor-player windows are added at
# snapshot time (they exist only while the app runs, e06s01).
LAYOUT_WINDOW_TAGS: list[str] = [
    "sequencer_window",
    "audio_window",
    "settings_window",
    "vimix_media_window",
    "logs_window",
]

# The Settings window is a config panel, not workspace: a saved layout must never re-open it
# at boot (otherwise every start would pop it up, since it is open while clicking "Salva").
# snapshot records it as closed and apply always hides it (e06s01 user revision).
LAYOUT_ALWAYS_HIDDEN_TAGS: tuple[str, ...] = ("settings_window",)

# e08: About window (Help menubar). The ASCII logo is the user-supplied art, kept verbatim
# (8 lines x 53 chars, trailing spaces included) as a raw string so the backslashes survive.
# The window is a transient dialog: it stays out of LAYOUT_WINDOW_TAGS, so a saved layout
# never re-opens it at boot and the layout snapshot never records it.
HELP_ASCII_LOGO: str = r""" ___      ___ ___  ________  _______   ________      
|\  \    /  /|\  \|\   ____\|\  ___ \ |\   __  \     
\ \  \  /  / | \  \ \  \___|\ \   __/|\ \  \|\  \    
 \ \  \/  / / \ \  \ \_____  \ \  \_|/_\ \  \\\  \   
  \ \    / /   \ \  \|____|\  \ \  \_|\ \ \  \\\  \  
   \ \__/ /     \ \__\____\_\  \ \_______\ \_____  \ 
    \|__|/       \|__|\_________\|_______|\|___| \__|
                     \|_________|               \|__|"""

# e08: About-window geometry (measured: logo is 53 chars wide; DejaVu Sans Mono 13px is
# ~7.8px/char, so ~414px of art in a 540px window leaves ~63px of side padding).
HELP_WINDOW_WIDTH = 540
HELP_WINDOW_HEIGHT = 300  # logo + title + version/license/author lines + GitHub button
HELP_LOGO_INDENT = (HELP_WINDOW_WIDTH - int(53 * 7.8)) // 2

# e08: monospace font for the ASCII logo; the first existing path wins, None falls back to
# the default proportional font (cosmetic only — the logo then drifts off alignment).
_HELP_MONO_FONT_PATHS: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
)
_help_mono_font: Any = None

# --- e09: MIDI control engine (single mido stack; notes + CCs, user-configurable bindings) ---
MIDI_ACTION_SEQ_TOGGLE = "seq_toggle"
MIDI_ACTION_TRANSPORT_PLAY = "transport_play"
MIDI_ACTION_TRANSPORT_RESYNC = "transport_resync"
MIDI_ACTION_TRANSPORT_TAP = "transport_tap"
MIDI_ACTION_NUDGE_BACK = "nudge_back"
MIDI_ACTION_NUDGE_FORWARD = "nudge_forward"
MIDI_ACTION_BEAT_SOURCE = "beat_source"
MIDI_ACTION_TRACK_ASSIGN = "track_assign"

DEFAULT_CONFIG: dict[str, Any] = {
    "layout": {"restore_on_boot": True, "windows": []},
    "theme": {"preset": "scuro", "colors": copy.deepcopy(DEFAULT_PALETTE)},
    "midi": {"enabled": False, "input_port": None, "bindings": []},
}

# MIDI control runtime mirrors of cfg["midi"] (e09). The worker thread reads these; the
# main thread writes them. Bindings: [{device, channel, type("note"/"cc"), number,
# action, params}]. Learn flow state (e09s02): pending = (action, params) captured by a
# learnable widget click, awaiting the next incoming MIDI message.
midi_enabled: bool = False
midi_input_port: str | None = None
midi_bindings: list[dict[str, Any]] = []
midi_auto_bindings: list[dict[str, Any]] = []  # e09s03: Launchpad grid bindings (not persisted)
midi_learn_mode: bool = False
midi_learn_pending: tuple[str, dict[str, Any]] | None = None

# --- e09s03: Novation Launchpad adapter constants ---
# Three protocol classes (user's device: novlpd01 = original Launchpad MK1):
#  - MK1 (plain "Launchpad"/"Launchpad S"): grid notes 16*row+col (0-119 grid; official
#    Programmers Reference), palette velocity = 16*Green + Red + 12 (normal use): 12 off,
#    15 red full, 63 amber full, 62 yellow full, 60 green full — NO SysEx.
#  - MK2 family (MK2/Mini MK2/Pro): grid notes row*10+col, 128-color palette (manual:
#    0 off, 3 white, 5 red, 12 amber, 60 green) — native note mode.
#  - MK3 family (X/Mini MK3/Pro MK3): programmer mode first (SysEx setup), then the same
#    note grid as MK2. Payload WITHOUT the F0/F7 framing bytes — mido adds them when
#    sending a sysex message (verified: data must be 0-127; mido.bytes() yields
#    F0 00 20 29 02 0C 03 01 F7).
LAUNCHPAD_MK1 = "mk1"
LAUNCHPAD_NOTE_MODE = "note"
LAUNCHPAD_PROGRAMMER_MODE = "programmer"
LAUNCHPAD_PROGRAMMER_SYSEX: list[int] = [0x00, 0x20, 0x29, 0x02, 0x0C, 0x03, 0x01]
LAUNCHPAD_GRID_ROWS = 8
LAUNCHPAD_GRID_COLS = 8
# Semantic LED colors (mirror/flash call sites) mapped per protocol in _launchpad_velocity.
LAUNCHPAD_LED_OFF = "off"
LAUNCHPAD_LED_WHITE = "white"
LAUNCHPAD_LED_RED = "red"
LAUNCHPAD_LED_AMBER = "amber"
LAUNCHPAD_LED_GREEN = "green"
LAUNCHPAD_FLASH_SECONDS = 0.12  # beat flash pulse duration (timer restores the head color)

_LAUNCHPAD_COLOR_MK1: dict[str, int] = {
    # Official Programmers Reference table (normal use, Flags=12): 16*Green + Red + 12.
    "off": 12,
    "red": 15,
    "amber": 63,
    "white": 62,  # yellow full — the brightest MK1 color (it has no white)
    "green": 60,
}
_LAUNCHPAD_COLOR_MK2: dict[str, int] = {
    "off": 0,
    "red": 5,
    "amber": 12,
    "white": 3,
    "green": 60,
}

launchpad_out: Any = None  # open mido output port; None = no Launchpad (all mirror calls no-op)
launchpad_protocol: str | None = None  # protocol class of the connected device (grid/colors)
_launchpad_lock = threading.Lock()

# Runtime bindings (tag -> palette slot) recorded at widget creation so apply_palette() can
# re-theme live. Theme color items are updated via set_value; text/draw items via
# configure_item (verified against DPG 2.3.1 in the e06 spike probes).
_theme_color_bindings: dict[Any, str] = {}
_text_color_bindings: dict[Any, str] = {}
_draw_color_bindings: dict[Any, tuple[str, str]] = {}

active_palette: dict[str, list[int]] = copy.deepcopy(DEFAULT_PALETTE)
theme_global: Any = None

# Global chrome theme components (bind_theme) and their palette slots; only bound for
# non-Scuro themes, so Scuro keeps the exact DPG dark defaults (legacy look).
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

# --- COMMUNICATION QUEUES ---
ui_state_queue: queue.Queue[Any] = queue.Queue()
blob_queue: queue.Queue[Any] = queue.Queue()
texture_queue: queue.Queue[Any] = queue.Queue()
log_queue: queue.Queue[str] = queue.Queue()
ui_task_queue: queue.Queue[Callable[[], None]] = (
    queue.Queue()
)  # UI mutations from worker threads, drained on the main thread


def ui_task(fn: Callable[[], None]) -> None:
    """Run a UI mutation on the main thread via the task queue."""
    ui_task_queue.put(fn)


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


# ==============================================================================
# THEMING (e06s02)
# ==============================================================================


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
    item_tag = dpg.add_theme_color(component, palette_rgba(active_palette[slot]), category=category)
    _theme_color_bindings[item_tag] = slot


def themed_text(*args: Any, slot: str, **kwargs: Any) -> Any:
    """Add text colored from the active palette; the (tag, slot) pair is recorded for re-theming."""
    kwargs.pop("color", None)
    tag = dpg.add_text(*args, color=palette_rgba(active_palette[slot]), **kwargs)
    _text_color_bindings[tag] = slot
    return tag


# Per-cell cache for the Mediagrid value updates (perf e07 P0): a viOSC state push that
# does not change a cell's displayed string skips the set_value entirely. Cleared whenever
# the tables are rebuilt, so freshly created widgets are never wrongly skipped.
_media_cell_cache: dict[str, str] = {}


def _set_media_cell(tag: str, value: str) -> None:
    """set_value for a media-grid cell, skipping writes whose displayed string is unchanged."""
    if _media_cell_cache.get(tag) != value and dpg.does_item_exist(tag):
        dpg.set_value(tag, value)
    _media_cell_cache[tag] = value


def themed_draw_rectangle(*args: Any, slot: str, color_kwarg: str = "fill", **kwargs: Any) -> Any:
    """draw_rectangle with a palette-driven color; the (tag, slot) pair is recorded."""
    kwargs[color_kwarg] = palette_rgba(active_palette[slot])
    tag = dpg.draw_rectangle(*args, **kwargs)
    _draw_color_bindings[tag] = (slot, color_kwarg)
    return tag


def ensure_global_theme() -> None:
    """Build (once) and bind the global chrome theme from the active palette."""
    global theme_global
    if theme_global is None:
        with dpg.theme() as t, dpg.theme_component(dpg.mvAll):
            for component, slot in GLOBAL_THEME_COMPONENTS:
                theme_color(component, slot)
        theme_global = t
    dpg.bind_theme(theme_global)


def apply_palette(palette: dict[str, list[int]]) -> None:
    """Push every recorded color binding; runs on the main thread only (boot + user changes)."""
    global active_palette
    for item_tag, slot in _theme_color_bindings.items():
        dpg.set_value(item_tag, palette_rgba(palette[slot]))
    for item_tag, slot in _text_color_bindings.items():
        if dpg.does_item_exist(item_tag):
            dpg.configure_item(item_tag, color=palette_rgba(palette[slot]))
    for item_tag, (slot, kwarg) in _draw_color_bindings.items():
        if dpg.does_item_exist(item_tag):
            dpg.configure_item(item_tag, **{kwarg: palette_rgba(palette[slot])})
    active_palette = palette


def _to_palette_color(raw: Any) -> list[int]:
    """Normalized 0..1 RGBA (color_edit callback payload) -> palette RGB on the 0..255 scale."""
    return [round(float(c) * DPG_COLOR_SCALE) for c in raw[:3]]


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
    global active_palette
    preset = str(theme.get("preset", "scuro"))
    palette = theme["colors"]
    active_palette = palette
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


# ==============================================================================
# USER CONFIG + WINDOW LAYOUT (e06s01)
# ==============================================================================


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
    return merged


def save_config(cfg: dict[str, Any]) -> None:
    """Atomically write the user config; failures are logged, never fatal."""
    try:
        tmp = f"{CONFIG_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        log_error("Config", f"cannot write {CONFIG_PATH}: {e}")


def _existing_layout_window_tags() -> list[str]:
    """Tags of every layout-tracked window currently present in the UI."""
    tags = [t for t in LAYOUT_WINDOW_TAGS if dpg.does_item_exist(t)]
    tags += [p["tag"] for p in monitor_players if dpg.does_item_exist(p["tag"])]
    return tags


def snapshot_window_layout() -> list[dict[str, Any]]:
    """Record shown/pos/size for every existing layout-tracked window (main thread only).

    LAYOUT_ALWAYS_HIDDEN_TAGS (the Settings window) are always recorded as closed: they
    stay open while the user clicks "Save layout", and must not come back at boot.
    """
    records: list[dict[str, Any]] = []
    for tag in _existing_layout_window_tags():
        try:
            shown = bool(dpg.is_item_shown(tag)) and tag not in LAYOUT_ALWAYS_HIDDEN_TAGS
            records.append(
                {
                    "tag": tag,
                    "shown": shown,
                    "pos": list(dpg.get_item_pos(tag)),
                    "size": [
                        int(dpg.get_item_width(tag) or 0),
                        int(dpg.get_item_height(tag) or 0),
                    ],
                }
            )
        except Exception as e:
            log_error("Layout", f"skip {tag}: {e}")
    return records


def apply_window_layout(records: list[dict[str, Any]]) -> None:
    """Re-apply a saved layout to currently existing windows; missing windows are skipped.

    LAYOUT_ALWAYS_HIDDEN_TAGS are never shown by a restore, even if the record says
    otherwise (heals configs saved before the e06s01 revision).
    """
    for rec in records:
        tag = rec.get("tag")
        if not tag or not dpg.does_item_exist(tag):
            continue
        try:
            dpg.set_item_pos(tag, rec["pos"])
            dpg.set_item_width(tag, rec["size"][0])
            dpg.set_item_height(tag, rec["size"][1])
            shown = bool(rec.get("shown")) and tag not in LAYOUT_ALWAYS_HIDDEN_TAGS
            if shown:
                dpg.show_item(tag)
            else:
                dpg.hide_item(tag)
        except Exception as e:
            log_error("Layout", f"apply {tag}: {e}")


def save_layout_to_config(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Settings 'Save layout': snapshot the current layout and persist it."""
    cfg = load_config()
    cfg["layout"]["windows"] = snapshot_window_layout()
    save_config(cfg)


def restore_layout_from_config(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Settings 'Restore layout': re-apply the saved layout from the config."""
    cfg = load_config()
    apply_window_layout(cfg["layout"]["windows"])


def should_restore_layout_on_boot(cfg: dict[str, Any]) -> bool:
    """Whether boot should re-apply the saved layout (default True when unset)."""
    return bool(cfg["layout"].get("restore_on_boot", True))


def on_restore_layout_boot_toggle(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Settings 'Restore at startup' checkbox: persist the flag."""
    cfg = load_config()
    cfg["layout"]["restore_on_boot"] = bool(app_data)
    save_config(cfg)


def apply_boot_config() -> None:
    """Boot: apply the persisted theme and (optionally) the saved window layout (e06)."""
    cfg = load_config()
    midi_init_from_config(cfg)  # e09: MIDI control mirrors (enabled, port, bindings)
    _apply_theme_config(cfg["theme"])
    if dpg.does_item_exist("cb_restore_layout_boot"):
        dpg.set_value("cb_restore_layout_boot", cfg["layout"]["restore_on_boot"])
    if should_restore_layout_on_boot(cfg):
        apply_window_layout(cfg["layout"]["windows"])


def enqueue_set_value(tag: str, value: Any) -> None:
    """Queue a dpg.set_value(tag, value) for the main thread, if the item exists."""

    def _set():
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, value)

    ui_task(_set)


def log_error(context: str, message: str) -> None:
    t = time.strftime("%H:%M:%S")
    log_queue.put(f"[{t}] ERROR: {context}: {message}")


def format_osc_log(history: list[str]) -> str:
    """Render the OSC log newest-first (the latest line on top)."""
    return "\n".join(reversed(history))


# --- GLOBAL VIMIX STATE ---
global_vimix_state: dict[str, Any] = {"current_source": None, "sources": {}}
# e10s06: viseq-side primary selection (target_id) — wins over the vimix current
# source for tile theming and for sequencer/monitor attachment.
viseq_selected_source: str | None = None

ALL_PROPERTIES = [
    "index",
    "name",
    "lock",
    "failed",
    "play",
    "pause",
    "blending",
    "alpha",
    "transparency",
    "depth",
    "position",
    "size",
    "corner",
    "angle",
    "seek",
    "speed",
    "brightness",
    "contrast",
    "saturation",
    "hue",
    "threshold",
    "gamma",
    "color",
    "posterize",
    "invert",
    "uri",
]

last_ui_signature: str = ""
last_num_cols: int = 4
osc_log_history: list[str] = []

# --- SEQUENCER STATE ---
NUM_STEPS = 8
NUM_TRACKS = 8
current_step: int = -1
is_playing: bool = False
phase_nudge: float = 0.0
sync_event_seq = threading.Event()
sync_event_led = threading.Event()

# Beat/clock source selection (e05): the sequencer can follow the analyzed BPM, a band
# hitting 1.0, standard MIDI clock, or a manual BPM (numeric/TAP). Event-driven modes wake
# the sequencer on sync_event_beat instead of sleeping a fixed interval.
BEAT_SOURCE_ANALYSIS = "bpm_analysis"
BEAT_SOURCE_BAND1 = "band1_beat"
BEAT_SOURCE_MIDI = "midi_sync"
BEAT_SOURCE_MANUAL = "manual_bpm"
BEAT_SOURCE_LABELS = {
    BEAT_SOURCE_ANALYSIS: "BPM Detection",
    BEAT_SOURCE_BAND1: "Beat Band 1",
    BEAT_SOURCE_MIDI: "MIDI Sync",
    BEAT_SOURCE_MANUAL: "Manual BPM",
}
beat_source: str = BEAT_SOURCE_ANALYSIS  # default: current behavior (essentia BPM)
sync_event_beat = threading.Event()  # fired once per beat in band/MIDI modes
MIDI_CLOCK_PULSES_PER_BEAT = 24  # MIDI standard: 24 clock pulses (0xF8) per quarter note
midi_pulses: int = 0  # running MIDI clock pulse count (worker thread)
tap_times: list[float] = []  # TAP timestamps for the manual BPM mode
band_prev_values: dict[int, float] = {1: 0.0, 2: 0.0, 3: 0.0}  # band rising-edge tracking
copied_step_data: dict[str, Any] | None = None  # step config copied for paste (e08)
active_step: tuple[int, int] | None = None  # last touched step (keyboard shortcuts target)
copied_step_pos: tuple[int, int] | None = None  # where the copied highlight is shown
# Width of the transport row (PLAY + spacer + < + RESYNC + > + spacer). Measured on real
# DPG 2.3.1: buttons render wider than their declared widths, so the alignment spacer is
# 312px with the compact 28px-high transport (audit L-6).
SEQ_TRANSPORT_WIDTH = 312

# One LED per beat source, shown next to its checkbox on the sequencer (e05)
BEAT_LED_TAGS = {
    BEAT_SOURCE_ANALYSIS: "led_analysis",
    BEAT_SOURCE_BAND1: "led_band1",
    BEAT_SOURCE_MIDI: "led_midi",
    BEAT_SOURCE_MANUAL: "led_manual",
}
BEAT_CHECKBOX_TAGS = {mode: f"cb_beat_{mode}" for mode in BEAT_SOURCE_LABELS}

# Sequencer data structure with the new "msgs" parameter
tracks_data: list[dict[str, Any]] = []
for _ in range(NUM_TRACKS):
    track: dict[str, Any] = {
        "target_id": None,
        "base_address": "",
        "active_fade": {"active": False},
        "steps": [],
    }
    for _ in range(NUM_STEPS):
        track["steps"].append(
            {
                "active": False,
                "type": "NONE",
                "v1": 0.0,
                "v2": 1.0,
                "frames": 4,
                "msgs": 1,  # NEW: number of messages to send in a single step
                "color": [1.0, 1.0, 1.0],
                "last_rand_v1": 0.0,
                "last_rand_seek": 0.0,
                "last_rand_color": [0, 0, 0],
            }
        )
    tracks_data.append(track)

# --- AUDIO STATE ---
samplerate = 44100
is_audio_analyzing: bool = False
is_beat_tracking: bool = False
lowpass_enabled: bool = True  # mirrors the "Use Low-Pass Filter" checkbox (read on worker threads)
audio_stream: Any = None
audio_buffer: np.ndarray = np.zeros(
    samplerate * 6, dtype=np.float32
)  # preallocated ring buffer (L-2)
audio_buffer_head: int = 0  # next write position in audio_buffer (modulo its length)
current_bpm: float = 120.0
beat_confidence: float = 0.0
rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
lowpass_filter = es.LowPass(cutoffFrequency=250.0)

# --- SPECTRUM ANALYZER (e04) ---
SPECTRUM_FFT_SIZE = 2048  # samples per FFT frame
# 16 bars > 32: benchmarked lighter (~26% less compute per frame + half the draw calls);
# the FFT dominates either way, and 16 bars keep a clear view of the audible range.
SPECTRUM_BARS = 16
NUM_BANDS = 3  # independent selectable bands (band1/band2/band3)
SPECTRUM_FPS = 30.0  # spectrum redraw rate while analyzing
SPECTRUM_DB_FLOOR = 60.0  # dB below full scale mapped to bar level 0
SPEC_DRAWLIST_W = 330  # spectrum drawlist width (px)
SPEC_DRAWLIST_H = 66  # spectrum drawlist height (px) — tall enough to read the bars
BAND_RECT_COLORS = {
    1: ((255, 255, 0, 40), (255, 255, 0, 200)),  # yellow overlay
    2: ((0, 255, 255, 40), (0, 255, 255, 200)),  # cyan overlay
    3: ((255, 0, 255, 40), (255, 0, 255, 200)),  # magenta overlay
}
BAND_DEFAULT_RANGES = {1: (0.0, 0.33), 2: (0.33, 0.66), 3: (0.66, 1.0)}  # equal thirds

# Last computed spectrum bars + per-band state. All written on the main thread inside the
# queued spectrum task; future features read band1/band2/band3 (0..1, 0 while disabled).
spectrum_bars_cache: np.ndarray = np.zeros(SPECTRUM_BARS, dtype=np.float32)
bands_enabled: dict[int, bool] = {1: False, 2: False, 3: False}
band1: float = 0.0
band2: float = 0.0
band3: float = 0.0

# e10s03: per-source list of texture tags (tex_<name>_<idx>)
thumbnails_data: dict[str, list[str]] = {}
request_timestamps: dict[str, float] = {}
# e10s04: per-source thumb cycle state {target_id: (current_index, last_switch_time)}
thumb_cycle_state: dict[str, tuple[int, float]] = {}
THUMB_CYCLE_INTERVAL = 0.75  # seconds per frame in the Mediagrid thumb cycle
# e10s04: consecutive unanswered thumb requests per source -> failed tile state
thumb_fail_count: dict[str, int] = {}
THUMB_FAIL_THRESHOLD = 5  # request cycles (~15 s) before the tile flips to failed
THUMB_FAIL_LABEL = " [ Thumb failed — right-click to retry ] "


def get_input_devices() -> list[str]:
    devices = sd.query_devices()
    inputs = [f"{i}: {d['name']}" for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    return inputs if inputs else ["No input device found"]


def flash_led(tag: str) -> None:
    """Flash a beat LED green on the main thread, fading back after 100ms (HIGH-1)."""

    def _on():
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, fill=(80, 255, 120, 255))

    def _off():
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, fill=(50, 50, 50, 255))

    ui_task(_on)
    threading.Timer(0.1, lambda: ui_task(_off)).start()


def append_log(direction: str, address: str) -> None:
    t = time.strftime("%H:%M:%S")
    log_msg = f"[{t}] {direction}: {address}"
    log_queue.put(log_msg)


def frame_sleep() -> float:
    """Main-loop sleep: full rate while animating, throttled while idle (perf e07 P1)."""
    if is_playing or is_audio_analyzing:
        return FRAME_SLEEP_ANIMATED
    for p in monitor_players:
        if not p.get("target_id"):
            continue
        _, props = find_source_by_name(p["target_id"])
        if props is None:
            continue
        seek = max(0.0, min(1.0, float(props.get("seek") or 0.0)))
        if video_is_playing(props, p.get("prev_seek", 0.0), seek):
            return FRAME_SLEEP_ANIMATED  # a spinning disc keeps full rate
    return FRAME_SLEEP_IDLE


# ==============================================================================
# SEQUENCER UI & CLIP ASSIGNMENT
# ==============================================================================


def midi_action_track_assign(row: int) -> None:
    """Assign the currently selected media to track row (e10s06: viseq selection first)."""
    target_id = get_current_target_id()
    if target_id is None:
        return
    tracks_data[row]["target_id"] = target_id
    tracks_data[row]["base_address"] = f"/vimix/{target_id}"
    update_track_slot_ui(row)


def assign_clip_to_track(sender: Any, app_data: Any, user_data: Any) -> None:
    midi_action_track_assign(user_data)


def update_track_slot_ui(row: int) -> None:
    slot_tag = f"seq_slot_{row}"
    if not dpg.does_item_exist(slot_tag):
        return

    dpg.delete_item(slot_tag, children_only=True)
    target_id = tracks_data[row].get("target_id")

    dpg.add_spacer(parent=slot_tag, height=SLOT_BUTTON_TOP_SPACER)
    if target_id:
        if target_id in thumbnails_data:
            tex_tag = thumbnails_data[target_id][0]
            dpg.add_image_button(
                texture_tag=tex_tag,
                width=SLOT_BUTTON_WIDTH,
                height=SLOT_BUTTON_HEIGHT,
                indent=SLOT_BUTTON_INDENT,
                tag=f"seq_thumb_{row}",  # stable tag: the thumb cycle switches it (e10s05)
                callback=learnable(
                    assign_clip_to_track, lambda ud: (MIDI_ACTION_TRACK_ASSIGN, {"row": ud})
                ),
                user_data=row,
                parent=slot_tag,
            )
        else:
            dpg.add_button(
                label=f"{target_id[:10]}\n(Waiting...)",
                width=SLOT_BUTTON_WIDTH,
                height=SLOT_BUTTON_HEIGHT,
                indent=SLOT_BUTTON_INDENT,
                callback=learnable(
                    assign_clip_to_track, lambda ud: (MIDI_ACTION_TRACK_ASSIGN, {"row": ud})
                ),
                user_data=row,
                parent=slot_tag,
            )
    else:
        dpg.add_button(
            label="ASSIGN\nCLIP",
            width=SLOT_BUTTON_WIDTH,
            height=SLOT_BUTTON_HEIGHT,
            indent=SLOT_BUTTON_INDENT,
            callback=learnable(
                assign_clip_to_track, lambda ud: (MIDI_ACTION_TRACK_ASSIGN, {"row": ud})
            ),
            user_data=row,
            parent=slot_tag,
        )


def set_step_type(sender: Any, app_data: Any, user_data: Any) -> None:
    row, col, step_type = user_data
    tracks_data[row]["steps"][col]["type"] = step_type
    update_step_ui(row, col)


def _highlight_copied_step(row: int, col: int) -> None:
    """Show the copied-step highlight and clear any previous one."""
    global copied_step_pos
    if copied_step_pos is not None:
        r, c = copied_step_pos
        update_step_theme(r, c)  # restore the standard theme for the previous copy
    copied_step_pos = (row, col)
    dpg.bind_item_theme(f"seq_cell_{row}_{col}", theme_step_copied)


def copy_step(sender: Any, app_data: Any, user_data: Any) -> None:
    """Remember the full configuration of a step for later paste (e08)."""
    global copied_step_data, active_step
    row, col = user_data
    active_step = (row, col)
    copied_step_data = copy.deepcopy(tracks_data[row]["steps"][col])
    _highlight_copied_step(row, col)


def paste_step(sender: Any, app_data: Any, user_data: Any) -> None:
    """Apply the copied step configuration to the given step (e08)."""
    global active_step
    row, col = user_data
    active_step = (row, col)
    if copied_step_data is None:
        return
    tracks_data[row]["steps"][col] = copy.deepcopy(copied_step_data)
    update_step_ui(row, col)


def paste_step_to_row(sender: Any, app_data: Any, user_data: Any) -> None:
    """Apply the copied step configuration to every step of the sequencer row (e08)."""
    global active_step
    row, _ = user_data
    active_step = (row, 0)
    if copied_step_data is None:
        return
    for c in range(NUM_STEPS):
        tracks_data[row]["steps"][c] = copy.deepcopy(copied_step_data)
        update_step_ui(row, c)


def on_copy_shortcut(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Ctrl+C: copy the last touched step (ignored while typing in an input)."""
    if _any_input_focused() or active_step is None:
        return
    copy_step(None, None, active_step)


def on_paste_shortcut(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Ctrl+V: paste into the last touched step (ignored while typing in an input)."""
    if _any_input_focused() or active_step is None:
        return
    paste_step(None, None, active_step)


def _on_copy_key(sender: Any, app_data: Any, user_data: Any) -> None:
    """Key handler wrapper: DPG 2.3.1 handlers ignore modifiers, so gate on Ctrl here."""
    if dpg.is_key_down(dpg.mvKey_ModCtrl):
        on_copy_shortcut(sender, app_data, user_data)


def _on_paste_key(sender: Any, app_data: Any, user_data: Any) -> None:
    """Key handler wrapper: only paste when Ctrl is held."""
    if dpg.is_key_down(dpg.mvKey_ModCtrl):
        on_paste_shortcut(sender, app_data, user_data)


INPUT_WIDGET_TAGS = ("manual_bpm_input", "viosc_ip", "viosc_port", "listen_ip", "listen_port")


def _any_input_focused() -> bool:
    """True when a text/number input has focus, so Ctrl+C/V keeps working for typing."""
    return any(dpg.does_item_exist(tag) and dpg.is_item_focused(tag) for tag in INPUT_WIDGET_TAGS)


def _set_step_active(row: int, col: int, state: bool) -> None:
    """Set a step's active state and refresh its visuals — shared by mouse and MIDI."""
    global active_step
    active_step = (row, col)  # clicking a step makes it the shortcut target
    tracks_data[row]["steps"][col]["active"] = state
    if dpg.does_item_exist(f"seq_cb_{row}_{col}"):
        dpg.set_value(f"seq_cb_{row}_{col}", state)  # keep the cell checkbox in sync
    update_step_theme(row, col)


def toggle_step_active(sender: Any, app_data: Any, user_data: Any) -> None:
    row, col = user_data
    _set_step_active(row, col, bool(app_data))


def midi_action_seq_toggle(row: int, col: int) -> None:
    """Toggle step (row, col) from a MIDI trigger (e09; mouse path keeps the checkbox value)."""
    _set_step_active(row, col, not tracks_data[row]["steps"][col]["active"])


def update_step_val(sender: Any, app_data: Any, user_data: Any) -> None:
    row, col, param_name = user_data
    if param_name == "color":
        tracks_data[row]["steps"][col][param_name] = app_data[:3]
        # live-refresh the step square when a color is picked from the popup picker
        square_tag = f"color_square_{row}_{col}"
        if dpg.does_item_exist(square_tag):
            dpg.set_value(square_tag, dpg_color_rgba(app_data[:3]))
    else:
        tracks_data[row]["steps"][col][param_name] = app_data


def update_step_theme(row: int, col: int, is_head: bool = False) -> None:
    # Runs on any thread: capture state here, apply the theme on the main thread.
    cell_tag = f"seq_cell_{row}_{col}"
    is_active = tracks_data[row]["steps"][col]["active"]
    launchpad_mirror_step(row, col, is_active, is_head)  # e09s03: LED mirror (thread-safe)
    ui_task(lambda: _apply_step_theme(cell_tag, is_active, is_head))


def _apply_step_theme(cell_tag: str, is_active: bool, is_head: bool) -> None:
    if not dpg.does_item_exist(cell_tag):
        return
    if is_head:
        dpg.bind_item_theme(cell_tag, theme_cell_play_on if is_active else theme_cell_play_off)
    else:
        dpg.bind_item_theme(cell_tag, theme_cell_on if is_active else theme_cell_off)


def update_step_ui(row: int, col: int) -> None:
    cell_tag = f"seq_cell_{row}_{col}"
    step_data = tracks_data[row]["steps"][col]

    if not dpg.does_item_exist(cell_tag):
        return

    dpg.delete_item(cell_tag, children_only=True)

    with dpg.group(horizontal=True, parent=cell_tag):
        cb = dpg.add_checkbox(
            default_value=step_data["active"],
            tag=f"seq_cb_{row}_{col}",
            callback=learnable(
                toggle_step_active,
                lambda ud: (MIDI_ACTION_SEQ_TOGGLE, {"row": ud[0], "col": ud[1]}),
            ),
            user_data=(row, col),
        )
        dpg.add_text(
            step_data["type"] if step_data["type"] != "NONE" else "",
            color=palette_rgba(active_palette["text"]),
            tag=f"seq_type_{row}_{col}",
        )
        _text_color_bindings[f"seq_type_{row}_{col}"] = "text"

        with dpg.popup(cb, mousebutton=dpg.mvMouseButton_Right):
            dpg.add_menu_item(label="Empty", callback=set_step_type, user_data=(row, col, "NONE"))
            dpg.add_separator()
            dpg.add_menu_item(
                label="Alpha Value", callback=set_step_type, user_data=(row, col, "AlphaV")
            )
            dpg.add_menu_item(
                label="Alpha Random", callback=set_step_type, user_data=(row, col, "AlphaR")
            )
            dpg.add_menu_item(
                label="Alpha Fade", callback=set_step_type, user_data=(row, col, "AlphaF")
            )
            dpg.add_separator()
            dpg.add_menu_item(
                label="Color Value", callback=set_step_type, user_data=(row, col, "ColorV")
            )
            dpg.add_menu_item(
                label="Color Random", callback=set_step_type, user_data=(row, col, "ColorR")
            )
            dpg.add_separator()
            dpg.add_menu_item(
                label="Seek Random", callback=set_step_type, user_data=(row, col, "SeekR")
            )
            dpg.add_separator()
            dpg.add_menu_item(label="Copy Step", callback=copy_step, user_data=(row, col))
            dpg.add_menu_item(label="Paste Step", callback=paste_step, user_data=(row, col))
            dpg.add_menu_item(
                label="Paste to Row", callback=paste_step_to_row, user_data=(row, col)
            )

    if step_data["type"] == "AlphaV":
        dpg.add_spacer(parent=cell_tag, height=5)
        dpg.add_drag_float(
            parent=cell_tag,
            width=70,
            default_value=step_data["v1"],
            min_value=0.0,
            max_value=1.0,
            speed=0.01,
            format="%.2f",
            callback=update_step_val,
            user_data=(row, col, "v1"),
        )

    elif step_data["type"] == "AlphaR":
        dpg.add_spacer(parent=cell_tag, height=5)
        dpg.add_text(
            f"{step_data['last_rand_v1']:.2f}",
            color=(150, 255, 150, 255),
            tag=f"rand_v1_{row}_{col}",
            parent=cell_tag,
            indent=20,
        )

    elif step_data["type"] == "AlphaF":
        dpg.add_spacer(parent=cell_tag, height=2)
        # NEW UI: split into two compact rows to fit the intermediate messages
        with dpg.group(horizontal=True, parent=cell_tag):
            dpg.add_drag_float(
                width=34,
                default_value=step_data["v1"],
                min_value=0.0,
                max_value=1.0,
                speed=0.01,
                format="%.1f",
                callback=update_step_val,
                user_data=(row, col, "v1"),
            )
            dpg.add_drag_float(
                width=34,
                default_value=step_data["v2"],
                min_value=0.0,
                max_value=1.0,
                speed=0.01,
                format="%.1f",
                callback=update_step_val,
                user_data=(row, col, "v2"),
            )
        with dpg.group(horizontal=True, parent=cell_tag):
            dpg.add_drag_int(
                width=34,
                default_value=step_data["frames"],
                min_value=1,
                max_value=32,
                speed=1,
                format="%ds",
                callback=update_step_val,
                user_data=(row, col, "frames"),
            )
            dpg.add_drag_int(
                width=34,
                default_value=step_data["msgs"],
                min_value=1,
                max_value=32,
                speed=1,
                format="%dm",
                callback=update_step_val,
                user_data=(row, col, "msgs"),
            )

    elif step_data["type"] == "ColorV":
        dpg.add_spacer(parent=cell_tag, height=6)
        # color is stored normalized (0..1); color_button needs DPG-scale RGBA (0..255)
        btn_tag = f"color_square_{row}_{col}"
        dpg.add_color_button(
            parent=cell_tag,
            default_value=dpg_color_rgba(step_data["color"]),
            no_border=True,
            no_tooltip=True,
            width=STEP_COLOR_SQUARE_SIZE,
            height=STEP_COLOR_SQUARE_SIZE,
            indent=STEP_COLOR_SQUARE_INDENT,
            tag=btn_tag,
        )
        with dpg.popup(btn_tag, mousebutton=dpg.mvMouseButton_Left):
            dpg.add_color_picker(
                default_value=dpg_color_rgba(step_data["color"]),
                no_alpha=True,
                width=200,
                callback=update_step_val,
                user_data=(row, col, "color"),
            )

    elif step_data["type"] == "ColorR":
        dpg.add_spacer(parent=cell_tag, height=6)
        # last_rand_color is stored normalized (0..1); color_button needs DPG-scale RGBA
        dpg.add_color_button(
            parent=cell_tag,
            default_value=dpg_color_rgba(step_data["last_rand_color"]),
            no_border=True,
            no_tooltip=True,
            width=STEP_COLOR_SQUARE_SIZE,
            height=STEP_COLOR_SQUARE_SIZE,
            indent=STEP_COLOR_SQUARE_INDENT,
            tag=f"rand_color_{row}_{col}",
        )

    elif step_data["type"] == "SeekR":
        dpg.add_spacer(parent=cell_tag, height=5)
        dpg.add_text(
            f"{step_data.get('last_rand_seek', 0.0):.2f}",
            color=(150, 200, 255, 255),
            tag=f"rand_seek_{row}_{col}",
            parent=cell_tag,
            indent=20,
        )

    update_step_theme(row, col, is_head=(is_playing and current_step == col))


def regen_thumb_callback(sender: Any, app_data: Any, user_data: Any) -> None:
    target_id = user_data
    if viosc_client:
        msg_addr = f"/viosc/regen_thumb/{target_id}"
        viosc_client.send_message(msg_addr, [])
        append_log("OUT", msg_addr)

    with dpg.mutex():
        tex_tags = thumbnails_data.pop(target_id, None)
        thumb_fail_count[target_id] = 0  # a manual retry clears the failure state (e10s04)
        if dpg.does_item_exist(f"img_{target_id}"):
            dpg.delete_item(f"img_{target_id}")
        for tex_tag in tex_tags or []:
            if dpg.does_item_exist(tex_tag):
                dpg.delete_item(tex_tag)

        container_tag = f"thumb_container_{target_id}"
        loading_tag = f"loading_txt_{target_id}"
        if dpg.does_item_exist(container_tag) and not dpg.does_item_exist(loading_tag):
            dpg.add_text(
                "  [ Rigenero... ]",
                color=(255, 200, 50, 255),
                tag=loading_tag,
                parent=container_tag,
            )
            with dpg.popup(loading_tag, mousebutton=dpg.mvMouseButton_Right):
                dpg.add_menu_item(
                    label="Regenerate Thumbnail (Random)",
                    callback=regen_thumb_callback,
                    user_data=target_id,
                )


def apply_thumbnail_texture(name: str, idx: str, img_data: Any, w: int, h: int) -> None:
    """Apply one decoded thumbnail frame on the main thread (e10s03).

    Each reply index becomes its own static texture (tex_<name>_<idx>) and is
    appended to the per-source list in thumbnails_data. The FIRST texture of a
    source creates the tile image (and its regen popup); later indices only
    append to the list — no widget churn.
    """
    target_id = name
    tex_tag = f"tex_{target_id}_{idx}"
    img_tag = f"img_{target_id}"
    container_tag = f"thumb_container_{target_id}"
    loading_tag = f"loading_txt_{target_id}"

    if dpg.does_item_exist(tex_tag):
        dpg.delete_item(tex_tag)

    dpg.add_static_texture(
        width=w, height=h, default_value=img_data, tag=tex_tag, parent="texture_registry"
    )

    is_first = target_id not in thumbnails_data
    thumbs = thumbnails_data.setdefault(target_id, [])
    if tex_tag not in thumbs:
        thumbs.append(tex_tag)
    thumb_fail_count.pop(name, None)  # a reply clears the failure state (e10s04)

    if is_first and not dpg.does_item_exist(img_tag) and dpg.does_item_exist(container_tag):
        if dpg.does_item_exist(loading_tag):
            dpg.delete_item(loading_tag)
        dpg.add_image(texture_tag=tex_tag, tag=img_tag, width=115, height=65, parent=container_tag)

        with dpg.popup(img_tag, mousebutton=dpg.mvMouseButton_Right):
            dpg.add_menu_item(
                label="Regenerate Thumbnail (Random)",
                callback=regen_thumb_callback,
                user_data=target_id,
            )

    for r, track in enumerate(tracks_data):
        if track.get("target_id") == target_id:
            update_track_slot_ui(r)
    for p in monitor_players:
        if p.get("target_id") == target_id:
            update_monitor_player_ui(p["id"])


def advance_thumb_cycle(
    thumbs_count: int, now: float, state: tuple[int, float]
) -> tuple[int, float]:
    """Advance one source's thumb-cycle state on a fixed wall-clock cadence (pure).

    Returns the new (index, last_switch_time). Sources with fewer than two
    frames never cycle (images stay static); the anchor moves only when a
    switch actually happens, so time spent hidden does not fast-forward frames.
    """
    cur, last = state
    if thumbs_count < 2:
        return 0, last
    if now - last < THUMB_CYCLE_INTERVAL:
        return cur, last
    return (cur + 1) % thumbs_count, now


def _thumb_cycle_active() -> bool:
    """True while any thumbnail consumer window is visible (e10s05 gate).

    The Mediagrid, the sequencer and every monitor player window each show
    per-source thumbnails; cycling runs while at least one of them is open so
    the animation follows the media wherever it is applied.
    """
    for tag in ("vimix_media_window", "sequencer_window"):
        if dpg.does_item_exist(tag) and dpg.is_item_shown(tag):
            return True
    for p in monitor_players:
        if p.get("target_id") and dpg.does_item_exist(p["tag"]) and dpg.is_item_shown(p["tag"]):
            return True
    return False


def _apply_cycle_frame(target_id: str, tex_tag: str) -> None:
    """Switch every visible consumer of a source to the cycled frame (e10s05).

    The Mediagrid tile, every sequencer slot and every monitor player assigned
    to the source switch together on the same cadence; consumers whose widget
    is gone (window closed, slot unassigned) are skipped.
    """
    img_tag = f"img_{target_id}"
    if dpg.does_item_exist(img_tag):
        dpg.configure_item(img_tag, texture_tag=tex_tag)
    for r, track in enumerate(tracks_data):
        if track.get("target_id") == target_id:
            slot_tag = f"seq_thumb_{r}"
            if dpg.does_item_exist(slot_tag):
                dpg.configure_item(slot_tag, texture_tag=tex_tag)
    for p in monitor_players:
        if p.get("target_id") == target_id:
            mon_tag = f"mon_thumb_{p['id']}"
            if dpg.does_item_exist(mon_tag):
                dpg.configure_item(mon_tag, texture_tag=tex_tag)


def tick_thumb_cycle(now: float) -> None:
    """Advance thumb frames once per main-loop frame (e10s04 + e10s05).

    Gated: no cycling while every consumer window (Mediagrid, sequencer,
    monitor players) is hidden or gone. Tiles with >=2 stored textures switch
    texture_tag via configure_item on the cadence; the switch reuses
    pre-loaded static textures (SPIKE-thumb-cycle: ~1.6 us per call).
    """
    if not _thumb_cycle_active():
        return
    for target_id, thumbs in list(thumbnails_data.items()):
        state = thumb_cycle_state.get(target_id, (0, now))
        new_state = advance_thumb_cycle(len(thumbs), now, state)
        thumb_cycle_state[target_id] = new_state
        if new_state[0] != state[0]:
            _apply_cycle_frame(target_id, thumbs[new_state[0]])


def _show_failed_tile_label(target_id: str) -> None:
    """Flip the tile's pending label to the failed state in place.

    The request loop runs on the main thread; when the unanswered-request
    counter crosses the threshold the existing "Loading..." label is replaced
    by the failed label + retry popup without waiting for a grid rebuild
    (BUG-2026-08-27T201742: the rebuild-only rendering kept the tile on
    "Loading..." forever).
    """
    container_tag = f"thumb_container_{target_id}"
    loading_tag = f"loading_txt_{target_id}"
    if not dpg.does_item_exist(container_tag) or dpg.does_item_exist(f"img_{target_id}"):
        return
    if dpg.does_item_exist(loading_tag):
        dpg.delete_item(loading_tag)
    dpg.add_text(
        THUMB_FAIL_LABEL,
        parent=container_tag,
        color=palette_rgba(active_palette["warning"]),
        tag=loading_tag,
    )
    _text_color_bindings[loading_tag] = "text_dim"
    with dpg.popup(loading_tag, mousebutton=dpg.mvMouseButton_Right):
        dpg.add_menu_item(
            label="Regenerate Thumbnail (Random)",
            callback=regen_thumb_callback,
            user_data=target_id,
        )


def _char_width_px() -> int:
    """Width of a single character in the live default font (e10s06).

    The default font is monospace (ProggyClean, 7 px); measuring 'M' (the
    widest glyph) keeps the budget conservative if a proportional font is ever
    loaded. Falls back to a constant until the font atlas is built
    (get_text_size -> None).
    """
    try:
        size = dpg.get_text_size("M")
        if size and size[0]:
            return max(1, int(size[0]))
    except Exception:
        pass
    return MEDIA_TITLE_CHAR_PX


def truncate_media_title(name: str) -> str:
    """Fit a media name into at most two Mediagrid title lines (e10s06).

    The budget is PER WRAPPED LINE (MEDIA_TITLE_WRAP / char width), not a
    total-width budget: a 245 px string at a 125 px wrap still needs three
    lines (17 + 17 + 1 chars). The longest prefix that keeps
    prefix+ellipsis within two lines is returned; the full name stays in the
    raw table and in target_id — only the display is truncated.
    """
    if not name:
        return name
    text = str(name)
    chars_per_line = max(1, MEDIA_TITLE_WRAP // _char_width_px())
    max_chars = chars_per_line * MEDIA_TITLE_MAX_LINES
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(MEDIA_TITLE_ELLIPSIS))
    return text[:keep] + MEDIA_TITLE_ELLIPSIS


def _tile_theme_for(idx: Any, target_id: str) -> Any:
    """Pick the Mediagrid tile theme (e10s06).

    The viseq-side primary selection uses the green selection theme; the vimix
    current source alone uses the lighter non-green theme; everything else is
    plain. The viseq selection wins when both point at the same source.
    """
    if target_id == viseq_selected_source:
        return theme_selected_clip
    if str(idx) == str(global_vimix_state.get("current_source")):
        return theme_vimix_current_clip
    return theme_normal_clip


def refresh_tile_selection_themes() -> None:
    """Re-apply the selection themes to every Mediagrid tile without a rebuild.

    Called from the tile click handler: the viseq selection changes without
    touching the grid signature, so the theme binding loop re-runs on the
    existing tiles instead of rebuilding them.
    """
    for idx, props in global_vimix_state.get("sources", {}).items():
        name = props.get("name")
        target_id = str(name) if name else str(idx)
        tile_tag = f"tile_{target_id}"
        if dpg.does_item_exist(tile_tag):
            dpg.bind_item_theme(tile_tag, _tile_theme_for(idx, target_id))


def on_media_tile_click(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Select a media from the Mediagrid — the viseq-side primary selection (e10s06)."""
    global viseq_selected_source
    target_id = user_data
    if target_id == viseq_selected_source:
        return
    viseq_selected_source = target_id
    refresh_tile_selection_themes()


def request_missing_thumbnails(now: float) -> None:
    """Request thumbs for sources that still lack them (3 s throttle, e10s04).

    Each sent-but-unanswered request bumps the source's fail counter; crossing
    the threshold fires ONE regen retry and the tile flips to the failed label
    (rendered by update_vimix_sources_ui). A successful reply clears the counter.
    """
    if not viosc_client:
        return
    for idx, props in global_vimix_state.get("sources", {}).items():
        name = props.get("name")
        uri = props.get("uri")
        target_id = str(name) if name else str(idx)

        if uri and target_id not in thumbnails_data:
            last_thumb = request_timestamps.get(f"thumb_{target_id}", 0)
            if now - last_thumb > THUMB_REQUEST_INTERVAL:
                msg_addr = f"/viosc/thumb/{target_id}"
                viosc_client.send_message(msg_addr, ["all"])
                append_log("OUT", msg_addr)
                request_timestamps[f"thumb_{target_id}"] = now
                thumb_fail_count[target_id] = thumb_fail_count.get(target_id, 0) + 1
                if thumb_fail_count[target_id] == THUMB_FAIL_THRESHOLD:
                    regen_addr = f"/viosc/regen_thumb/{target_id}"
                    viosc_client.send_message(regen_addr, [])
                    append_log("OUT", regen_addr)
                    _show_failed_tile_label(target_id)


def update_vimix_sources_ui(json_string: str) -> None:
    global global_vimix_state, last_ui_signature, last_num_cols, viseq_selected_source
    try:
        payload = json.loads(json_string)
        if not isinstance(payload, dict):
            raise ValueError("replydata payload is not a JSON object")
        sources = payload.get("sources", {})
        if sources is None:
            sources = {}
        if not isinstance(sources, dict):
            raise ValueError("replydata 'sources' is not an object")
        # Drop malformed entries so a single bad source can't crash the UI/main loop
        sources = {k: v for k, v in sources.items() if isinstance(v, dict)}
        global_vimix_state["current_source"] = payload.get("current_source")
        global_vimix_state["sources"] = sources

        # L-1: prune cached state for sources that no longer exist (main thread only),
        # so thumbnails_data / request_timestamps / registry textures do not grow across churn.
        live_ids = set()
        for k, props in sources.items():
            name = props.get("name")
            live_ids.add(str(name) if name else str(k))
        for target_id in list(thumbnails_data):
            if target_id not in live_ids:
                tex_tags = thumbnails_data.pop(target_id)
                for tex_tag in tex_tags:
                    if dpg.does_item_exist(tex_tag):
                        dpg.delete_item(tex_tag)
                click_reg_tag = media_tile_click_registry_tag(target_id)
                if dpg.does_item_exist(click_reg_tag):
                    dpg.delete_item(click_reg_tag)  # stale click registry must not linger
        for key in list(request_timestamps):
            if key.startswith("thumb_"):
                target_id = key[len("thumb_") :]
                if target_id not in live_ids:
                    request_timestamps.pop(key)
        for target_id in list(thumb_fail_count):
            if target_id not in live_ids:
                thumb_fail_count.pop(target_id)
        if viseq_selected_source is not None and viseq_selected_source not in live_ids:
            viseq_selected_source = None  # a pruned source can't stay selected (e10s06)

        current_source = global_vimix_state["current_source"]
        data_dict = global_vimix_state["sources"]

        def get_sort_index(k):
            idx_val = data_dict[k].get("index")
            if idx_val is not None:
                try:
                    return int(idx_val)
                except (TypeError, ValueError):
                    pass
            try:
                return int(k)
            except (TypeError, ValueError):
                return 0

        sorted_keys = sorted(data_dict.keys(), key=get_sort_index)
        # current_source joins the signature so a selection change re-runs the structural
        # tile updates (theme/title/index) — the only per-source fields it affects (perf e07).
        current_signature = f"cols:{last_num_cols}_src:{current_source}_" + str(
            [(k, data_dict[k].get("name"), data_dict[k].get("index")) for k in sorted_keys]
        )

        if current_signature != last_ui_signature:
            # every rebuild recreates the widgets from scratch (defaults): the per-cell
            # value cache must not skip writes for the freshly created cells (perf e07 P0)
            _media_cell_cache.clear()
            if dpg.does_item_exist("vimix_table"):
                dpg.delete_item("vimix_table")
            if dpg.does_item_exist("media_grid"):
                dpg.delete_item("media_grid")

            t_raw = dpg.add_table(
                parent="vimix_raw_group",
                tag="vimix_table",
                header_row=True,
                borders_innerH=True,
                borders_innerV=True,
                row_background=True,
                scrollX=True,
                scrollY=True,
                freeze_columns=2,
                height=180,
            )
            for prop in ALL_PROPERTIES:
                dpg.add_table_column(label=prop.capitalize(), parent=t_raw)

            for idx in sorted_keys:
                r_id = dpg.add_table_row(parent=t_raw)
                props_i = data_dict[idx]
                for prop in ALL_PROPERTIES:
                    tag_name = f"raw_{idx}_{prop}"
                    if dpg.does_item_exist(tag_name):
                        dpg.delete_item(tag_name)
                    if dpg.does_alias_exist(tag_name):
                        dpg.remove_alias(tag_name)
                    val = props_i.get(prop)
                    if prop == "index" and val is None:
                        val = idx
                    if isinstance(val, float):
                        val_str = f"{val:.2f}"
                    elif val is None:
                        val_str = "---"
                    else:
                        val_str = str(val)
                    dpg.add_text(val_str, parent=r_id, tag=tag_name)

            num_cols = last_num_cols
            t_grid = dpg.add_table(
                parent="vimix_media_group",
                tag="media_grid",
                header_row=False,
                borders_innerH=False,
                borders_innerV=False,
                policy=dpg.mvTable_SizingFixedFit,
            )
            for _ in range(num_cols):
                dpg.add_table_column(parent=t_grid)

            for i in range(0, len(sorted_keys), num_cols):
                row_indices = sorted_keys[i : i + num_cols]
                r_id = dpg.add_table_row(parent=t_grid)
                for idx in row_indices:
                    name = data_dict[idx].get("name")
                    target_id = str(name) if name else str(idx)
                    tile_tag = f"tile_{target_id}"

                    if dpg.does_item_exist(tile_tag):
                        dpg.delete_item(tile_tag)
                    cw = dpg.add_child_window(
                        parent=r_id,
                        width=135,
                        height=MEDIA_TILE_H,
                        border=True,
                        no_scrollbar=True,
                        tag=tile_tag,
                    )
                    click_reg_tag = media_tile_click_registry_tag(target_id)
                    if dpg.does_item_exist(click_reg_tag):
                        dpg.delete_item(click_reg_tag)  # a rebuild must not leak registries
                    with dpg.item_handler_registry(tag=click_reg_tag):
                        # DPG 2.3.1 calls item-handler callbacks with co_argcount
                        # args (Python 3.13 counts defaults -> 4 args, extras are
                        # None), so the target id must be captured, never received.
                        dpg.add_item_clicked_handler(
                            0,  # left click selects; right-click keeps the regen popup
                            callback=lambda *_, t=target_id: on_media_tile_click(None, None, t),
                        )

                    title_tag = f"tile_title_{target_id}"
                    if dpg.does_item_exist(title_tag):
                        dpg.delete_item(title_tag)
                    dpg.add_text(
                        "---",
                        parent=cw,
                        wrap=MEDIA_TITLE_WRAP,
                        color=palette_rgba(active_palette["text_bright"]),
                        tag=title_tag,
                    )
                    _text_color_bindings[title_tag] = "text_bright"
                    with dpg.popup(title_tag, mousebutton=dpg.mvMouseButton_Right):
                        dpg.add_menu_item(
                            label="Regenerate Thumbnail (Random)",
                            callback=regen_thumb_callback,
                            user_data=target_id,
                        )

                    dpg.add_spacer(parent=cw, height=4)
                    container_tag = f"thumb_container_{target_id}"
                    if dpg.does_item_exist(container_tag):
                        dpg.delete_item(container_tag)

                    g_id = dpg.add_group(parent=cw, tag=container_tag, indent=4)
                    img_tag = f"img_{target_id}"
                    loading_tag = None  # set only in the no-thumbs branch below
                    if target_id in thumbnails_data:
                        tex_tag = thumbnails_data[target_id][0]
                        if dpg.does_item_exist(img_tag):
                            dpg.delete_item(img_tag)
                        dpg.add_image(
                            texture_tag=tex_tag, parent=g_id, tag=img_tag, width=115, height=65
                        )
                        with dpg.popup(img_tag, mousebutton=dpg.mvMouseButton_Right):
                            dpg.add_menu_item(
                                label="Regenerate Thumbnail (Random)",
                                callback=regen_thumb_callback,
                                user_data=target_id,
                            )
                    else:
                        loading_tag = f"loading_txt_{target_id}"
                        if dpg.does_item_exist(loading_tag):
                            dpg.delete_item(loading_tag)
                        is_failed = thumb_fail_count.get(target_id, 0) >= THUMB_FAIL_THRESHOLD
                        dpg.add_text(
                            THUMB_FAIL_LABEL if is_failed else " [ Loading... ]",
                            parent=g_id,
                            color=palette_rgba(
                                active_palette["warning"]
                                if is_failed
                                else active_palette["text_dim"]
                            ),
                            tag=loading_tag,
                        )
                        _text_color_bindings[loading_tag] = "text_dim"
                        with dpg.popup(loading_tag, mousebutton=dpg.mvMouseButton_Right):
                            dpg.add_menu_item(
                                label="Regenerate Thumbnail (Random)",
                                callback=regen_thumb_callback,
                                user_data=target_id,
                            )

                    # Compact per-media readout: index badge + bare alpha value (e06)
                    index_tag = f"tile_index_{target_id}"
                    alpha_tag = f"tile_alpha_{target_id}"
                    with dpg.group(horizontal=True, parent=cw):
                        dpg.add_button(
                            label="-",
                            width=MEDIA_BADGE_W,
                            height=MEDIA_BADGE_H,
                            tag=index_tag,
                        )
                        dpg.bind_item_theme(index_tag, theme_media_badge)
                        dpg.add_text("---", color=(200, 230, 200, 255), tag=alpha_tag)

                    # e10s06: the tile's clickable children select the media on left
                    # click (child windows can't host clicked handlers in DPG 2.x).
                    _bind_tile_click_targets(
                        click_reg_tag,
                        title_tag,
                        container_tag,
                        img_tag,
                        loading_tag,
                        index_tag,
                        alpha_tag,
                    )

                for _ in range(num_cols - len(row_indices)):
                    dpg.add_text("", parent=r_id)

            # Structural per-source updates: theme/title/index depend only on signature
            # fields (name/index/current_source/columns), so they run only on a real change
            # (perf e07 P0: an unchanged push used to re-write them every time). The
            # signature is committed AFTER the loop: a mid-loop failure must not lock the
            # grid to a half-built state (the next push retries the rebuild).
            for idx in sorted_keys:
                props = data_dict[idx]
                name = props.get("name")
                target_id = str(name) if name else str(idx)
                display_name = str(name) if name else f"Idx: {idx}"
                tile_tag = f"tile_{target_id}"

                if dpg.does_item_exist(tile_tag):
                    dpg.bind_item_theme(tile_tag, _tile_theme_for(idx, target_id))
                _set_media_cell(f"tile_title_{target_id}", truncate_media_title(display_name))
                if dpg.does_item_exist(f"tile_index_{target_id}"):
                    idx_val = props.get("index")
                    idx_str = str(idx_val) if idx_val is not None else str(idx)
                    dpg.configure_item(f"tile_index_{target_id}", label=idx_str)
            last_ui_signature = current_signature

        # Value per-source updates: every cell write goes through the per-cell cache, so a
        # push that changes nothing performs zero dpg calls (perf e07 P0; measured ~20
        # calls/source/push before).
        for idx in sorted_keys:
            props = data_dict[idx]
            name = props.get("name")
            target_id = str(name) if name else str(idx)
            alpha_val = props.get("alpha")
            alpha_str = f"{alpha_val:.2f}" if isinstance(alpha_val, float) else "---"
            _set_media_cell(f"tile_alpha_{target_id}", alpha_str)

            for prop in ALL_PROPERTIES:
                val = props.get(prop)
                if prop == "index" and val is None:
                    val = idx
                if isinstance(val, float):
                    val_str = f"{val:.2f}"
                elif val is None:
                    val_str = "---"
                else:
                    val_str = str(val)
                _set_media_cell(f"raw_{idx}_{prop}", val_str)

    except Exception as e:
        log_error("UI update", str(e))


def thumbnail_decoder_worker() -> None:
    while True:
        name, idx, blob_bytes = blob_queue.get()
        try:
            image = Image.open(io.BytesIO(blob_bytes))
            width, height = image.size
            if width * height > MAX_THUMBNAIL_PIXELS:
                raise ValueError(f"thumbnail too large: {width}x{height} px")
            rgba = image.convert("RGBA")
            img_data = np.array(rgba, dtype=np.float32) / 255.0
            texture_queue.put((name, idx, img_data.flatten(), width, height))
        except Exception as e:
            print(f"[viseq Decoder Error] Unable to decode '{name}': {e}")
        blob_queue.task_done()


# ==============================================================================
# MONITOR PLAYERS
# ==============================================================================
monitor_players: list[dict[str, Any]] = []  # each: {"id", "tag", "target_id", "props"}
monitor_player_counter = 0


def get_current_target_id() -> str | None:
    """Return the target id of the currently selected media (e10s06).

    The viseq Mediagrid selection is primary; before the first click (or after
    the selected source is removed) it falls back to the vimix current source.
    """
    if viseq_selected_source is not None:
        return viseq_selected_source
    current_source = global_vimix_state.get("current_source")
    if current_source is None:
        return None
    for k, props in global_vimix_state.get("sources", {}).items():
        if str(k) == str(current_source):
            name = props.get("name")
            return str(name) if name else str(k)
    return None


def find_source_by_name(name: str) -> Any:
    for idx, props in global_vimix_state.get("sources", {}).items():
        if str(props.get("name")) == str(name):
            return idx, props
    return None, None


def find_player_index(player_id: int) -> int | None:
    for i, p in enumerate(monitor_players):
        if p["id"] == player_id:
            return i
    return None


def send_monitor_command(player_id: int) -> None:
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = player["target_id"]
    if not target_id:
        return
    addr = f"/viosc/monitor/{target_id}"
    props = player.get("props", [])
    if props:
        osc_client.send_message(addr, list(props))
        append_log("OUT", f"{addr} {props}")
    else:
        osc_client.send_message(addr, [])
        append_log("OUT", f"{addr} (stop)")


def new_monitor_player(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    global monitor_player_counter
    monitor_player_counter += 1
    player_id = monitor_player_counter
    tag = f"monitor_player_{player_id}"
    player = {
        "id": player_id,
        "tag": tag,
        "target_id": None,
        "props": list(DEFAULT_MONITOR_PROPS),
        "disc_angle": 0.0,
        "disc_last": 0.0,
    }
    monitor_players.append(player)
    pos = (
        10 + MONITOR_OFFSET[0] * ((player_id - 1) % 4),
        30 + MONITOR_OFFSET[1] * ((player_id - 1) // 4),
    )
    with dpg.window(label=f"Monitor Player {player_id}", tag=tag, width=270, height=150, pos=pos):
        head_tag = f"mon_head_{player_id}"
        themed_text(
            "Click the box below to assign the current source.",
            slot="text_dim",
            tag=head_tag,
            wrap=250,
        )
        with dpg.popup(head_tag, mousebutton=dpg.mvMouseButton_Right):
            dpg.add_menu_item(
                label="Monitor Properties...",
                callback=lambda s, a, u: open_monitor_props(player_id),
                user_data=player_id,
            )
            dpg.add_separator()
            dpg.add_menu_item(
                label="Remove Player",
                callback=lambda s, a, u: remove_monitor_player(player_id),
                user_data=player_id,
            )
        with dpg.group(tag=f"mon_body_{player_id}"):
            pass
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="Properties...",
                width=120,
                callback=lambda s, a, u: open_monitor_props(player_id),
                user_data=player_id,
            )
            dpg.add_button(
                label="Remove",
                width=90,
                callback=lambda s, a, u: remove_monitor_player(player_id),
                user_data=player_id,
            )
    update_monitor_player_ui(player_id)  # build the body: assign box or the readout


def update_monitor_player_ui(player_id: int) -> None:
    try:
        idx = find_player_index(player_id)
        if idx is None:
            return
        player = monitor_players[idx]
        tag = player["tag"]
        if not dpg.does_item_exist(tag):
            return
        target_id = player["target_id"]
        head = f"mon_head_{player_id}"
        if dpg.does_item_exist(head):
            if target_id:
                dpg.set_value(head, target_id)  # just the source name, no label
            else:
                dpg.set_value(head, "Click the box below to assign the current source.")
        body = f"mon_body_{player_id}"
        if not dpg.does_item_exist(body):
            return
        dpg.delete_item(body, children_only=True)
        with dpg.group(parent=body):
            if target_id:
                with dpg.group(horizontal=True):
                    if target_id in thumbnails_data:
                        dpg.add_image(
                            texture_tag=thumbnails_data[target_id][0],
                            width=MONITOR_THUMB_W,
                            height=MONITOR_THUMB_H,
                            tag=f"mon_thumb_{player_id}",  # stable tag: cycled by tick_thumb_cycle
                        )
                    else:
                        themed_text("Loading thumbnail...", slot="text_dim", wrap=MONITOR_THUMB_W)
                    # turntable disc: spins while playing, rate follows speed (1.0 = normal)
                    with dpg.drawlist(width=MONITOR_DISC_SIZE, height=MONITOR_DISC_SIZE):
                        dpg.draw_circle(
                            center=[MONITOR_DISC_SIZE // 2, MONITOR_DISC_SIZE // 2],
                            radius=MONITOR_DISC_SIZE // 2 - 4,
                            color=(60, 60, 70, 255),
                            fill=(25, 25, 35, 255),
                        )
                        dpg.draw_circle(
                            center=[MONITOR_DISC_SIZE // 2, MONITOR_DISC_SIZE // 2],
                            radius=MONITOR_DISC_SIZE // 2 - 12,
                            color=(40, 40, 50, 255),
                        )
                        dpg.draw_line(
                            p1=[MONITOR_DISC_SIZE // 2, MONITOR_DISC_SIZE // 2],
                            p2=[MONITOR_DISC_SIZE // 2, MONITOR_DISC_SIZE // 2 - MONITOR_DISC_R],
                            color=(180, 190, 200, 255),
                            thickness=2,
                            tag=f"mon_arm_{player_id}",
                        )
                        dpg.draw_circle(
                            center=[MONITOR_DISC_SIZE // 2, MONITOR_DISC_SIZE // 2],
                            radius=3,
                            color=(70, 70, 80, 255),
                            fill=(90, 90, 100, 255),
                        )
                        dpg.draw_text(
                            pos=(
                                MONITOR_DISC_SIZE // 2 - 12,
                                MONITOR_DISC_SIZE // 2 - MONITOR_SPEED_TEXT_SIZE // 2,
                            ),
                            text="1.00",
                            color=(220, 230, 240, 255),
                            size=MONITOR_SPEED_TEXT_SIZE,
                            tag=f"mon_speed_{player_id}",
                        )

                    # vertical alpha bar (filled from the bottom)
                    with dpg.drawlist(width=MONITOR_ALPHA_W, height=MONITOR_DISC_SIZE):
                        dpg.draw_rectangle(
                            pmin=[0, 0],
                            pmax=[MONITOR_ALPHA_W, MONITOR_DISC_SIZE],
                            color=(60, 60, 70, 255),
                            fill=(35, 35, 45, 255),
                        )
                        dpg.draw_rectangle(
                            pmin=[0, MONITOR_DISC_SIZE],
                            pmax=[MONITOR_ALPHA_W, MONITOR_DISC_SIZE],
                            color=(200, 255, 200, 255),
                            fill=(120, 220, 120, 255),
                            tag=f"mon_alpha_fill_{player_id}",
                        )
                # horizontal seek bar (video progress 0..1)
                with dpg.drawlist(width=MONITOR_SEEK_W, height=10):
                    dpg.draw_rectangle(
                        pmin=[0, 0],
                        pmax=[MONITOR_SEEK_W, 10],
                        color=(60, 60, 70, 255),
                        fill=(35, 35, 45, 255),
                    )
                    dpg.draw_rectangle(
                        pmin=[0, 0],
                        pmax=[0, 10],
                        color=(180, 220, 255, 255),
                        fill=(90, 160, 220, 255),
                        tag=f"mon_seek_fill_{player_id}",
                    )
            else:
                dpg.add_button(
                    label="CLICK TO ASSIGN",
                    width=MONITOR_SEEK_W,
                    height=60,
                    callback=assign_monitor_player,
                    user_data=player_id,
                )
    except Exception as e:
        print(f"[viseq Monitor UI] Error updating player {player_id}: {e}")


def video_is_playing(props: dict[str, Any], prev_seek: float, cur_seek: float) -> bool:
    """True when the source video is moving: explicit play flag, or seek advancing.

    viOSC may report play as a bool, 0/1, or a string; when it is absent, a
    progressing seek is a reliable playing signal (paused video -> static seek).
    """
    play = props.get("play")
    if isinstance(play, bool):
        return play
    if isinstance(play, (int, float)):
        return play != 0
    if isinstance(play, str):
        return play.strip().lower() in ("1", "true", "yes", "on")
    return cur_seek > prev_seek + 1e-4


def refresh_monitor_display(player_id: int) -> None:
    """Spin the turntable and update the alpha/seek bars from the source props.

    Runs on the main thread every frame; the disc angle advances only while
    the video plays, at a rate proportional to the speed. Configure calls are
    skipped when nothing changed (perf e07 P2): the arm only moves while the
    video plays, and speed/alpha/seek are re-written only on value changes.
    """
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = player["target_id"]
    if not target_id:
        return
    _, props = find_source_by_name(target_id)
    if props is None:
        return
    now = time.time()
    dt = now - player.get("disc_last", now)
    player["disc_last"] = now
    speed = float(props.get("speed") or 1.0)
    if speed <= 0.0:
        speed = 1.0
    seek = max(0.0, min(1.0, float(props.get("seek") or 0.0)))
    # 33 RPM at speed 1.0 (0.55 rev/s = 3.455 rad/s); the disc spins only while moving
    disc_rate = MONITOR_DISC_RPM / 60.0 * 2.0 * math.pi
    playing = video_is_playing(props, player.get("prev_seek", 0.0), seek)
    if playing:
        player["disc_angle"] = player.get("disc_angle", 0.0) + disc_rate * speed * dt
        angle = player["disc_angle"]
        if dpg.does_item_exist(f"mon_arm_{player_id}"):
            dpg.configure_item(
                f"mon_arm_{player_id}",
                p2=[
                    MONITOR_DISC_SIZE / 2 + MONITOR_DISC_R * math.sin(angle),
                    MONITOR_DISC_SIZE / 2 - MONITOR_DISC_R * math.cos(angle),
                ],
            )
    player["prev_seek"] = seek

    if speed != player.get("last_speed", None):
        player["last_speed"] = speed
        if dpg.does_item_exist(f"mon_speed_{player_id}"):
            speed_str = f"{speed:.2f}"
            dpg.configure_item(
                f"mon_speed_{player_id}",
                text=speed_str,
                pos=(
                    MONITOR_DISC_SIZE // 2 - 6 * len(speed_str) + 2,
                    MONITOR_DISC_SIZE // 2 - MONITOR_SPEED_TEXT_SIZE // 2,
                ),
            )

    alpha = max(0.0, min(1.0, float(props.get("alpha") or 0.0)))
    if alpha != player.get("last_alpha", None):
        player["last_alpha"] = alpha
        if dpg.does_item_exist(f"mon_alpha_fill_{player_id}"):
            dpg.configure_item(
                f"mon_alpha_fill_{player_id}",
                pmin=[0, MONITOR_DISC_SIZE - alpha * MONITOR_DISC_SIZE],
            )
    if seek != player.get("last_seek", None):
        player["last_seek"] = seek
        if dpg.does_item_exist(f"mon_seek_fill_{player_id}"):
            dpg.configure_item(
                f"mon_seek_fill_{player_id}",
                pmax=[seek * MONITOR_SEEK_W, 10],
            )


def assign_monitor_player(sender: Any, app_data: Any, user_data: Any) -> None:
    player_id = user_data
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = get_current_target_id()
    if not target_id:
        if dpg.does_item_exist(f"mon_head_{player_id}"):
            dpg.set_value(f"mon_head_{player_id}", "No source selected in the media library.")
        return
    for other in monitor_players:
        if other["id"] != player_id and other.get("target_id") == target_id:
            if dpg.does_item_exist(f"mon_head_{player_id}"):
                dpg.set_value(
                    f"mon_head_{player_id}", f"Already monitored in Player {other['id']}."
                )
            return
    player["target_id"] = target_id
    player["props"] = list(DEFAULT_MONITOR_PROPS)
    send_monitor_command(player_id)
    update_monitor_player_ui(player_id)


def open_monitor_props(player_id: int) -> None:
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = player["target_id"]
    if not target_id:
        return
    modal_tag = f"mon_props_modal_{player_id}"
    if dpg.does_item_exist(modal_tag):
        dpg.delete_item(modal_tag)
    with dpg.window(
        label=f"Monitor Properties - {target_id}",
        tag=modal_tag,
        modal=True,
        width=270,
        height=400,
        no_resize=True,
    ):
        dpg.add_text("Select the properties to monitor:", wrap=240)
        dpg.add_separator()
        with dpg.child_window(height=310, border=True):
            for prop in ALL_PROPERTIES:
                dpg.add_checkbox(
                    label=prop,
                    default_value=(prop in player["props"]),
                    tag=f"mon_cb_{player_id}_{prop}",
                    callback=on_monitor_prop_toggle,
                    user_data=player_id,
                )


def on_monitor_prop_toggle(sender: Any, app_data: Any, user_data: Any) -> None:
    player_id = user_data
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    new_props = []
    for prop in ALL_PROPERTIES:
        cb_tag = f"mon_cb_{player_id}_{prop}"
        if dpg.does_item_exist(cb_tag) and dpg.get_value(cb_tag):
            new_props.append(prop)
    player["props"] = new_props
    send_monitor_command(player_id)


def remove_monitor_player(player_id: int) -> None:
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    if player.get("target_id"):
        addr = f"/viosc/monitor/{player['target_id']}"
        osc_client.send_message(addr, [])
        append_log("OUT", f"{addr} (stop)")
    tag = player["tag"]
    if dpg.does_item_exist(tag):
        dpg.delete_item(tag)
    del monitor_players[idx]


def incoming_osc_handler(address: str, *args: Any) -> None:
    append_log("IN ", address)
    try:
        if address == "/viosc/replydata" and args and len(args[0]) <= MAX_STATE_JSON_BYTES:
            ui_state_queue.put(args[0])
        elif (
            address.startswith("/viosc/replythumb/")
            and args
            and len(args[0]) <= MAX_THUMBNAIL_BLOB_BYTES
        ):
            parts = address.split("/")
            blob_queue.put((parts[-2], parts[-1], args[0]))
    except Exception as e:
        log_error("OSC input", str(e))


class ViseqOSCUDPServer(osc_server.ThreadingOSCUDPServer):
    """OSC receiver with a recv buffer large enough for full thumbnail blobs.

    socketserver.UDPServer defaults max_packet_size to 8192, which truncates
    thumbnail datagrams larger than 8 KB before python-osc can parse them
    (BUG-2026-08-27T201742); the buffer must fit the largest accepted blob plus
    the OSC header (address + typetag + size + padding) margin.
    """

    max_packet_size = MAX_THUMBNAIL_BLOB_BYTES + 4096


def start_osc_server(ip: str, port: int) -> bool:
    """Start the local OSC listening server (main thread); True when it is up."""
    global local_osc_server, local_server_thread, is_server_running
    if is_server_running:
        return True
    try:
        disp = dispatcher.Dispatcher()
        disp.set_default_handler(incoming_osc_handler)
        local_osc_server = ViseqOSCUDPServer((ip, port), disp)
        local_server_thread = threading.Thread(target=local_osc_server.serve_forever, daemon=True)
        local_server_thread.start()
        is_server_running = True
        dpg.set_item_label("btn_server_toggle", "Stop Server")
        dpg.set_value("server_status", f"Server Status: Listening on {ip}:{port}")
        return True
    except Exception as e:
        dpg.set_value("server_status", f"Server Status: ERROR ({e})")
        return False


def toggle_local_server() -> None:
    global local_osc_server, local_server_thread, is_server_running
    if is_server_running:
        if local_osc_server and local_server_thread is not None:
            local_osc_server.shutdown()
            local_server_thread.join(timeout=1.0)
            local_osc_server = None
        is_server_running = False
        dpg.set_item_label("btn_server_toggle", "Start Server")
        dpg.set_value("server_status", "Server Status: Stopped")
    else:
        start_osc_server(str(dpg.get_value("listen_ip")), int(dpg.get_value("listen_port")))


def connect_osc_client(ip: str, port: int) -> bool:
    """Create the viOSC client (main thread); True when ready."""
    global viosc_client
    try:
        viosc_client = udp_client.SimpleUDPClient(ip, port)
        dpg.set_value("viosc_status", f"Client Status: Ready on {ip}:{port}")
        return True
    except Exception:
        dpg.set_value("viosc_status", "Client Status: Initialization error")
        return False


def connect_to_viosc() -> None:
    """Connect the OSC client from the viOSC panel inputs (button callback)."""
    connect_osc_client(str(dpg.get_value("viosc_ip")), int(dpg.get_value("viosc_port")))


def autostart_osc() -> None:
    """Boot wiring: auto-connect the viOSC client and start the listening server."""
    connect_osc_client(VIOSC_IP, VIOSC_PORT)
    start_osc_server(VIOSC_IP, VIOSC_LISTEN_PORT)


def midi_action_beat_source(mode: str) -> None:
    """Select the sequencer beat source — shared by mouse and MIDI (e09)."""
    global beat_source, current_bpm
    beat_source = mode
    for m in BEAT_SOURCE_LABELS:
        dpg.set_value(f"cb_beat_{m}", m == beat_source)
    is_manual = beat_source == BEAT_SOURCE_MANUAL
    dpg.configure_item("manual_bpm_input", show=is_manual)
    dpg.configure_item("btn_tap", show=is_manual)
    if is_manual:
        current_bpm = float(dpg.get_value("manual_bpm_input"))
        dpg.set_value("testo_bpm", f"BPM: {current_bpm:.1f}")
        dpg.set_value("manual_bpm_text", f"{current_bpm:.0f} BPM")


def on_beat_source(sender: Any, app_data: Any, user_data: Any) -> None:
    """Select the sequencer beat source; exactly one checkbox stays active."""
    if not app_data:
        dpg.set_value(sender, True)  # a beat source must remain selected
        return
    midi_action_beat_source(user_data)


def on_manual_bpm(sender: Any, app_data: Any, user_data: Any) -> None:
    """Set the sequencer BPM from the manual numeric input."""
    global current_bpm
    current_bpm = float(app_data)
    dpg.set_value("testo_bpm", f"BPM: {current_bpm:.1f}")
    dpg.set_value("manual_bpm_text", f"{current_bpm:.0f} BPM")


def midi_action_transport_tap() -> None:
    """Register a tap for the manual BPM — shared by mouse and MIDI (e09)."""
    global current_bpm
    now = time.time()
    if tap_times and now - tap_times[-1] > 2.0:
        tap_times.clear()  # stale tap starts a new sequence
    tap_times.append(now)
    del tap_times[:-8]  # keep the most recent taps
    if len(tap_times) >= 2:
        intervals = [tap_times[i + 1] - tap_times[i] for i in range(len(tap_times) - 1)]
        bpm = 60.0 / (sum(intervals) / len(intervals))
        current_bpm = round(bpm, 2)
        dpg.set_value("manual_bpm_input", round(current_bpm))
        dpg.set_value("testo_bpm", f"BPM: {current_bpm:.1f}")
        dpg.set_value("manual_bpm_text", f"{current_bpm:.0f} BPM")


def tap_bpm(sender: Any, app_data: Any, user_data: Any) -> None:
    """Set the BPM from the average interval of the last taps (manual mode)."""
    midi_action_transport_tap()


def midi_beats_from_pulses(pulses: int) -> int:
    """Whole quarter-note beats contained in a MIDI clock pulse count (24 pulses/beat)."""
    return pulses // MIDI_CLOCK_PULSES_PER_BEAT


# ---------- e09: MIDI control engine ----------
def binding_matches(binding: dict[str, Any], msg_type: str, number: int, channel: int) -> bool:
    """True when a parsed MIDI message (type, number, channel) matches the binding."""
    if binding.get("type") != msg_type:
        return False
    if int(binding.get("number", -1)) != number:
        return False
    return int(binding.get("channel", 0)) == channel


def _binding_device_ok(binding: dict[str, Any], port_name: str) -> bool:
    """A binding matches the port when its device is empty (wildcard) or equals it."""
    dev = binding.get("device")
    return not dev or dev == port_name or dev == "*"


def _parse_midi_msg(msg: Any) -> tuple[str | None, int, int]:
    """Map a mido message to (type, number, value); release edges and other types -> None.

    note_on velocity>0 is the trigger edge (velocity 0 and note_off are releases and must
    never fire a binding — Launchpad sends note_on with velocity 0 on release).
    """
    if msg.type == "note_on":
        if msg.velocity > 0:
            return ("note", int(msg.note), int(msg.velocity))
        return (None, 0, 0)
    if msg.type == "note_off":
        return (None, 0, 0)
    if msg.type == "control_change":
        return ("cc", int(msg.control), int(msg.value))
    return (None, 0, 0)


def resolve_midi_message(msg: Any, port_name: str) -> list[tuple[str, dict[str, Any], int]]:
    """Bindings matching a raw mido message on the given port -> (action, params, value)."""
    msg_type, number, value = _parse_midi_msg(msg)
    if msg_type is None:
        return []
    channel = int(getattr(msg, "channel", 0))
    out: list[tuple[str, dict[str, Any], int]] = []
    for binding in list(midi_bindings) + list(midi_auto_bindings):
        if not _binding_device_ok(binding, port_name):
            continue
        if binding_matches(binding, msg_type, number, channel):
            out.append((str(binding.get("action", "")), dict(binding.get("params") or {}), value))
    return out


def binding_source_from_message(msg: Any, port_name: str) -> dict[str, Any] | None:
    """The (device, channel, type, number) half of a binding from a message; releases -> None."""
    msg_type, number, _ = _parse_midi_msg(msg)
    if msg_type is None:
        return None
    return {"device": port_name, "channel": int(msg.channel), "type": msg_type, "number": number}


def midi_execute(action: str, params: dict[str, Any], value: int) -> None:
    """Execute a resolved MIDI action on the main thread (called via ui_task_queue, e09)."""
    if action == MIDI_ACTION_SEQ_TOGGLE:
        midi_action_seq_toggle(int(params.get("row", 0)), int(params.get("col", 0)))
    elif action == MIDI_ACTION_TRANSPORT_PLAY:
        toggle_play()
    elif action == MIDI_ACTION_TRANSPORT_RESYNC:
        callback_resync()
    elif action == MIDI_ACTION_TRANSPORT_TAP:
        midi_action_transport_tap()
    elif action == MIDI_ACTION_NUDGE_BACK:
        callback_nudge_backward()
    elif action == MIDI_ACTION_NUDGE_FORWARD:
        callback_nudge_forward()
    elif action == MIDI_ACTION_BEAT_SOURCE:
        midi_action_beat_source(str(params.get("mode", "")))
    elif action == MIDI_ACTION_TRACK_ASSIGN:
        midi_action_track_assign(int(params.get("row", 0)))


def _midi_enqueue_execute(action: str, params: dict[str, Any], value: int) -> None:
    """Push one resolved MIDI action execution to the main thread (ui_task_queue)."""
    ui_task(lambda: midi_execute(action, params, value))


def handle_midi_message(msg: Any, port_name: str) -> None:
    """Route one incoming message (worker thread): learn capture first, then dispatch."""
    if midi_learn_pending is not None:
        source = binding_source_from_message(msg, port_name)
        if source is not None:
            ui_task(lambda: midi_learn_complete(source))
            return
    for action, params, value in resolve_midi_message(msg, port_name):
        _midi_enqueue_execute(action, params, value)


def midi_learn_complete(binding: dict[str, Any]) -> None:
    """Main thread: merge the captured source with the pending action and store the binding."""
    global midi_learn_pending
    if midi_learn_pending is None:
        return
    action, params = midi_learn_pending
    binding["action"] = action
    binding["params"] = params
    midi_bindings.append(binding)
    midi_learn_pending = None
    refresh_midi_mappings_ui()
    if dpg.does_item_exist("midi_learn_btn"):
        dpg.set_item_label("midi_learn_btn", "Learn mapping...")
    if dpg.does_item_exist("midi_learn_status"):
        dpg.set_value(
            "midi_learn_status",
            f"Bound: {action} <- {binding['device']} {binding['type']} {binding['number']}",
        )


def learnable(callback: Any, action_builder: Callable[[Any], tuple[str, dict[str, Any]]]) -> Any:
    """Wrap a widget callback so MIDI Learn captures its action instead of executing it.

    In learn mode the wrapper stores (action, params) from action_builder(user_data) into
    midi_learn_pending and skips the real callback; otherwise it delegates unchanged, so a
    mouse click and a MIDI trigger share the exact same callback path (e09s02).
    """

    def wrapper(sender: Any, app_data: Any, user_data: Any) -> None:
        global midi_learn_pending
        if midi_learn_mode:
            midi_learn_pending = action_builder(user_data)
            if dpg.does_item_exist("midi_learn_status"):
                dpg.set_value("midi_learn_status", "Now press your MIDI button")
            return
        callback(sender, app_data, user_data)

    return wrapper


def toggle_midi_learn(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Toggle MIDI Learn mode from the MIDI window; the button doubles as Cancel (e09s02)."""
    global midi_learn_mode, midi_learn_pending
    if midi_learn_mode:
        midi_learn_mode = False
        midi_learn_pending = None
        if dpg.does_item_exist("midi_learn_btn"):
            dpg.set_item_label("midi_learn_btn", "Learn mapping...")
        if dpg.does_item_exist("midi_learn_status"):
            dpg.set_value("midi_learn_status", "MIDI Learn off")
        return
    if not midi_enabled:
        if dpg.does_item_exist("midi_learn_status"):
            dpg.set_value("midi_learn_status", "Enable MIDI first (tick Enable MIDI above)")
        return
    midi_learn_mode = True
    midi_learn_pending = None
    if dpg.does_item_exist("midi_learn_btn"):
        dpg.set_item_label("midi_learn_btn", "Cancel learn")
    if dpg.does_item_exist("midi_learn_status"):
        dpg.set_value("midi_learn_status", "MIDI Learn: click a viseq control")


def on_midi_enable(sender: Any, app_data: Any, user_data: Any) -> None:
    """MIDI window Enable checkbox: persist and apply the engine toggle (e09s02)."""
    global midi_learn_mode, midi_learn_pending
    set_midi_enabled(bool(app_data))
    if not app_data and midi_learn_mode:  # disabling cancels an in-flight learn
        midi_learn_mode = False
        midi_learn_pending = None
        if dpg.does_item_exist("midi_learn_btn"):
            dpg.set_item_label("midi_learn_btn", "Learn mapping...")


def on_midi_input_port(sender: Any, app_data: Any, user_data: Any) -> None:
    """MIDI device combo: remember the chosen input port (e09s02)."""
    global midi_input_port
    midi_input_port = app_data or None
    cfg = load_config()
    cfg["midi"]["input_port"] = midi_input_port
    save_config(cfg)


def _midi_binding_label(binding: dict[str, Any]) -> str:
    """Human-readable row label for one mapping (e09s02)."""
    params = binding.get("params") or {}
    suffix = f" {params}" if params else ""
    return (
        f"{binding.get('device', '?')} {binding.get('type', '?')} "
        f"{binding.get('number', '?')} -> {binding.get('action', '?')}{suffix}"
    )


def refresh_midi_mappings_ui() -> None:
    """Rebuild the MIDI Mappings window list (main thread; call after any change)."""
    if not dpg.does_item_exist("midi_mappings_group"):
        return
    dpg.delete_item("midi_mappings_group", children_only=True)
    for idx, binding in enumerate(midi_bindings):
        with dpg.group(horizontal=True, parent="midi_mappings_group"):
            dpg.add_text(_midi_binding_label(binding))
            dpg.add_button(label="Delete", callback=delete_midi_binding, user_data=idx)


def delete_midi_binding(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Remove a binding by list index and refresh the Mappings window (e09s02)."""
    idx = int(user_data)
    if 0 <= idx < len(midi_bindings):
        del midi_bindings[idx]
    refresh_midi_mappings_ui()


def save_midi_bindings(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Persist the current MIDI bindings to the config (e09s02)."""
    cfg = load_config()
    cfg["midi"]["bindings"] = midi_bindings
    save_config(cfg)


def refresh_midi_devices(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Re-scan MIDI inputs and update the Controller combo (keeps the selection)."""
    global _midi_input_devices
    try:
        import mido

        _midi_input_devices = list(mido.get_input_names())
    except Exception:
        _midi_input_devices = []
    if dpg.does_item_exist("midi_input_combo"):
        dpg.configure_item("midi_input_combo", items=_midi_input_devices)
        if midi_input_port in _midi_input_devices:
            dpg.set_value("midi_input_combo", midi_input_port)


def show_midi_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the MIDI window from the menubar, with a fresh device list and mappings."""
    refresh_midi_devices()
    refresh_midi_mappings_ui()
    dpg.show_item("midi_window")


def midi_init_from_config(cfg: dict[str, Any]) -> None:
    """Load the MIDI control mirrors from the config (boot; the menu UI comes in e09s02)."""
    global midi_enabled, midi_input_port
    midi_cfg = cfg.get("midi") or {}
    midi_enabled = bool(midi_cfg.get("enabled", False))
    midi_input_port = midi_cfg.get("input_port") or None
    midi_bindings[:] = midi_cfg.get("bindings") or []


def set_midi_enabled(enabled: bool) -> None:
    """Enable/disable the MIDI control engine and persist the flag (main thread)."""
    global midi_enabled
    midi_enabled = enabled
    cfg = load_config()
    cfg["midi"]["enabled"] = enabled
    save_config(cfg)


def midi_control_loop() -> None:
    """MIDI control worker (e09): match messages against bindings, push executions to the
    main thread via ui_task_queue (HIGH-1 — no direct dpg calls here).
    """
    global midi_input_port
    try:
        import mido
    except ImportError:
        return
    while True:
        if not midi_enabled:
            time.sleep(0.2)
            continue
        port_name = midi_input_port
        if not port_name:
            names = mido.get_input_names()
            if not names:
                time.sleep(1.0)  # no device: retry quietly
                continue
            port_name = names[0]  # device discovery (Launchpad picked by name in e09s03)
        try:
            with mido.open_input(port_name) as port:
                midi_input_port = port_name
                launchpad_connect(port_name, mido)  # e09s03: LED output + grid bindings
                append_log("MIDI", f"Control listening on {port_name}")
                while midi_enabled:
                    if midi_input_port != port_name:
                        break  # the user switched the controller: reconnect on next loop
                    for msg in port.iter_pending():
                        handle_midi_message(msg, port_name)
                    time.sleep(0.002)
        except Exception as e:
            log_error("MIDI", str(e))
            midi_input_port = None
            time.sleep(2.0)
        finally:
            launchpad_disconnect()


# ---------- e09s03: Novation Launchpad adapter ----------
def launchpad_model_from_name(port_name: str) -> str | None:
    """Classify a MIDI port name into a Launchpad protocol class (None = not a Launchpad)."""
    name = (port_name or "").lower()
    if "launchpad" not in name:
        return None
    if "mk3" in name or " launchpad x" in name or name.startswith("launchpad x"):
        return LAUNCHPAD_PROGRAMMER_MODE  # X / Mini MK3 / Pro MK3: SysEx first, then note grid
    if "mk2" in name or "pro" in name:
        return LAUNCHPAD_NOTE_MODE  # MK2 / Mini MK2 / Pro: native note mode, row*10+col grid
    return LAUNCHPAD_MK1  # plain "Launchpad"/"Launchpad S" (novlpd01): row*16+col grid


def launchpad_grid_note(row: int, col: int) -> int:
    """Grid note for pad (row, col) under the connected protocol.

    MK1: row*16+col (0-87); MK2 family: row*10+col (0-79).
    """
    if launchpad_protocol == LAUNCHPAD_MK1:
        return row * 16 + col
    return row * 10 + col


def _launchpad_velocity(color: str) -> int:
    """Translate a semantic color to the device velocity under the connected protocol."""
    table = _LAUNCHPAD_COLOR_MK1 if launchpad_protocol == LAUNCHPAD_MK1 else _LAUNCHPAD_COLOR_MK2
    return table.get(color, 0)


def launchpad_led(row: int, col: int, color: str) -> None:
    """Set one Launchpad pad LED (semantic color; lock-guarded best-effort)."""
    if launchpad_out is None:
        return
    try:
        import mido

        msg = mido.Message(
            "note_on", note=launchpad_grid_note(row, col), velocity=_launchpad_velocity(color)
        )
        with _launchpad_lock:
            launchpad_out.send(msg)
    except Exception as e:
        log_error("MIDI", f"Launchpad LED ({row},{col}): {e}")


def launchpad_mirror_step(row: int, col: int, is_active: bool, is_head: bool) -> None:
    """Mirror one step cell on the Launchpad grid (any thread; no-op without a device)."""
    if launchpad_out is None:
        return
    if is_head:
        launchpad_led(row, col, LAUNCHPAD_LED_AMBER)
    elif is_active:
        launchpad_led(row, col, LAUNCHPAD_LED_GREEN)
    else:
        launchpad_led(row, col, LAUNCHPAD_LED_OFF)


def launchpad_flash_playhead() -> None:
    """White pulse on the current playhead column, restored by a timer (beat flash, e09s03)."""
    if launchpad_out is None or current_step < 0:
        return
    for r in range(LAUNCHPAD_GRID_ROWS):
        launchpad_led(r, current_step, LAUNCHPAD_LED_WHITE)
    threading.Timer(LAUNCHPAD_FLASH_SECONDS, _launchpad_restore_playhead).start()


def _launchpad_restore_playhead() -> None:
    """Timer thread: re-apply the playhead amber after a beat flash."""
    if launchpad_out is None or current_step < 0:
        return
    for r in range(LAUNCHPAD_GRID_ROWS):
        active = tracks_data[r]["steps"][current_step]["active"]
        launchpad_mirror_step(r, current_step, active, True)


def _launchpad_register_grid_bindings(port_name: str) -> None:
    """Internal 8x8 grid bindings: pad (r,c) toggles step (r,c) — never persisted (e09s03)."""
    midi_auto_bindings.clear()
    for r in range(LAUNCHPAD_GRID_ROWS):
        for c in range(LAUNCHPAD_GRID_COLS):
            midi_auto_bindings.append(
                {
                    "device": port_name,
                    "channel": 0,
                    "type": "note",
                    "number": launchpad_grid_note(r, c),
                    "action": MIDI_ACTION_SEQ_TOGGLE,
                    "params": {"row": r, "col": c},
                    "auto": True,
                }
            )


def launchpad_connect(input_port_name: str, mido: Any) -> None:
    """Open the Launchpad output port; enable programmer mode for MK3-family."""
    global launchpad_out, launchpad_protocol
    launchpad_disconnect()
    launchpad_protocol = launchpad_model_from_name(input_port_name)
    if launchpad_protocol is None:
        return  # not a Launchpad: no LED output, no grid bindings
    out_names = mido.get_output_names()
    out_name = None
    for cand in out_names:
        if cand == input_port_name:
            out_name = cand
            break
    if out_name is None:
        for cand in out_names:
            if "launchpad" in cand.lower():
                out_name = cand
                break
    if out_name is None and out_names:
        out_name = out_names[0]  # single MIDI device around: assume it is the same one
    if out_name is None:
        return
    try:
        with _launchpad_lock:
            launchpad_out = mido.open_output(out_name)
        if launchpad_protocol == LAUNCHPAD_PROGRAMMER_MODE:
            launchpad_out.send(mido.Message("sysex", data=LAUNCHPAD_PROGRAMMER_SYSEX))
        _launchpad_register_grid_bindings(input_port_name)
        append_log("MIDI", f"Launchpad ({launchpad_protocol}) output on {out_name}")
    except Exception as e:
        log_error("MIDI", f"Launchpad output {out_name}: {e}")
        launchpad_disconnect()


def launchpad_disconnect() -> None:
    """Close the Launchpad output port and drop grid bindings/protocol (idempotent)."""
    global launchpad_out, launchpad_protocol
    with _launchpad_lock:
        if launchpad_out is not None:
            with contextlib.suppress(Exception):
                launchpad_out.close()
            launchpad_out = None
    midi_auto_bindings.clear()
    launchpad_protocol = None


def midi_clock_loop() -> None:
    """Listen for MIDI clock (0xF8, 24 pulses/beat) and fire the sequencer beat in MIDI mode.

    Opens the first available MIDI input; without any port (or backend) it logs once and
    idles so the app keeps running.
    """
    global midi_pulses
    try:
        import mido

        ports = mido.get_input_names()
        if not ports:
            log_error("MIDI", "no MIDI input port available")
            while True:
                time.sleep(10)
        with mido.open_input(ports[0]) as port:
            append_log("MIDI", f"Listening on {ports[0]}")
            while True:
                for msg in port.iter_pending():
                    if msg.type == "clock":
                        midi_pulses += 1
                        if midi_pulses >= MIDI_CLOCK_PULSES_PER_BEAT:
                            midi_pulses = 0
                            flash_led("led_midi")
                            if beat_source == BEAT_SOURCE_MIDI and is_playing:
                                sync_event_beat.set()
                time.sleep(0.001)
    except Exception as e:
        log_error("MIDI", str(e))
        while True:
            time.sleep(10)


def show_settings_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the general settings window from the top menubar."""
    dpg.show_item("settings_window")


def show_logs_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the OSC logs window from the top menubar (Show > Logs)."""
    dpg.show_item("logs_window")


def centered_window_pos(
    viewport_w: int, viewport_h: int, window_w: int, window_h: int
) -> tuple[int, int]:
    """Top-left position centering a window of the given size on a viewport.

    Pure math (no dpg): each axis is (viewport - window) // 2, clamped at >= 0 so a window
    larger than the viewport never gets a negative offset.
    """
    return (max(0, (viewport_w - window_w) // 2), max(0, (viewport_h - window_h) // 2))


def show_help_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the About window from the menubar, re-centered on the viewport."""
    dpg.set_item_pos(
        "help_window",
        centered_window_pos(
            dpg.get_viewport_width(),
            dpg.get_viewport_height(),
            dpg.get_item_width("help_window"),
            dpg.get_item_height("help_window"),
        ),
    )
    dpg.show_item("help_window")


def open_github(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the author's GitHub profile in the default browser (Help window link)."""
    try:
        import webbrowser

        webbrowser.open(GITHUB_URL)
    except Exception as e:
        log_error("Help", str(e))


def callback_resync(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    global current_step
    current_step = -1
    for r in range(NUM_TRACKS):
        tracks_data[r]["active_fade"]["active"] = False
    sync_event_seq.set()
    sync_event_led.set()


def callback_nudge_backward(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    global phase_nudge
    phase_nudge += 0.05


def callback_nudge_forward(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    global phase_nudge
    phase_nudge -= 0.05


def audio_callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
    global audio_buffer, audio_buffer_head
    if status:
        print(status)
    samples = indata[:, 0].astype(np.float32)
    # L-2 ring-buffer write: in-place, modulo indexing, no full-buffer reallocation
    # (np.roll allocated a fresh ~1 MB array ~43x/s on every callback).
    n = len(samples)
    if n >= len(audio_buffer):  # defensive: block larger than the buffer
        audio_buffer[:] = samples[-len(audio_buffer) :]
        audio_buffer_head = 0
    else:
        end = audio_buffer_head + n
        if end <= len(audio_buffer):
            audio_buffer[audio_buffer_head:end] = samples
        else:
            split = len(audio_buffer) - audio_buffer_head
            audio_buffer[audio_buffer_head:] = samples[:split]
            audio_buffer[: n - split] = samples[split:]
        audio_buffer_head = end % len(audio_buffer)


def get_audio_snapshot() -> np.ndarray:
    """Chronological copy of the last len(audio_buffer) samples (newest at tail).

    Linearizes the ring buffer for the BPM thread. Called once per second (not per
    audio callback), so this allocation is acceptable.
    """
    head = audio_buffer_head
    if head == 0:
        return audio_buffer.copy()
    return np.concatenate((audio_buffer[head:], audio_buffer[:head]))


def compute_spectrum_bars(samples: np.ndarray, n_bars: int = SPECTRUM_BARS) -> np.ndarray:
    """Magnitude spectrum of the latest samples, binned into n_bars levels (0..1).

    Hann-windowed rfft, dB scale with a -SPECTRUM_DB_FLOOR floor; a full-scale sine
    reaches ~1.0, silence ~0.0.
    """
    if samples.size < SPECTRUM_FFT_SIZE:
        samples = np.pad(samples, (0, SPECTRUM_FFT_SIZE - samples.size))
    frame = samples[-SPECTRUM_FFT_SIZE:] * np.hanning(SPECTRUM_FFT_SIZE)
    mag = np.abs(np.fft.rfft(frame))[1:]  # drop DC
    # max per bar: averaging in dB would drown a narrow peak among quiet bins
    mag_bins = np.array_split(mag, n_bars)
    db = 20.0 * np.log10(
        np.array([np.max(b) for b in mag_bins]) / (SPECTRUM_FFT_SIZE / 4.0) + 1e-12
    )
    levels = (db + SPECTRUM_DB_FLOOR) / SPECTRUM_DB_FLOOR
    return np.clip(levels, 0.0, 1.0)


def band_value_from_bars(
    bars: np.ndarray,
    start: float,
    end: float,
    min_level: float = 0.0,
    max_level: float = 1.0,
) -> float:
    """Mean fill (0..1) of the selection rectangle over the bars.

    The horizontal window [start, end) picks the bars; the vertical window
    [min_level, max_level] maps each bar's level so 0 = at/below min and
    1 = at/above max. An inverted/empty level window falls back to the plain
    bar mean (backward compatible with the frequency-only usage).
    """
    if bars.size == 0:
        return 0.0
    n = bars.size
    lo = round(start * n)
    hi = round(end * n)
    if hi <= lo:  # inverted/degenerate horizontal selection -> at least one bar
        hi = lo + 1
    lo = max(0, min(lo, n - 1))
    hi = max(lo + 1, min(hi, n))
    selected = bars[lo:hi]
    if max_level <= min_level:
        return float(np.mean(selected))
    mapped = np.clip((selected - min_level) / (max_level - min_level), 0.0, 1.0)
    return float(np.mean(mapped))


def _set_band_variable(band_id: int, value: float) -> None:
    """Store a band level into its module variable (band1/band2/band3)."""
    global band1, band2, band3
    if band_id == 1:
        band1 = value
    elif band_id == 2:
        band2 = value
    else:
        band3 = value


def refresh_band_value(bars: np.ndarray, band_id: int) -> None:
    """Recompute one band's level from its sliders; update text and overlay (main thread)."""
    if not bands_enabled[band_id]:
        return
    f_start = float(dpg.get_value(f"band{band_id}_start"))
    f_end = float(dpg.get_value(f"band{band_id}_end"))
    l_min = float(dpg.get_value(f"band{band_id}_min"))
    l_max = float(dpg.get_value(f"band{band_id}_max"))
    value = band_value_from_bars(bars, f_start, f_end, l_min, l_max)
    _set_band_variable(band_id, value)
    # Beat trigger: any band rising to >= 1.0 flashes its LED; only band 1 can
    # drive the sequencer beat (edge only) — bands 2/3 stay spectrum-only (e10s07)
    if value >= 1.0 and band_prev_values[band_id] < 1.0:
        flash_led(f"led_band{band_id}")
        if band_id == 1 and beat_source == BEAT_SOURCE_BAND1:
            sync_event_beat.set()
    band_prev_values[band_id] = value
    dpg.set_value(f"band{band_id}_value_text", f"{value:.2f}")
    dpg.configure_item(
        f"band{band_id}_rect",
        pmin=(f_start * SPEC_DRAWLIST_W, (1 - l_max) * SPEC_DRAWLIST_H),
        pmax=(f_end * SPEC_DRAWLIST_W, (1 - l_min) * SPEC_DRAWLIST_H),
        show=True,
    )


def refresh_bands(bars: np.ndarray) -> None:
    """Refresh every enabled band (disabled bands stay 0 and hidden)."""
    for band_id in bands_enabled:
        if bands_enabled[band_id]:
            refresh_band_value(bars, band_id)


def update_spectrum_ui(bars: np.ndarray) -> None:
    """Redraw the spectrum bars and refresh the enabled bands (main thread)."""
    global spectrum_bars_cache
    n = len(bars)
    bw = SPEC_DRAWLIST_W / n
    for i, level in enumerate(bars):
        h = level * (SPEC_DRAWLIST_H - 4)
        dpg.configure_item(
            f"spec_bar_{i}",
            pmin=(i * bw + 1, SPEC_DRAWLIST_H - h),
            pmax=((i + 1) * bw - 1, SPEC_DRAWLIST_H - 2),
        )
    spectrum_bars_cache = bars
    refresh_bands(bars)


def on_band_enable(sender: Any, app_data: Any, user_data: Any) -> None:
    """Show/hide a band's overlay and (re)compute it when the checkbox toggles."""
    band_id = int(user_data)
    bands_enabled[band_id] = bool(app_data)
    if bands_enabled[band_id]:
        refresh_band_value(spectrum_bars_cache, band_id)
    else:
        _set_band_variable(band_id, 0.0)
        dpg.set_value(f"band{band_id}_value_text", "—")
        dpg.configure_item(f"band{band_id}_rect", show=False)


def on_band_change(sender: Any, app_data: Any, user_data: Any) -> None:
    """Refresh a band when its selection sliders move."""
    refresh_band_value(spectrum_bars_cache, int(user_data))


def spectrum_analyzer_loop() -> None:
    """Compute the spectrum ~30x/s and enqueue the main-thread redraw (HIGH-1)."""
    while True:
        if is_audio_analyzing:
            try:
                bars = compute_spectrum_bars(get_audio_snapshot())
                ui_task(partial(update_spectrum_ui, bars))
            except Exception as e:
                log_error("Spectrum", str(e))
        time.sleep(1.0 / SPECTRUM_FPS)


def essentia_analyzer_loop() -> None:
    global current_bpm, beat_confidence
    last_error = ""
    while True:
        if is_beat_tracking and beat_source == BEAT_SOURCE_ANALYSIS:
            try:
                audio_slice = essentia.array(get_audio_snapshot())
                if np.max(np.abs(audio_slice)) > 0.005:
                    if lowpass_enabled:
                        audio_slice = lowpass_filter(audio_slice)
                    bpm, _, confidence, _, _ = rhythm_extractor(audio_slice)
                    if confidence > 0.2 or beat_confidence == 0.0:
                        current_bpm = float(bpm)
                        beat_confidence = float(confidence)
                        enqueue_set_value(
                            "testo_bpm", f"BPM: {current_bpm:.1f} (Conf: {beat_confidence:.2f})"
                        )
            except Exception as e:
                # Log each distinct failure once, not every second
                err = f"{type(e).__name__}: {e}"
                if err != last_error:
                    last_error = err
                    log_error("BPM analysis", err)
        time.sleep(1.0)


def visual_metronome_loop() -> None:
    global phase_nudge
    while True:
        if is_beat_tracking and current_bpm > 0 and not is_playing:
            base_sleep = 60.0 / current_bpm
            actual_sleep = max(0.0, base_sleep + phase_nudge)
            led_tag = BEAT_LED_TAGS.get(beat_source)
            if led_tag:
                flash_led(led_tag)
            sync_event_led.wait(actual_sleep)
            if sync_event_led.is_set():
                sync_event_led.clear()
            phase_nudge = 0.0
        else:
            time.sleep(0.1)


# ==============================================================================
# NEW ASYNC THREAD FOR HIGH-RESOLUTION FADES
# ==============================================================================
def fade_tick_loop() -> None:
    while True:
        if is_playing:
            current_time = time.time()
            for track in tracks_data:
                fade = track.get("active_fade", {})
                if fade and fade.get("active"):
                    elapsed = current_time - fade["start_time"]
                    expected_msg_index = int(elapsed / fade["msg_interval"])

                    # If we fell behind, or it is time for the next tick
                    if expected_msg_index > fade["last_msg_index"]:
                        max_msg = min(expected_msg_index, fade["total_msgs"] - 1)

                        # Send all the accumulated intermediate messages
                        for i in range(fade["last_msg_index"] + 1, max_msg + 1):
                            progress = (
                                i / float(fade["total_msgs"] - 1) if fade["total_msgs"] > 1 else 1.0
                            )
                            val = (
                                fade["start_val"] + (fade["end_val"] - fade["start_val"]) * progress
                            )
                            try:
                                osc_client.send_message(fade["address"], float(val))
                                append_log("OUT", f"{fade['address']} [FADE: {val:.2f}]")
                            except Exception:
                                pass

                        fade["last_msg_index"] = max_msg

                        # Deactivate when the fade is finished
                        if fade["last_msg_index"] >= fade["total_msgs"] - 1:
                            fade["active"] = False
        time.sleep(0.01)  # 100 FPS check loop for smooth fades


def send_colorv_step(track: dict[str, Any], row: int, col: int) -> None:
    """Send the picked RGB (0..1) for a ColorV step (HIGH-1 safe)."""
    target_addr = f"{track['base_address']}/color"
    r_val, g_val, b_val = [float(c) for c in track["steps"][col]["color"]]
    osc_client.send_message(target_addr, [r_val, g_val, b_val])
    append_log("OUT", f"{target_addr} [{r_val:.2f}, {g_val:.2f}, {b_val:.2f}]")


def send_colorr_step(track: dict[str, Any], row: int, col: int) -> None:
    """Send a random RGB for a ColorR step and show it in the step's color square."""
    target_addr = f"{track['base_address']}/color"
    r_val, g_val, b_val = (
        random.uniform(0.0, 1.0),
        random.uniform(0.0, 1.0),
        random.uniform(0.0, 1.0),
    )
    osc_client.send_message(target_addr, [r_val, g_val, b_val])
    append_log("OUT", f"{target_addr} [{r_val:.2f}, {g_val:.2f}, {b_val:.2f}]")

    step_data = track["steps"][col]
    step_data["last_rand_color"] = [r_val, g_val, b_val]
    tag_color = f"rand_color_{row}_{col}"
    enqueue_set_value(tag_color, dpg_color_rgba(step_data["last_rand_color"]))


def send_seekr_step(track: dict[str, Any], row: int, col: int) -> None:
    """Send a random seek (0..1) for a SeekR step and show the value in the cell."""
    target_addr = f"{track['base_address']}/seek"
    rand_val = random.uniform(0.0, 1.0)
    osc_client.send_message(target_addr, float(rand_val))
    append_log("OUT", f"{target_addr} [{rand_val:.2f}]")

    step_data = track["steps"][col]
    step_data["last_rand_seek"] = rand_val
    tag_seek = f"rand_seek_{row}_{col}"
    enqueue_set_value(tag_seek, f"{rand_val:.2f}")


def beat_is_event_driven() -> bool:
    """True when the beat comes from an event (band 1 peak / MIDI clock), not a fixed interval."""
    return beat_source in (BEAT_SOURCE_BAND1, BEAT_SOURCE_MIDI)


def sequencer_tick() -> None:
    global current_step, phase_nudge
    while True:
        if is_playing:
            if beat_is_event_driven():
                # Band/MIDI modes: wait for the beat event. The wait is polled so a
                # beat-source switch or STOP always breaks through — an unbounded wait
                # strands the tick thread in a mode that no longer fires (BUG-2026-08-27T213000).
                if not sync_event_beat.wait(0.1):
                    continue  # no beat this poll: re-evaluate mode/stop
                sync_event_beat.clear()
                phase_nudge = 0.0
            else:
                base_sleep = 60.0 / current_bpm if current_bpm > 0 else 0.5
                actual_sleep = max(0.0, base_sleep + phase_nudge)
                phase_nudge = 0.0
                sync_event_seq.wait(actual_sleep)
                if sync_event_seq.is_set():
                    sync_event_seq.clear()

            prev_step = current_step
            current_step = (current_step + 1) % NUM_STEPS
            launchpad_flash_playhead()  # e09s03: beat flash on the new playhead column

            for r, track in enumerate(tracks_data):
                if prev_step != -1:
                    update_step_theme(r, prev_step, is_head=False)
                update_step_theme(r, current_step, is_head=True)

                step_data = track["steps"][current_step]

                if step_data["active"]:
                    # A new step cancels any pending fade unless it starts its own (audit HIGH-2)
                    track["active_fade"]["active"] = False
                    base_addr = track["base_address"]
                    if base_addr and base_addr.strip():
                        try:
                            if step_data["type"] == "AlphaV":
                                target_addr = f"{base_addr}/alpha"
                                osc_client.send_message(target_addr, float(step_data["v1"]))
                                append_log("OUT", f"{target_addr} [{step_data['v1']:.2f}]")

                            elif step_data["type"] == "AlphaR":
                                target_addr = f"{base_addr}/alpha"
                                rand_val = random.uniform(0.0, 1.0)
                                osc_client.send_message(target_addr, float(rand_val))
                                append_log("OUT", f"{target_addr} [{rand_val:.2f}]")

                                step_data["last_rand_v1"] = rand_val
                                tag_v1 = f"rand_v1_{r}_{current_step}"
                                enqueue_set_value(tag_v1, f"{rand_val:.2f}")

                            elif step_data["type"] == "AlphaF":
                                target_addr = f"{base_addr}/alpha"
                                total_msgs = step_data["frames"] * step_data["msgs"]

                                # Start the asynchronous state machine
                                track["active_fade"] = {
                                    "active": True,
                                    "address": target_addr,
                                    "start_val": step_data["v1"],
                                    "end_val": step_data["v2"],
                                    "total_msgs": total_msgs,
                                    "msg_interval": base_sleep / step_data["msgs"]
                                    if step_data["msgs"] > 0
                                    else base_sleep,
                                    "start_time": time.time(),
                                    "last_msg_index": 0,
                                }
                                # The sequencer sends the FIRST value immediately
                                osc_client.send_message(target_addr, float(step_data["v1"]))
                                append_log(
                                    "OUT", f"{target_addr} [FADE START: {step_data['v1']:.2f}]"
                                )

                            elif step_data["type"] == "ColorV":
                                send_colorv_step(track, r, current_step)

                            elif step_data["type"] == "ColorR":
                                send_colorr_step(track, r, current_step)

                            elif step_data["type"] == "SeekR":
                                send_seekr_step(track, r, current_step)

                        except Exception as e:
                            print(f"[viseq OSC Error] {e}")

            led_tag = BEAT_LED_TAGS.get(beat_source)
            if led_tag:
                flash_led(led_tag)
        else:
            time.sleep(0.1)


def on_lowpass_toggle(sender: Any, app_data: Any, user_data: Any) -> None:
    global lowpass_enabled
    lowpass_enabled = bool(app_data)


def toggle_audio_stream(sender: Any, app_data: Any, user_data: Any) -> None:
    global audio_stream, is_audio_analyzing, is_beat_tracking
    if user_data == "vu_meter":
        is_audio_analyzing = app_data
    elif user_data == "beat_tracking":
        is_beat_tracking = app_data
    needs_stream = is_audio_analyzing or is_beat_tracking

    if needs_stream and audio_stream is None:
        device_string = dpg.get_value("combo_devices")
        if "No input device" in device_string:
            dpg.set_value(sender, False)
            return
        device_id = int(device_string.split(":")[0])
        try:
            audio_stream = sd.InputStream(
                device=device_id,
                channels=1,
                samplerate=samplerate,
                dtype=np.float32,
                callback=audio_callback,
            )
            audio_stream.start()
        except Exception:
            dpg.set_value(sender, False)
    elif not needs_stream and audio_stream is not None:
        audio_stream.stop()
        audio_stream.close()
        audio_stream = None
        dpg.set_value("testo_bpm", "BPM: ---")


def toggle_play(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    global is_playing, current_step
    is_playing = not is_playing
    if not is_playing:
        for r in range(NUM_TRACKS):
            for c in range(NUM_STEPS):
                update_step_theme(r, c, is_head=False)
            tracks_data[r]["active_fade"]["active"] = False
        current_step = -1
        dpg.set_item_label("btn_play", "PLAY")
    else:
        dpg.set_item_label("btn_play", "STOP")
        sync_event_seq.set()


dpg.create_context()

with dpg.texture_registry(tag="texture_registry"):
    pass

# e08: monospace font for the About-window ASCII logo (guarded: no font file -> None means
# the logo falls back to the default proportional font). Built right after create_context.
for _help_mono_font_path in _HELP_MONO_FONT_PATHS:
    if os.path.exists(_help_mono_font_path):
        with dpg.font_registry():
            _help_mono_font = dpg.add_font(_help_mono_font_path, size=13)
        break

with dpg.handler_registry():
    # DPG 2.3.1 key handlers have no modifier support: the wrapper checks Ctrl itself
    dpg.add_key_press_handler(dpg.mvKey_C, callback=_on_copy_key)
    dpg.add_key_press_handler(dpg.mvKey_V, callback=_on_paste_key)


# e10s06: one click-handler registry per Mediagrid tile. DPG 2.x item handlers
# live in an item_handler_registry bound to the item(s) they watch; child windows
# cannot host a clicked handler (verified against 2.3.1), so the registry is bound
# to the tile's clickable children (title, thumbnail, badge, alpha).
def media_tile_click_registry_tag(target_id: str) -> str:
    return f"click_reg_{target_id}"


def _bind_tile_click_targets(click_reg_tag: str, *item_tags: str | None) -> None:
    """Bind one tile's click registry to every clickable child that exists."""
    for item_tag in item_tags:
        if item_tag and dpg.does_item_exist(item_tag):
            dpg.bind_item_handler_registry(item_tag, click_reg_tag)


with dpg.theme() as theme_selected_clip, dpg.theme_component(dpg.mvChildWindow):
    theme_color(dpg.mvThemeCol_Border, "border_active")
    theme_color(dpg.mvThemeCol_ChildBg, "accent_bg")
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_normal_clip, dpg.theme_component(dpg.mvChildWindow):
    theme_color(dpg.mvThemeCol_Border, "border")
    theme_color(dpg.mvThemeCol_ChildBg, "panel_bg")
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_vimix_current_clip, dpg.theme_component(dpg.mvChildWindow):
    # Vimix's current source in a lighter, non-green border (e10s06): clearly
    # distinct from the green viseq primary selection and the plain tile.
    theme_color(dpg.mvThemeCol_Border, "text_dim")
    theme_color(dpg.mvThemeCol_ChildBg, "panel_bg")
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_compact_table, dpg.theme_component(dpg.mvTable):
    dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 1, 1)

with dpg.theme() as theme_cell_off, dpg.theme_component(dpg.mvChildWindow):
    theme_color(dpg.mvThemeCol_Border, "border")
    theme_color(dpg.mvThemeCol_ChildBg, "panel_bg")
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_cell_on, dpg.theme_component(dpg.mvChildWindow):
    theme_color(dpg.mvThemeCol_Border, "border_active")
    theme_color(dpg.mvThemeCol_ChildBg, "accent_bg")
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_cell_play_off, dpg.theme_component(dpg.mvChildWindow):
    theme_color(dpg.mvThemeCol_Border, "text_bright")
    theme_color(dpg.mvThemeCol_ChildBg, "play_bg")
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_cell_play_on, dpg.theme_component(dpg.mvChildWindow):
    theme_color(dpg.mvThemeCol_Border, "text_bright")
    theme_color(dpg.mvThemeCol_ChildBg, "play_on_bg")
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_slot_clear, dpg.theme_component(dpg.mvChildWindow):
    # borderless clip slot: no frame, no background (border=False + transparent ChildBg)
    dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (0, 0, 0, 0))

with dpg.theme() as theme_media_badge, dpg.theme_component(dpg.mvButton):
    # Mediagrid index badge: a slate box with a bright digit (e06 palette)
    theme_color(dpg.mvThemeCol_Button, "badge_bg")
    theme_color(dpg.mvThemeCol_ButtonHovered, "badge_bg")
    theme_color(dpg.mvThemeCol_ButtonActive, "badge_bg")
    theme_color(dpg.mvThemeCol_Text, "text_bright")
    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 2)

with dpg.theme() as theme_step_copied, dpg.theme_component(dpg.mvChildWindow):
    # copied-step highlight: warm border on the source cell (e08); the bg stays dark in
    # every theme because the flash state must stand out on both light and dark panels
    theme_color(dpg.mvThemeCol_Border, "warning")
    dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (40, 40, 30, 255))
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)


# WINDOW 1: SEQUENCER (explicit tag so the layout save/restore can address it, e06)
with dpg.window(
    label="Step Sequencer",
    width=1050,
    height=800,
    pos=(10, 10),
    no_close=True,
    tag="sequencer_window",
):
    with dpg.group(horizontal=True):
        dpg.add_button(
            label="PLAY",
            tag="btn_play",
            callback=learnable(toggle_play, lambda ud: (MIDI_ACTION_TRANSPORT_PLAY, {})),
            width=100,
            height=28,
        )
        dpg.add_spacer(width=14)
        dpg.add_button(
            label="<",
            callback=learnable(callback_nudge_backward, lambda ud: (MIDI_ACTION_NUDGE_BACK, {})),
            width=36,
            height=28,
        )
        dpg.add_button(
            label="RESYNC",
            callback=learnable(callback_resync, lambda ud: (MIDI_ACTION_TRANSPORT_RESYNC, {})),
            width=72,
            height=28,
        )
        dpg.add_button(
            label=">",
            callback=learnable(callback_nudge_forward, lambda ud: (MIDI_ACTION_NUDGE_FORWARD, {})),
            width=36,
            height=28,
        )
        dpg.add_spacer(width=14)
        # Beat source line 1: BPM detection (with its BPM readout) + band 1
        dpg.add_checkbox(
            label=BEAT_SOURCE_LABELS[BEAT_SOURCE_ANALYSIS],
            tag="cb_beat_bpm_analysis",
            default_value=True,
            callback=learnable(on_beat_source, lambda ud: (MIDI_ACTION_BEAT_SOURCE, {"mode": ud})),
            user_data=BEAT_SOURCE_ANALYSIS,
        )
        with dpg.drawlist(width=14, height=14):
            dpg.draw_circle(
                center=[7, 7],
                radius=5,
                color=(0, 0, 0, 255),
                fill=(50, 50, 50, 255),
                tag="led_analysis",
            )
        dpg.add_text("BPM: ---", tag="testo_bpm")
        dpg.add_spacer(width=10)
        dpg.add_checkbox(
            label="Beat Band 1",
            tag=f"cb_beat_{BEAT_SOURCE_BAND1}",
            callback=learnable(on_beat_source, lambda ud: (MIDI_ACTION_BEAT_SOURCE, {"mode": ud})),
            user_data=BEAT_SOURCE_BAND1,
        )
        with dpg.drawlist(width=14, height=14):
            dpg.draw_circle(
                center=[7, 7],
                radius=5,
                color=(0, 0, 0, 255),
                fill=(50, 50, 50, 255),
                tag="led_band1",
            )
        dpg.add_spacer(width=10)

    # Beat source line 2: MIDI + manual (with the numeric input and TAP).
    # The leading spacer aligns it under line 1 (transport width: PLAY+sp+<+RESYNC+>+sp).
    with dpg.group(horizontal=True):
        dpg.add_spacer(width=SEQ_TRANSPORT_WIDTH)
        dpg.add_checkbox(
            label="MIDI Sync",
            tag=f"cb_beat_{BEAT_SOURCE_MIDI}",
            callback=learnable(on_beat_source, lambda ud: (MIDI_ACTION_BEAT_SOURCE, {"mode": ud})),
            user_data=BEAT_SOURCE_MIDI,
        )
        with dpg.drawlist(width=14, height=14):
            dpg.draw_circle(
                center=[7, 7],
                radius=5,
                color=(0, 0, 0, 255),
                fill=(50, 50, 50, 255),
                tag="led_midi",
            )
        dpg.add_spacer(width=10)
        dpg.add_checkbox(
            label=BEAT_SOURCE_LABELS[BEAT_SOURCE_MANUAL],
            tag="cb_beat_manual_bpm",
            callback=learnable(on_beat_source, lambda ud: (MIDI_ACTION_BEAT_SOURCE, {"mode": ud})),
            user_data=BEAT_SOURCE_MANUAL,
        )
        with dpg.drawlist(width=14, height=14):
            dpg.draw_circle(
                center=[7, 7],
                radius=5,
                color=(0, 0, 0, 255),
                fill=(50, 50, 50, 255),
                tag="led_manual",
            )
        dpg.add_spacer(width=6)
        dpg.add_input_int(
            default_value=120,
            min_value=30,
            max_value=300,
            width=84,
            tag="manual_bpm_input",
            callback=on_manual_bpm,
            show=False,
        )
        dpg.add_button(
            label="TAP",
            tag="btn_tap",
            callback=learnable(tap_bpm, lambda ud: (MIDI_ACTION_TRANSPORT_TAP, {})),
            width=36,
            height=22,
            show=False,
        )
        dpg.add_text("", tag="manual_bpm_text", color=(150, 255, 150, 255))

    dpg.add_spacer(height=10)

    with dpg.table(
        header_row=False,
        borders_innerH=False,
        borders_innerV=False,
        borders_outerH=False,
        borders_outerV=False,
        scrollX=True,
        scrollY=True,
        policy=dpg.mvTable_SizingFixedFit,
        tag="seq_table",
    ):
        for _ in range(NUM_STEPS):
            dpg.add_table_column(width_fixed=True, init_width_or_weight=90)
        dpg.add_table_column(width_fixed=True, init_width_or_weight=SLOT_WIDTH)

        for row in range(NUM_TRACKS):
            with dpg.table_row():
                # THE 8 PADS
                for step in range(NUM_STEPS):
                    cell_tag = f"seq_cell_{row}_{step}"
                    with dpg.child_window(
                        width=STEP_CELL_SIZE, height=STEP_CELL_SIZE, tag=cell_tag, no_scrollbar=True
                    ):
                        pass
                    update_step_ui(row, step)

                # ASSIGNABLE CLIP SLOT (bare centered button, no table frame)
                with dpg.child_window(
                    width=SLOT_WIDTH,
                    height=SLOT_HEIGHT,
                    border=False,
                    tag=f"seq_slot_{row}",
                    no_scrollbar=True,
                ):
                    pass
                dpg.bind_item_theme(f"seq_slot_{row}", theme_slot_clear)
                update_track_slot_ui(row)

    dpg.bind_item_theme("seq_table", theme_compact_table)

# WINDOW 2: AUDIO ANALYZER
input_devices_list = get_input_devices()

with dpg.window(
    label="Audio analyzer",
    width=350,
    height=272,
    pos=(10, 806),
    no_close=True,
    tag="audio_window",
):
    dpg.add_combo(
        items=input_devices_list, default_value=input_devices_list[0], tag="combo_devices", width=-1
    )
    dpg.add_checkbox(
        label="Enable Level Analysis (Spectrum)",
        callback=toggle_audio_stream,
        user_data="vu_meter",
    )
    dpg.add_spacer(height=2)
    with dpg.drawlist(width=SPEC_DRAWLIST_W, height=SPEC_DRAWLIST_H, tag="spec_drawlist"):
        for i in range(SPECTRUM_BARS):
            themed_draw_rectangle(
                pmin=(i * (SPEC_DRAWLIST_W / SPECTRUM_BARS) + 1, SPEC_DRAWLIST_H - 2),
                pmax=((i + 1) * (SPEC_DRAWLIST_W / SPECTRUM_BARS) - 1, SPEC_DRAWLIST_H - 2),
                color=(0, 0, 0, 0),
                slot="spectrum",
                tag=f"spec_bar_{i}",
            )
        for band_id, (fill, edge) in BAND_RECT_COLORS.items():
            dpg.draw_rectangle(
                pmin=(0, 2),
                pmax=(SPEC_DRAWLIST_W, SPEC_DRAWLIST_H - 2),
                color=edge,
                fill=fill,
                tag=f"band{band_id}_rect",
                show=False,
            )
    for band_id, (start_default, end_default) in BAND_DEFAULT_RANGES.items():
        with dpg.group(horizontal=True):
            dpg.add_checkbox(
                label=f"Band {band_id}",
                tag=f"band{band_id}_enabled",
                callback=on_band_enable,
                user_data=band_id,
            )
            themed_text("F", slot="text")
            dpg.add_drag_float(
                default_value=start_default,
                min_value=0.0,
                max_value=0.99,
                speed=0.005,
                format="%.2f",
                width=44,
                tag=f"band{band_id}_start",
                callback=on_band_change,
                user_data=band_id,
            )
            dpg.add_drag_float(
                default_value=end_default,
                min_value=0.01,
                max_value=1.0,
                speed=0.005,
                format="%.2f",
                width=44,
                tag=f"band{band_id}_end",
                callback=on_band_change,
                user_data=band_id,
            )
            themed_text("L", slot="text")
            dpg.add_drag_float(
                default_value=0.0,
                min_value=0.0,
                max_value=1.0,
                speed=0.005,
                format="%.2f",
                width=44,
                tag=f"band{band_id}_min",
                callback=on_band_change,
                user_data=band_id,
            )
            dpg.add_drag_float(
                default_value=1.0,
                min_value=0.0,
                max_value=1.0,
                speed=0.005,
                format="%.2f",
                width=44,
                tag=f"band{band_id}_max",
                callback=on_band_change,
                user_data=band_id,
            )
            dpg.add_text("—", tag=f"band{band_id}_value_text", color=(230, 230, 120, 255))
    with dpg.group(horizontal=True):
        dpg.add_checkbox(
            label="Enable BPM Analysis (Essentia)",
            callback=toggle_audio_stream,
            user_data="beat_tracking",
        )
    dpg.add_checkbox(
        label="Use Low-Pass Filter (kick only)",
        default_value=True,
        tag="cb_lowpass",
        callback=on_lowpass_toggle,
    )

# WINDOW 3: SETTINGS (hidden; opened from the menubar "Settings" entry)
with dpg.window(
    label="Settings", width=340, height=320, pos=(370, 820), tag="settings_window", show=False
):
    themed_text("OSC", slot="text")
    dpg.add_separator()
    dpg.add_text("1. Setup Client (to viOSC):")
    with dpg.group(horizontal=True):
        dpg.add_input_text(default_value="127.0.0.1", tag="viosc_ip", width=120)
        dpg.add_input_int(default_value=6666, tag="viosc_port", width=80, step=0)
        dpg.add_button(label="Connect Client", callback=connect_to_viosc)
    themed_text("Client Status: Waiting", slot="text_dim", tag="viosc_status")
    dpg.add_separator()
    dpg.add_spacer(height=5)
    dpg.add_text("2. Setup Server (Listening):")
    with dpg.group(horizontal=True):
        dpg.add_input_text(default_value="127.0.0.1", tag="listen_ip", width=120)
        dpg.add_input_int(default_value=VIOSC_LISTEN_PORT, tag="listen_port", width=80, step=0)
        dpg.add_button(label="Start Server", tag="btn_server_toggle", callback=toggle_local_server)
    themed_text("Server Status: Stopped", slot="text_dim", tag="server_status")
    dpg.add_separator()
    dpg.add_spacer(height=5)

    with dpg.group(tag="vimix_raw_group"):
        pass

    # --- Windows section (e06s01): window layout save/restore ---
    dpg.add_spacer(height=8)
    themed_text("Windows", slot="text")
    dpg.add_separator()
    with dpg.group(horizontal=True):
        dpg.add_button(label="Save layout", callback=save_layout_to_config, width=110)
        dpg.add_button(label="Restore layout", callback=restore_layout_from_config, width=130)
    dpg.add_checkbox(
        label="Restore at startup",
        tag="cb_restore_layout_boot",
        default_value=True,
        callback=on_restore_layout_boot_toggle,
    )
    dpg.add_spacer(height=8)

    # --- Tema section (e06s02): preset combo + five custom color pickers ---
    themed_text("Theme", slot="text")
    dpg.add_separator()
    dpg.add_combo(
        items=["Dark", "Light", "Custom"],
        default_value="Dark",
        tag="theme_preset",
        width=150,
        callback=on_theme_preset,
    )
    for slot in THEME_PRIMARY_SLOTS:
        dpg.add_color_edit(
            label=THEME_PRIMARY_LABELS[slot],
            default_value=palette_rgba(active_palette[slot]),
            tag=f"theme_color_{slot}",
            width=170,
            callback=on_theme_color,
            user_data=slot,
        )

# WINDOW 4: VIMIX MEDIA
with (
    dpg.window(
        label="Mediagrid",
        width=550,
        height=690,
        pos=(1100, 10),
        no_close=True,
        tag="vimix_media_window",
    ),
    dpg.group(tag="vimix_media_group"),
):
    pass

# WINDOW 5: OSC LOGS (hidden; opened from the menubar "Show" > "Logs")
with dpg.window(
    label="OSC Logs", width=950, height=150, pos=(720, 820), tag="logs_window", show=False
):
    dpg.add_text("Waiting for OSC traffic...", tag="osc_log_text")

# WINDOW 6: HELP / ABOUT (hidden; opened from the menubar "Help", re-centered on open, e08)
with dpg.window(
    label="Help",
    width=HELP_WINDOW_WIDTH,
    height=HELP_WINDOW_HEIGHT,
    pos=(0, 0),
    tag="help_window",
    show=False,
):
    dpg.add_spacer(height=8)
    with dpg.group(horizontal=True):
        dpg.add_spacer(width=HELP_LOGO_INDENT)
        dpg.add_text(HELP_ASCII_LOGO, tag="help_logo_text")
        if _help_mono_font is not None:
            # DPG 2.3.1: bind_item_font(item, font); bind_font() only takes a global font.
            dpg.bind_item_font("help_logo_text", _help_mono_font)
    dpg.add_spacer(height=6)
    themed_text("viSeq — Audio-Reactive VJ Controller for Vimix", slot="text_bright")
    dpg.add_separator()
    themed_text(f"Version: {APP_VERSION}", slot="text")
    themed_text("License: GPL-3.0", slot="text")
    themed_text("Created by: Luca Franceschini aka Lupin3rd", slot="text")
    dpg.add_button(label=f"GitHub: {GITHUB_URL}", callback=open_github)

# WINDOW 7: MIDI (hidden; opened from the menubar "MIDI"). ALL MIDI features live here:
# enable toggle, controller selection, MIDI Learn and the mappings list (e09s02, user
# revision — no scattered menu items).
try:
    import mido as _mido

    _midi_input_devices: list[str] = list(_mido.get_input_names())
except Exception:
    _midi_input_devices = []

with dpg.window(label="MIDI", width=460, height=470, pos=(560, 320), tag="midi_window", show=False):
    dpg.add_checkbox(
        label="Enable MIDI",
        tag="midi_enable_cb",
        default_value=midi_enabled,
        callback=on_midi_enable,
    )
    dpg.add_separator()
    dpg.add_spacer(height=4)
    themed_text("Controller", slot="text")
    with dpg.group(horizontal=True):
        dpg.add_combo(
            items=_midi_input_devices,
            default_value=(
                midi_input_port or (_midi_input_devices[0] if _midi_input_devices else "")
            ),
            tag="midi_input_combo",
            width=320,
            callback=on_midi_input_port,
        )
        dpg.add_button(label="Refresh", callback=refresh_midi_devices, width=80)
    dpg.add_separator()
    dpg.add_spacer(height=4)
    themed_text("MIDI Learn", slot="text")
    dpg.add_button(
        label="Learn mapping...", tag="midi_learn_btn", callback=toggle_midi_learn, width=150
    )
    dpg.add_text("", tag="midi_learn_status")
    dpg.add_separator()
    dpg.add_spacer(height=4)
    themed_text("Mappings", slot="text")
    with (
        dpg.child_window(height=180, tag="midi_mappings_scroll"),
        dpg.group(tag="midi_mappings_group"),
    ):
        pass
    dpg.add_spacer(height=4)
    dpg.add_button(label="Save", callback=save_midi_bindings, width=80)

# NEW THREAD FOR HIGH-FREQUENCY FADES
threading.Thread(target=fade_tick_loop, daemon=True).start()
threading.Thread(target=spectrum_analyzer_loop, daemon=True).start()
threading.Thread(target=midi_clock_loop, daemon=True).start()
threading.Thread(target=midi_control_loop, daemon=True).start()  # e09: control worker

threading.Thread(target=sequencer_tick, daemon=True).start()
threading.Thread(target=visual_metronome_loop, daemon=True).start()
threading.Thread(target=essentia_analyzer_loop, daemon=True).start()
threading.Thread(target=thumbnail_decoder_worker, daemon=True).start()

dpg.create_viewport(title="viSeq - Audio-Reactive VJ Controller", width=1700, height=1080)
apply_boot_config()  # e06: apply the saved theme + (optionally) the saved window layout
with dpg.viewport_menu_bar():
    with dpg.menu(label="Monitor"):
        dpg.add_menu_item(label="New Monitor Player", callback=new_monitor_player)
    with dpg.menu(label="Show"):
        dpg.add_menu_item(label="Logs", callback=show_logs_window)
    dpg.add_menu_item(label="Settings", callback=show_settings_window)
    dpg.add_menu_item(label="MIDI", callback=show_midi_window)
    dpg.add_menu_item(label="Help", callback=show_help_window)
dpg.setup_dearpygui()
dpg.show_viewport()
autostart_osc()  # boot: auto-connect OSC client + start listening server (no manual clicks)

try:
    while dpg.is_dearpygui_running():
        if dpg.does_item_exist("vimix_media_window"):
            w = dpg.get_item_width("vimix_media_window")
            current_cols = max(1, int((w - 20) / 145))
            if current_cols != last_num_cols:
                last_num_cols = current_cols
                if global_vimix_state.get("sources"):
                    update_vimix_sources_ui(json.dumps(global_vimix_state))

        has_new_logs = False
        while not log_queue.empty():
            osc_log_history.append(log_queue.get())
            has_new_logs = True

        if has_new_logs:
            if len(osc_log_history) > LOG_HISTORY_LIMIT:
                del osc_log_history[:-LOG_HISTORY_LIMIT]
            if dpg.does_item_exist("osc_log_text"):
                dpg.set_value("osc_log_text", format_osc_log(osc_log_history))

        # Run queued UI mutations from worker threads on the main thread (audit HIGH-1)
        while not ui_task_queue.empty():
            task = ui_task_queue.get()
            try:
                task()
            except Exception as e:
                log_error("UI task", str(e))

        latest_json = None
        while not ui_state_queue.empty():
            latest_json = ui_state_queue.get()

        if latest_json:
            update_vimix_sources_ui(latest_json)

        while not texture_queue.empty():
            name, idx, img_data, w, h = texture_queue.get()
            apply_thumbnail_texture(name, idx, img_data, w, h)

        tick_thumb_cycle(time.time())

        request_missing_thumbnails(time.time())

        # monitor players: cleanup closed windows and refresh values
        for p in list(monitor_players):
            if not dpg.does_item_exist(p["tag"]):
                if p.get("target_id"):
                    addr = f"/viosc/monitor/{p['target_id']}"
                    osc_client.send_message(addr, [])
                    append_log("OUT", f"{addr} (stop)")
                monitor_players.remove(p)
                continue
            refresh_monitor_display(p["id"])

        dpg.render_dearpygui_frame()
        time.sleep(frame_sleep())  # perf e07 P1: throttle the idle render cadence
finally:
    # L-4 clean exit: stop audio, shut down the OSC server, destroy the context
    if audio_stream is not None:
        with contextlib.suppress(Exception):
            audio_stream.stop()
            audio_stream.close()
    if local_osc_server is not None:
        with contextlib.suppress(Exception):
            local_osc_server.shutdown()
    dpg.destroy_context()
