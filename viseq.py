import contextlib
import copy
import json
import math
import os
import random
import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any

import dearpygui.dearpygui as dpg
import essentia
import numpy as np
import sounddevice as sd
from PIL import Image
from pythonosc import dispatcher, udp_client

import viseqapp  # noqa: F401  scaffold hook (REFACTOR_LATEST.md commit 1): proves the package import path works at boot
from viseqapp import mapper, state
from viseqapp.audio import (
    _set_band_variable,
    apply_spectrum_agc,
    audio_callback,
    band_value_from_bars,
    compute_spectrum_bars,
    get_audio_snapshot,
    lowpass_filter,
    rhythm_extractor,
)
from viseqapp.config import _sanitize_palette, load_config, save_config
from viseqapp.constants import (
    BAND_BEAT_THRESHOLD,
    BEAT_SOURCE_ANALYSIS,
    BEAT_SOURCE_BAND1,
    BEAT_SOURCE_LABELS,
    BEAT_SOURCE_MANUAL,
    BEAT_SOURCE_MIDI,
    DEFAULT_MANUAL_BPM,
    DEFAULT_PALETTE,
    FRAME_SLEEP_ANIMATED,
    FRAME_SLEEP_IDLE,
    HELP_ASCII_LOGO,
    HELP_LOGO_INDENT,
    HELP_WINDOW_HEIGHT,
    HELP_WINDOW_WIDTH,
    LAYOUT_ALWAYS_HIDDEN_TAGS,
    LAYOUT_WINDOW_TAGS,
    LOG_HISTORY_LIMIT,
    MAPPER_CARD_H,
    MAPPER_CARD_STRIDE,
    MAPPER_CARD_W,
    MAPPER_THUMB_H,
    MAPPER_THUMB_W,
    MAPPER_WINDOW_HEIGHT,
    MAPPER_WINDOW_WIDTH,
    MEDIA_BADGE_H,
    MEDIA_BADGE_W,
    MEDIA_TILE_H,
    MEDIA_TITLE_CHAR_PX,
    MEDIA_TITLE_ELLIPSIS,
    MEDIA_TITLE_MAX_LINES,
    MEDIA_TITLE_WRAP,
    MIDI_ACTION_BEAT_SOURCE,
    MIDI_ACTION_MAPPER_MAPPING,
    MIDI_ACTION_NUDGE_BACK,
    MIDI_ACTION_NUDGE_FORWARD,
    MIDI_ACTION_SEQ_TOGGLE,
    MIDI_ACTION_TRACK_ASSIGN,
    MIDI_ACTION_TRANSPORT_PLAY,
    MIDI_ACTION_TRANSPORT_RESYNC,
    MIDI_ACTION_TRANSPORT_TAP,
    MIDI_CLOCK_PULSES_PER_BEAT,
    MIDI_LEARN_TIMEOUT_SECONDS,
    MONITOR_ALPHA_W,
    MONITOR_DISC_R,
    MONITOR_DISC_RPM,
    MONITOR_DISC_SIZE,
    MONITOR_OFFSET,
    MONITOR_SEEK_W,
    MONITOR_SPEED_TEXT_SIZE,
    MONITOR_THUMB_H,
    MONITOR_THUMB_W,
    NUM_STEPS,
    NUM_TRACKS,
    PROJECT_FILE_EXTENSION,
    PROJECT_FORMAT,
    PROJECT_VERSION,
    RECENT_PROJECTS_MAX,
    SLOT_BUTTON_HEIGHT,
    SLOT_BUTTON_INDENT,
    SLOT_BUTTON_TOP_SPACER,
    SLOT_BUTTON_WIDTH,
    SLOT_HEIGHT,
    SLOT_WIDTH,
    SPEC_DRAWLIST_H,
    SPEC_DRAWLIST_W,
    SPECTRUM_BARS,
    SPECTRUM_FPS,
    STEP_CELL_SIZE,
    STEP_COLOR_SQUARE_INDENT,
    STEP_COLOR_SQUARE_SIZE,
    STEP_PERSISTED_KEYS,
    THEME_PRESET_LABELS,
    THEME_PRIMARY_LABELS,
    THEME_PRIMARY_SLOTS,
    THUMB_CYCLE_INTERVAL,
    THUMB_FAIL_LABEL,
    THUMB_FAIL_THRESHOLD,
    THUMB_REQUEST_INTERVAL,
    VIOSC_IP,
    VIOSC_LISTEN_PORT,
    VIOSC_PORT,
)
from viseqapp.midi import (
    _clock_port_name,
    _close_midi_input,
    _parse_midi_msg,
    available_controller_ports,
    binding_source_from_message,
    controller_connect,
    controller_disconnect,
    controller_profile_of,
    controller_profiles,
    find_controller_by_port,
    grid_controller,
    grid_flash_playhead,
    grid_mirror_step,
    midi_init_from_config,
    resolve_midi_message,
    save_midi_controllers,
    selected_bindings,
    set_midi_enabled,
)
from viseqapp.osc import (
    ALL_PROPERTIES,
    ViseqOSCUDPServer,
    find_player_index,
    find_source_by_name,
    get_current_target_id,
    incoming_osc_handler,
    osc_client,
    send_monitor_command,
    thumbnail_decoder_worker,
)
from viseqapp.palette import (
    _apply_theme_config,
    _preset_key,
    _set_media_cell,
    dpg_color_rgba,
    on_theme_color,
    on_theme_preset,
    palette_rgba,
    theme_color,
    themed_draw_rectangle,
    themed_text,
)
from viseqapp.profiles import (
    match_controller_profile,
)
from viseqapp.queues import append_log, enqueue_set_value, log_error, ui_task
from viseqapp.sequencer import (
    _timed_bpm_live,
    beat_is_event_driven,
    send_colorr_step,
    send_colorv_step,
    send_seekr_step,
)
from viseqapp.state import (
    _last_unmatched_log,
    _media_cell_cache,
    _midi_first_msg_logged,
    _pristine_track,
    _text_color_bindings,
    band_prev_values,
    bands_enabled,
    log_queue,
    midi_bindings,
    midi_controllers,
    monitor_players,
    osc_log_history,
    request_timestamps,
    samplerate,
    sync_event_beat,
    sync_event_led,
    sync_event_seq,
    tap_times,
    texture_queue,
    thumb_cycle_state,
    thumb_fail_count,
    thumbnails_data,
    tracks_data,
    ui_state_queue,
    ui_task_queue,
)

# --- HARD CAPS ON NETWORK-FED DATA (viOSC replies) ---
# Bound memory use and block PIL decompression bombs (audit MED-6).
Image.MAX_IMAGE_PIXELS = 25_000_000  # PIL's hard ceiling (~25 MP)

# Monitor player: compact graphical readout (e07)
DEFAULT_MONITOR_PROPS = ["alpha", "seek", "speed"]  # requested when a monitor starts

# --- OSC CONFIGURATION ---
# viseq talks exclusively to viOSC: /vimix/* messages are forwarded by viOSC
# to Vimix (port 7000), replies come back on viOSC's output port 6667.


# --- USER CONFIG + THEMING (e06) ---
# Single JSON file next to viseq.py stores the window layout and the theme. The storage
# mechanism was delegated to the agent by the user ("puoi decidere tu cosa utilizzare").
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))

# viseq application version — single source of truth (matches specs/release-plan.yaml, e08s02).
# e13s01: this is the first real release of viSeq (user decision).
# e20s03: 0.2.0 — viseqapp refactor + controller profiles + new project + Mapper family.
APP_VERSION: str = "0.2.0"

# Author's GitHub profile, shown as a link in the About window (e08s01, user request).
GITHUB_URL: str = "https://github.com/lupin3rd"

# e08: monospace font for the ASCII logo; the first existing path wins, None falls back to
# the default proportional font (cosmetic only — the logo then drifts off alignment).
_HELP_MONO_FONT_PATHS: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
)
_help_mono_font: Any = None

# --- e09: MIDI control engine (single mido stack; notes + CCs, user-configurable bindings) ---


# MIDI control runtime mirrors of cfg["midi"] (e09). The worker thread reads these; the
# main thread writes them. Bindings: [{device, channel, type("note"/"cc"), number,
# action, params}]. Learn flow state (e09s02): pending = (action, params) captured by a
# learnable widget click, awaiting the next incoming MIDI message.

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
# e14s04: the LAUNCHPAD_* constants moved into the controller profile files
# (controllers/*.json); the runtime uses GRID_LED_* + profile fields.

# ==============================================================================
# CONTROLLER PROFILES (e14) — the profiles system moved to viseqapp/profiles.py;
# viseq re-exports its public names (port matcher, loader, note formula).


# Runtime bindings (tag -> palette slot) recorded at widget creation so apply_palette() can
# re-theme live. Theme color items are updated via set_value; text/draw items via
# configure_item (verified against DPG 2.3.1 in the e06 spike probes).


# Global chrome theme components (bind_theme) and their palette slots; only bound for
# non-Scuro themes, so Scuro keeps the exact DPG dark defaults (legacy look).

# --- COMMUNICATION QUEUES ---


# ==============================================================================
# THEMING (e06s02)
# ==============================================================================


# Per-cell cache for the Mediagrid value updates (perf e07 P0): a viOSC state push that
# does not change a cell's displayed string skips the set_value entirely. Cleared whenever
# the tables are rebuilt, so freshly created widgets are never wrongly skipped.


# ==============================================================================
# USER CONFIG + WINDOW LAYOUT (e06s01)
# ==============================================================================


def _existing_layout_window_tags() -> list[str]:
    """Tags of every layout-tracked window currently present in the UI."""
    tags = [t for t in LAYOUT_WINDOW_TAGS if dpg.does_item_exist(t)]
    tags += [p["tag"] for p in monitor_players if dpg.does_item_exist(p["tag"])]
    return tags


def snapshot_window_layout() -> list[dict[str, Any]]:
    """Record shown/pos/size for every existing layout-tracked window (main thread only).

    LAYOUT_ALWAYS_HIDDEN_TAGS (the Settings window) are always recorded as closed: they
    stay open while the user saves a project, and must not come back at boot.
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


# ==============================================================================
# PROJECT SAVE/LOAD (e11) — .viseq files capture window layout + theme + every
# sequencer configuration; the viSeq menu (e11s03) drives the file dialogs.
# ==============================================================================
PROJECTS_DIR = os.path.join(CONFIG_DIR, "projects")
# Step fields that belong in a project file; the last_rand_* keys are runtime-only.


def _step_persisted(step: dict[str, Any]) -> dict[str, Any]:
    """Reduce a runtime step dict to its persisted fields (e11s01)."""
    return {key: copy.deepcopy(step[key]) for key in STEP_PERSISTED_KEYS if key in step}


def _capture_audio_state() -> dict[str, Any]:
    """Snapshot the audio-analyzer section from its widgets (e11s01)."""
    bands: dict[str, dict[str, Any]] = {}
    for band_id in BAND_DEFAULT_RANGES:
        bands[str(band_id)] = {
            "enabled": bool(dpg.get_value(f"band{band_id}_enabled")),
            "start": float(dpg.get_value(f"band{band_id}_start")),
            "end": float(dpg.get_value(f"band{band_id}_end")),
            "min": float(dpg.get_value(f"band{band_id}_min")),
            "max": float(dpg.get_value(f"band{band_id}_max")),
        }
    return {
        "device": str(dpg.get_value("combo_devices")),
        "lowpass": bool(dpg.get_value("cb_lowpass")),
        "bands": bands,
    }


def capture_project_state() -> dict[str, Any]:
    """Snapshot layout + theme + sequencer state into a project dict (e11s01)."""
    preset_label = str(dpg.get_value("theme_preset"))
    return {
        "layout": {"windows": snapshot_window_layout()},
        "theme": {
            "preset": _preset_key(preset_label),
            "colors": copy.deepcopy(state.active_palette),
        },
        "sequencer": {
            "beat_source": state.beat_source,
            "manual_bpm": float(dpg.get_value("manual_bpm_input")),
            "tracks": [
                {
                    "target_id": track.get("target_id"),
                    "base_address": track.get("base_address", ""),
                    "steps": [_step_persisted(step) for step in track["steps"]],
                }
                for track in tracks_data
            ],
            "audio": _capture_audio_state(),
        },
    }


def _restore_step(row: int, col: int, step_data: dict[str, Any]) -> None:
    """Apply one persisted step onto the live cell and rebuild its UI (e11s01)."""
    step = tracks_data[row]["steps"][col]
    for key in STEP_PERSISTED_KEYS:
        if key in step_data:
            step[key] = copy.deepcopy(step_data[key])
    update_step_ui(row, col)


def _restore_track(row: int, track_data: dict[str, Any]) -> None:
    """Apply one persisted track (clip assignment + steps) and rebuild its UI (e11s01)."""
    target_id = track_data.get("target_id")
    tracks_data[row]["target_id"] = target_id
    tracks_data[row]["base_address"] = f"/vimix/{target_id}" if target_id else ""
    for col, step_data in enumerate(track_data.get("steps", [])):
        if col >= NUM_STEPS:
            break
        _restore_step(row, col, step_data)
    update_track_slot_ui(row)


def _apply_audio_state(audio: dict[str, Any]) -> None:
    """Re-apply the audio-analyzer section (device, low-pass, bands) (e11s01)."""
    if audio.get("device") in input_devices_list:
        dpg.set_value("combo_devices", audio["device"])
    state.lowpass_enabled = bool(audio.get("lowpass", True))
    if dpg.does_item_exist("cb_lowpass"):
        dpg.set_value("cb_lowpass", state.lowpass_enabled)
    bands = audio.get("bands", {})
    for band_id in BAND_DEFAULT_RANGES:
        band = bands.get(str(band_id), {})
        bands_enabled[band_id] = bool(band.get("enabled", False))
        if dpg.does_item_exist(f"band{band_id}_enabled"):
            dpg.set_value(f"band{band_id}_enabled", bands_enabled[band_id])
        for key in ("start", "end", "min", "max"):
            tag = f"band{band_id}_{key}"
            if key in band and dpg.does_item_exist(tag):
                dpg.set_value(tag, float(band[key]))
        if bands_enabled[band_id]:
            refresh_band_value(state.spectrum_bars_cache, band_id)


def _apply_sequencer_state(seq: dict[str, Any]) -> None:
    """Re-apply beat source, manual BPM, tracks and the audio section (e11s01)."""
    mode = seq.get("beat_source")
    if mode not in BEAT_SOURCE_LABELS:
        mode = BEAT_SOURCE_ANALYSIS
    if "manual_bpm" in seq and dpg.does_item_exist("manual_bpm_input"):
        dpg.set_value("manual_bpm_input", float(seq["manual_bpm"]))
    midi_action_beat_source(mode)  # beat_source + checkboxes + manual-widget visibility
    for row, track_data in enumerate(seq.get("tracks", [])):
        if row >= NUM_TRACKS:
            break
        _restore_track(row, track_data)
    audio = seq.get("audio")
    if isinstance(audio, dict):
        _apply_audio_state(audio)


def apply_project_state(state: dict[str, Any]) -> None:
    """Re-apply a project dict onto the live app (layout, theme, sequencer) (e11s01)."""
    apply_window_layout(state.get("layout", {}).get("windows", []))
    theme = state.get("theme")
    if isinstance(theme, dict):
        _apply_theme_config(theme)
    seq = state.get("sequencer")
    if isinstance(seq, dict):
        _apply_sequencer_state(seq)


def pristine_project_state() -> dict[str, Any]:
    """The New-project document: blank sequencer, current layout + theme kept (e15s01).

    Layout and theme are app-level preferences — a fresh project never moves
    the windows or changes the colors; only the sequencer content resets.
    """
    pristine_tracks = [_pristine_track() for _ in range(NUM_TRACKS)]
    return {
        "layout": {"windows": snapshot_window_layout()},
        "theme": {
            "preset": _preset_key(str(dpg.get_value("theme_preset"))),
            "colors": copy.deepcopy(state.active_palette),
        },
        "sequencer": {
            "beat_source": BEAT_SOURCE_ANALYSIS,
            "manual_bpm": DEFAULT_MANUAL_BPM,
            "tracks": [
                {
                    "target_id": track["target_id"],
                    "base_address": track["base_address"],
                    "steps": [_step_persisted(step) for step in track["steps"]],
                }
                for track in pristine_tracks
            ],
            "audio": {
                "device": str(dpg.get_value("combo_devices")),
                "lowpass": True,
                "bands": {
                    str(band_id): {
                        "enabled": False,
                        "start": default_range[0],
                        "end": default_range[1],
                        "min": 0.0,
                        "max": 1.0,
                    }
                    for band_id, default_range in BAND_DEFAULT_RANGES.items()
                },
            },
        },
    }


def apply_new_project() -> None:
    """Reset the live sequencer to pristine defaults and rebuild its UI (e15s01).

    Tracks are replaced wholesale (clearing pending fades and the runtime
    last_rand_* keys), then the project-apply path rebuilds every cell, slot,
    beat-source checkbox and audio widget.
    """
    for row in range(NUM_TRACKS):
        tracks_data[row] = _pristine_track()
    _apply_sequencer_state(pristine_project_state()["sequencer"])


def _project_document(state: dict[str, Any]) -> dict[str, Any]:
    """Wrap a state dict into a versioned project document (e11s01)."""
    return {"format": PROJECT_FORMAT, "version": PROJECT_VERSION, **state}


def save_project_to_file(path: str, state: dict[str, Any]) -> bool:
    """Atomically write a project document; False + logged reason on failure (e11s01)."""
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_project_document(state), f, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as e:
        log_error("Project", f"cannot write {path}: {e}")
        return False


def load_project_file(path: str) -> dict[str, Any] | None:
    """Read + validate a project file, sanitized; None (logged) on any problem (e11s01)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        log_error("Project", f"cannot read {path}: {e}")
        return None
    if not isinstance(raw, dict):
        log_error("Project", f"{path}: not a project document")
        return None
    if raw.get("format") != PROJECT_FORMAT or raw.get("version") != PROJECT_VERSION:
        log_error(
            "Project",
            f"{path}: unsupported format/version {raw.get('format')}/{raw.get('version')}",
        )
        return None
    return _sanitize_project_state(raw)


def _to_float(value: Any, default: float) -> float:
    """Coerce a stored value to float, falling back on garbage (e11s01)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _default_step() -> dict[str, Any]:
    """A pristine step cell, the template for sanitize healing (e11s01)."""
    return {
        "active": False,
        "type": "NONE",
        "v1": 0.0,
        "v2": 1.0,
        "frames": 4,
        "msgs": 1,
        "color": [1.0, 1.0, 1.0],
    }


def _sanitize_step(step: Any) -> dict[str, Any]:
    """Heal one step: missing persisted keys get defaults, unknown keys drop (e11s01)."""
    base = _default_step()
    if isinstance(step, dict):
        for key in STEP_PERSISTED_KEYS:
            if key in step:
                base[key] = step[key]
    return base


def _sanitize_tracks(tracks: Any) -> list[dict[str, Any]]:
    """Heal the track list: bounded to NUM_TRACKS, steps healed and capped (e11s01)."""
    clean: list[dict[str, Any]] = []
    if isinstance(tracks, list):
        for track in tracks[:NUM_TRACKS]:
            if not isinstance(track, dict):
                track = {}
            steps = track.get("steps", [])
            if not isinstance(steps, list):
                steps = []
            clean.append(
                {
                    "target_id": track.get("target_id"),
                    "base_address": track.get("base_address", ""),
                    "steps": [_sanitize_step(s) for s in steps[:NUM_STEPS]],
                }
            )
    return clean


def _sanitize_audio_state(audio: Any) -> dict[str, Any]:
    """Heal the audio section: band values clamped to their defaults (e11s01)."""
    if not isinstance(audio, dict):
        audio = {}
    raw_bands = audio.get("bands", {})
    if not isinstance(raw_bands, dict):
        raw_bands = {}
    bands: dict[str, dict[str, Any]] = {}
    for band_id, default_range in BAND_DEFAULT_RANGES.items():
        band = raw_bands.get(str(band_id), {})
        if not isinstance(band, dict):
            band = {}
        bands[str(band_id)] = {
            "enabled": bool(band.get("enabled", False)),
            "start": _to_float(band.get("start"), default_range[0]),
            "end": _to_float(band.get("end"), default_range[1]),
            "min": _to_float(band.get("min"), 0.0),
            "max": _to_float(band.get("max"), 1.0),
        }
    return {
        "device": str(audio.get("device", "")),
        "lowpass": bool(audio.get("lowpass", True)),
        "bands": bands,
    }


def _sanitize_project_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a loaded project document into the capture shape (e11s01)."""
    theme = raw.get("theme")
    if not isinstance(theme, dict):
        theme = {"preset": "scuro", "colors": copy.deepcopy(DEFAULT_PALETTE)}
    else:
        preset = str(theme.get("preset", "scuro"))
        if preset not in THEME_PRESET_LABELS:
            preset = "scuro"
        palette = theme.get("colors")
        if not isinstance(palette, dict):
            palette = copy.deepcopy(DEFAULT_PALETTE)
        theme = {"preset": preset, "colors": _sanitize_palette(palette)}
    layout = raw.get("layout")
    if not isinstance(layout, dict) or not isinstance(layout.get("windows"), list):
        layout = {"windows": []}
    seq = raw.get("sequencer")
    if not isinstance(seq, dict):
        seq = {}
    beat = seq.get("beat_source")
    return {
        "layout": {"windows": layout["windows"]},
        "theme": theme,
        "sequencer": {
            "beat_source": beat if beat in BEAT_SOURCE_LABELS else BEAT_SOURCE_ANALYSIS,
            "manual_bpm": _to_float(seq.get("manual_bpm"), DEFAULT_MANUAL_BPM),
            "tracks": _sanitize_tracks(seq.get("tracks")),
            "audio": _sanitize_audio_state(seq.get("audio")),
        },
    }


def remember_recent_project(cfg: dict[str, Any], path: str) -> list[str]:
    """Insert a project path at the front of the recent list (dedupe + cap) (e11s02)."""
    recent = cfg.setdefault("projects", {}).setdefault("recent", [])
    if path in recent:
        recent.remove(path)
    recent.insert(0, path)
    del recent[RECENT_PROJECTS_MAX:]
    return recent


def recent_project_paths(cfg: dict[str, Any]) -> list[str]:
    """The recent project list with entries whose files no longer exist pruned (e11s02)."""
    return [p for p in cfg.get("projects", {}).get("recent", []) if os.path.exists(p)]


def should_restore_last_project_on_boot(cfg: dict[str, Any]) -> bool:
    """Whether boot should re-apply the most recent project (default True) (e11s02)."""
    return bool(cfg.get("projects", {}).get("restore_last_on_boot", True))


def on_restore_project_boot_toggle(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Settings 'Restore last project at startup' checkbox: persist the flag (e11s02)."""
    cfg = load_config()
    cfg.setdefault("projects", {})["restore_last_on_boot"] = bool(app_data)
    save_config(cfg)


# --- e11s03: viSeq menu flows (Open / Last / Save / Exit + file dialogs) ---
def _ensure_project_extension(path: str) -> str:
    """Append the .viseq extension when the chosen name has none (e11s03)."""
    if path.lower().endswith(PROJECT_FILE_EXTENSION):
        return path
    return f"{path}{PROJECT_FILE_EXTENSION}"


def save_project_file(path: str) -> bool:
    """Capture + write a project, then remember it; False + logged on failure (e11s03)."""
    path = _ensure_project_extension(path)
    if not save_project_to_file(path, capture_project_state()):
        return False
    cfg = load_config()
    remember_recent_project(cfg, path)
    save_config(cfg)
    rebuild_last_project_menu()
    return True


def open_project_file(path: str) -> bool:
    """Load + apply a project, sync the fallback theme, remember it (e11s03)."""
    state = load_project_file(path)
    if state is None:
        return False
    apply_project_state(state)
    cfg = load_config()
    cfg["theme"] = state["theme"]
    remember_recent_project(cfg, path)
    save_config(cfg)
    rebuild_last_project_menu()
    return True


def rebuild_last_project_menu() -> None:
    """Rebuild the Last-project submenu from the recent list (e11s03)."""
    if not dpg.does_item_exist("menu_last_project"):
        return
    dpg.delete_item("menu_last_project", children_only=True)
    recent = recent_project_paths(load_config())
    if not recent:
        dpg.add_menu_item(label="No recent projects", enabled=False, parent="menu_last_project")
        return
    for path in recent:
        dpg.add_menu_item(
            label=os.path.basename(path),
            callback=open_recent_project,
            user_data=path,
            parent="menu_last_project",
        )


def open_recent_project(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Last-project submenu entry -> open that project file (e11s03)."""
    if isinstance(user_data, str):
        open_project_file(user_data)


def _recreate_project_dialog(tag: str, callback: Any, default_filename: str | None) -> None:
    """(Re)create one project file dialog with .viseq/.* filters, then show it (e13s02).

    DPG's file dialog shows only directories when no extension filters exist;
    recreating on every show guarantees a fresh dialog whose default path was
    just created by the caller.
    """
    if dpg.does_item_exist(tag):
        dpg.delete_item(tag)
    kwargs: dict[str, Any] = {
        "tag": tag,
        "show": False,
        "width": 480,
        "height": 360,
        "callback": callback,
        "default_path": PROJECTS_DIR,
        "modal": True,
    }
    if default_filename:
        kwargs["default_filename"] = default_filename
    with dpg.file_dialog(**kwargs):
        dpg.add_file_extension(".viseq")
        dpg.add_file_extension(".*")
        dpg.add_file_extension("")  # #2080 defensive: some systems hide files with .* only
    dpg.show_item(tag)


def show_open_project_dialog() -> None:
    """Show the Open-project file dialog, defaulting to the projects folder (e11s03, e13s02)."""
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    _recreate_project_dialog("open_project_dialog", on_open_project_picked, None)


def show_save_project_dialog() -> None:
    """Show the Save-project file dialog, defaulting to the projects folder (e11s03, e13s02)."""
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    _recreate_project_dialog("save_project_dialog", on_save_project_picked, "project.viseq")


def on_open_project_picked(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open-dialog result -> open the chosen project file (e11s03)."""
    path = app_data.get("file_path_name") if isinstance(app_data, dict) else None
    if path:
        open_project_file(path)


def on_save_project_picked(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Save-dialog result -> save a project, forcing the .viseq extension (e11s03)."""
    path = app_data.get("file_path_name") if isinstance(app_data, dict) else None
    if path:
        save_project_file(path)


def exit_app(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """viSeq > Exit: ask for confirmation before closing the app (e19)."""
    show_exit_confirm()


# e19: exit confirmation — the modal is shared by viSeq > Exit and the OS
# main-window X (set_exit_callback + disable_close). ``_exiting_app`` guards
# the shutdown-time re-invocation of the exit callback (destroy_context queues
# it again while tearing down, when no modal may be created).
EXIT_CONFIRM_TAG = "exit_confirm_modal"


_exiting_app = False


def show_exit_confirm(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Close request (menu Exit or the OS window X): confirm before quitting (e19)."""
    if _exiting_app:
        return  # already confirmed — destroy_context re-invokes the exit callback
    if dpg.does_item_exist(EXIT_CONFIRM_TAG):
        dpg.delete_item(EXIT_CONFIRM_TAG)
    with dpg.window(
        label="Exit viSeq",
        tag=EXIT_CONFIRM_TAG,
        modal=True,
        width=380,
        height=150,
        no_resize=True,
    ):
        themed_text("Close viSeq?", slot="text")
        themed_text("Any unsaved changes will be lost.", slot="text_dim")
        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_button(label="Cancel", callback=cancel_exit, width=140)
            dpg.add_button(label="Exit", callback=confirm_exit, width=140)
    dpg.show_item(EXIT_CONFIRM_TAG)


def cancel_exit(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Exit modal Cancel: close the prompt, the app keeps running (e19)."""
    if dpg.does_item_exist(EXIT_CONFIRM_TAG):
        dpg.delete_item(EXIT_CONFIRM_TAG)


def confirm_exit(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Exit modal Exit: close the prompt and stop the app (normal cleanup runs)."""
    global _exiting_app
    _exiting_app = True
    if dpg.does_item_exist(EXIT_CONFIRM_TAG):
        dpg.delete_item(EXIT_CONFIRM_TAG)
    dpg.stop_dearpygui()


NEW_PROJECT_CONFIRM_TAG = "new_project_confirm"


def show_new_project_confirm(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """viSeq > New project: open the confirmation modal (e15s01).

    Never resets directly — wiping the sequencer is destructive, so the user
    must confirm first.
    """
    if dpg.does_item_exist(NEW_PROJECT_CONFIRM_TAG):
        dpg.delete_item(NEW_PROJECT_CONFIRM_TAG)
    with dpg.window(
        label="New project",
        tag=NEW_PROJECT_CONFIRM_TAG,
        modal=True,
        width=380,
        height=140,
        no_resize=True,
    ):
        dpg.add_text("Start a new project?", wrap=340)
        dpg.add_text("The sequencer (clips and steps) will be cleared.", wrap=340)
        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_button(label="Cancel", callback=cancel_new_project, width=140)
            dpg.add_button(label="New project", callback=confirm_new_project, width=140)
    dpg.show_item(NEW_PROJECT_CONFIRM_TAG)


def cancel_new_project(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Modal Cancel: close the confirmation without touching the session (e15s01)."""
    if dpg.does_item_exist(NEW_PROJECT_CONFIRM_TAG):
        dpg.delete_item(NEW_PROJECT_CONFIRM_TAG)


def confirm_new_project(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Modal New project: close the confirmation and reset the sequencer (e15s01)."""
    if dpg.does_item_exist(NEW_PROJECT_CONFIRM_TAG):
        dpg.delete_item(NEW_PROJECT_CONFIRM_TAG)
    apply_new_project()


def apply_boot_config() -> None:
    """Boot: apply the fallback theme, then restore the last project when flagged (e11s04)."""
    cfg = load_config()
    midi_init_from_config(cfg)  # e09: MIDI control mirrors (enabled, port, bindings)
    if dpg.does_item_exist("midi_enable_cb"):
        # The MIDI window is built before the config loads, so the Enable checkbox
        # starts unchecked even when the engine is on — sync it (BUG-2026-08-29T102156).
        dpg.set_value("midi_enable_cb", state.midi_enabled)
    _apply_theme_config(cfg["theme"])
    if dpg.does_item_exist("cb_restore_project_boot"):
        dpg.set_value("cb_restore_project_boot", cfg["projects"]["restore_last_on_boot"])
    if should_restore_last_project_on_boot(cfg):
        recent = recent_project_paths(cfg)
        if recent:
            project_state = load_project_file(recent[0])
            if project_state is not None:
                apply_project_state(project_state)


def format_osc_log(history: list[str]) -> str:
    """Render the OSC log newest-first (the latest line on top)."""
    return "\n".join(reversed(history))


# --- GLOBAL VIMIX STATE ---
# e10s06: viseq-side primary selection (target_id) — wins over the vimix current
# source for tile theming and for sequencer/monitor attachment.


# --- SEQUENCER STATE ---

# Beat/clock source selection (e05): the sequencer can follow the analyzed BPM, a band
# hitting 1.0, standard MIDI clock, or a manual BPM (numeric/TAP). Event-driven modes wake
# the sequencer on sync_event_beat instead of sleeping a fixed interval.
# One LED per beat source, shown next to its checkbox on the sequencer (e05)
BEAT_LED_TAGS = {
    BEAT_SOURCE_ANALYSIS: "led_analysis",
    BEAT_SOURCE_BAND1: "led_band1",
    BEAT_SOURCE_MIDI: "led_midi",
    BEAT_SOURCE_MANUAL: "led_manual",
}
BEAT_CHECKBOX_TAGS = {mode: f"cb_beat_{mode}" for mode in BEAT_SOURCE_LABELS}


# Sequencer data structure: one pristine track per row; the New-project reset
# (e15s01) reuses the same factories, so a fresh project equals a cold boot.

# --- AUDIO STATE ---
audio_stream: Any = None
# e10s08: timestamp of the last successful BPM detection — a stale/absent reading
# means current_bpm is not a real tempo (e.g. the manual value left over from a
# previous mode), so the sequencer must not advance on it.

# --- SPECTRUM ANALYZER (e04) ---
# 16 bars > 32: benchmarked lighter (~26% less compute per frame + half the draw calls);
# the FFT dominates either way, and 16 bars keep a clear view of the audible range.
# e10s09: perceptual spectrum — log-spaced bars over the musical range, level-
# independent AGC and peak-aware band values so every band responds to music.
# slow enough that quiet content between transients stays quiet (beat edges re-arm)
# A kick/bass transient lands around 0.6-0.9 after AGC+blend (measured); 0.6 fires
# on strong band transients while the edge semantics ignore sustained content.
BAND_RECT_COLORS = {
    1: ((255, 255, 0, 40), (255, 255, 0, 200)),  # yellow overlay
    2: ((0, 255, 255, 40), (0, 255, 255, 200)),  # cyan overlay
    3: ((255, 0, 255, 40), (255, 0, 255, 200)),  # magenta overlay
}
BAND_DEFAULT_RANGES = {1: (0.0, 0.33), 2: (0.33, 0.66), 3: (0.66, 1.0)}  # equal thirds

# Last computed spectrum bars + per-band state. All written on the main thread inside the
# queued spectrum task; future features read band1/band2/band3 (0..1, 0 while disabled).

# e10s03: per-source list of texture tags (tex_<name>_<idx>)
# e10s04: per-source thumb cycle state {target_id: (current_index, last_switch_time)}
# e10s04: consecutive unanswered thumb requests per source -> failed tile state


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


def frame_sleep() -> float:
    """Main-loop sleep: full rate while animating, throttled while idle (perf e07 P1)."""
    if state.is_playing or state.is_audio_analyzing:
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
    if state.copied_step_pos is not None:
        r, c = state.copied_step_pos
        update_step_theme(r, c)  # restore the standard theme for the previous copy
    state.copied_step_pos = (row, col)
    dpg.bind_item_theme(f"seq_cell_{row}_{col}", theme_step_copied)


def copy_step(sender: Any, app_data: Any, user_data: Any) -> None:
    """Remember the full configuration of a step for later paste (e08)."""
    row, col = user_data
    state.active_step = (row, col)
    state.copied_step_data = copy.deepcopy(tracks_data[row]["steps"][col])
    _highlight_copied_step(row, col)


def paste_step(sender: Any, app_data: Any, user_data: Any) -> None:
    """Apply the copied step configuration to the given step (e08)."""
    row, col = user_data
    state.active_step = (row, col)
    if state.copied_step_data is None:
        return
    tracks_data[row]["steps"][col] = copy.deepcopy(state.copied_step_data)
    update_step_ui(row, col)


def paste_step_to_row(sender: Any, app_data: Any, user_data: Any) -> None:
    """Apply the copied step configuration to every step of the sequencer row (e08)."""
    row, _ = user_data
    state.active_step = (row, 0)
    if state.copied_step_data is None:
        return
    for c in range(NUM_STEPS):
        tracks_data[row]["steps"][c] = copy.deepcopy(state.copied_step_data)
        update_step_ui(row, c)


def on_copy_shortcut(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Ctrl+C: copy the last touched step (ignored while typing in an input)."""
    if _any_input_focused() or state.active_step is None:
        return
    copy_step(None, None, state.active_step)


def on_paste_shortcut(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Ctrl+V: paste into the last touched step (ignored while typing in an input)."""
    if _any_input_focused() or state.active_step is None:
        return
    paste_step(None, None, state.active_step)


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


def _set_step_active(row: int, col: int, active: bool) -> None:
    """Set a step's active state and refresh its visuals — shared by mouse and MIDI."""
    state.active_step = (row, col)  # clicking a step makes it the shortcut target
    tracks_data[row]["steps"][col]["active"] = active
    if dpg.does_item_exist(f"seq_cb_{row}_{col}"):
        dpg.set_value(f"seq_cb_{row}_{col}", active)  # keep the cell checkbox in sync
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
    grid_mirror_step(row, col, is_active, is_head)  # e14s02: LED mirror (thread-safe)
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
            color=palette_rgba(state.active_palette["text"]),
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

    update_step_theme(row, col, is_head=(state.is_playing and state.current_step == col))


def _add_tile_context_items(target_id: str) -> None:
    """Both right-click actions of a Mediagrid tile: regen thumb + new mapping (e16).

    Must run inside a ``with dpg.window(popup=True, ...)`` block so the items are
    parented to that popup window. Every tile calls this — the two actions must
    never drift apart.
    """
    dpg.add_menu_item(
        label="Regenerate Thumbnail (Random)",
        callback=regen_thumb_callback,
        user_data=target_id,
    )
    dpg.add_menu_item(
        label="New Mapping...",
        callback=open_new_mapping_dialog,
        user_data=target_id,
    )


def _tile_popup_tag(target_id: str) -> str:
    """Tag of the single right-click action popup window of a Mediagrid tile."""
    return f"tile_popup_{target_id}"


def _create_tile_popup(target_id: str) -> None:
    """One right-click action popup per tile, recreated on every grid rebuild.

    DPG binds ONE handler registry per item (bind_item_handler_registry replaces
    the previous one), so the old per-item ``dpg.popup`` registries were clobbered
    by the tile click registry on every rebuild and the right-click menu died.
    The fix: a single combined registry (left = select, right = show this popup
    window) — no registry conflict, the menu survives rebuilds.
    """
    popup_tag = _tile_popup_tag(target_id)
    if dpg.does_item_exist(popup_tag):
        dpg.delete_item(popup_tag)
    with dpg.window(popup=True, show=False, no_title_bar=True, autosize=True, tag=popup_tag):
        _add_tile_context_items(target_id)


def on_tile_context_click(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Right-click on a Mediagrid tile: show the tile's action popup (e16)."""
    popup_tag = _tile_popup_tag(user_data)
    if dpg.does_item_exist(popup_tag):
        dpg.show_item(popup_tag)


def regen_thumb_callback(sender: Any, app_data: Any, user_data: Any) -> None:
    target_id = user_data
    if state.viosc_client:
        msg_addr = f"/viosc/regen_thumb/{target_id}"
        state.viosc_client.send_message(msg_addr, [])
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
            click_reg_tag = media_tile_click_registry_tag(target_id)
            if dpg.does_item_exist(click_reg_tag):
                dpg.bind_item_handler_registry(loading_tag, click_reg_tag)


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

        # e16: the img joins the tile's combined click registry (left = select,
        # right = action popup) — the old per-item popup registry here was
        # replaced by the click registry on the next rebuild, killing right-click.
        click_reg_tag = media_tile_click_registry_tag(target_id)
        if dpg.does_item_exist(click_reg_tag):
            dpg.bind_item_handler_registry(img_tag, click_reg_tag)

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
        color=palette_rgba(state.active_palette["warning"]),
        tag=loading_tag,
    )
    _text_color_bindings[loading_tag] = "text_dim"
    click_reg_tag = media_tile_click_registry_tag(target_id)
    if dpg.does_item_exist(click_reg_tag):
        dpg.bind_item_handler_registry(loading_tag, click_reg_tag)


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
    if target_id == state.viseq_selected_source:
        return theme_selected_clip
    if str(idx) == str(state.global_vimix_state.get("current_source")):
        return theme_vimix_current_clip
    return theme_normal_clip


def refresh_tile_selection_themes() -> None:
    """Re-apply the selection themes to every Mediagrid tile without a rebuild.

    Called from the tile click handler: the viseq selection changes without
    touching the grid signature, so the theme binding loop re-runs on the
    existing tiles instead of rebuilding them.
    """
    for idx, props in state.global_vimix_state.get("sources", {}).items():
        name = props.get("name")
        target_id = str(name) if name else str(idx)
        tile_tag = f"tile_{target_id}"
        if dpg.does_item_exist(tile_tag):
            dpg.bind_item_theme(tile_tag, _tile_theme_for(idx, target_id))


def on_media_tile_click(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Select a media from the Mediagrid — the viseq-side primary selection (e10s06)."""
    target_id = user_data
    if target_id == state.viseq_selected_source:
        return
    state.viseq_selected_source = target_id
    refresh_tile_selection_themes()


def request_missing_thumbnails(now: float) -> None:
    """Request thumbs for sources that still lack them (3 s throttle, e10s04).

    Each sent-but-unanswered request bumps the source's fail counter; crossing
    the threshold fires ONE regen retry and the tile flips to the failed label
    (rendered by update_vimix_sources_ui). A successful reply clears the counter.
    """
    if not state.viosc_client:
        return
    for idx, props in state.global_vimix_state.get("sources", {}).items():
        name = props.get("name")
        uri = props.get("uri")
        target_id = str(name) if name else str(idx)

        if uri and target_id not in thumbnails_data:
            last_thumb = request_timestamps.get(f"thumb_{target_id}", 0)
            if now - last_thumb > THUMB_REQUEST_INTERVAL:
                msg_addr = f"/viosc/thumb/{target_id}"
                state.viosc_client.send_message(msg_addr, ["all"])
                append_log("OUT", msg_addr)
                request_timestamps[f"thumb_{target_id}"] = now
                thumb_fail_count[target_id] = thumb_fail_count.get(target_id, 0) + 1
                if thumb_fail_count[target_id] == THUMB_FAIL_THRESHOLD:
                    regen_addr = f"/viosc/regen_thumb/{target_id}"
                    state.viosc_client.send_message(regen_addr, [])
                    append_log("OUT", regen_addr)
                    _show_failed_tile_label(target_id)


def update_vimix_sources_ui(json_string: str) -> None:
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
        state.global_vimix_state["current_source"] = payload.get("current_source")
        state.global_vimix_state["sources"] = sources

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
                popup_tag = _tile_popup_tag(target_id)
                if dpg.does_item_exist(popup_tag):
                    dpg.delete_item(popup_tag)  # stale action popup must not linger (e16)
        for key in list(request_timestamps):
            if key.startswith("thumb_"):
                target_id = key[len("thumb_") :]
                if target_id not in live_ids:
                    request_timestamps.pop(key)
        for target_id in list(thumb_fail_count):
            if target_id not in live_ids:
                thumb_fail_count.pop(target_id)
        if mapper.prune_mappings(live_ids):
            refresh_mapper_ui()  # a removed source takes its mappings with it (e16)
        if state.viseq_selected_source is not None and state.viseq_selected_source not in live_ids:
            state.viseq_selected_source = None  # a pruned source can't stay selected (e10s06)

        current_source = state.global_vimix_state["current_source"]
        data_dict = state.global_vimix_state["sources"]

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
        current_signature = f"cols:{state.last_num_cols}_src:{current_source}_" + str(
            [(k, data_dict[k].get("name"), data_dict[k].get("index")) for k in sorted_keys]
        )

        if current_signature != state.last_ui_signature:
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

            num_cols = state.last_num_cols
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
                            0,  # left click selects the media (e10s06)
                            callback=lambda *_, t=target_id: on_media_tile_click(None, None, t),
                        )
                        dpg.add_item_clicked_handler(
                            1,  # right click opens the tile action popup (e16)
                            callback=lambda *_, t=target_id: on_tile_context_click(None, None, t),
                        )
                    # e16: ONE popup window per tile, opened by the right-click
                    # handler above. The old per-item dpg.popup registries were
                    # replaced by the click registry on every rebuild (DPG binds a
                    # single registry per item) — the menu silently died. The
                    # combined registry + shared popup survives rebuilds.
                    _create_tile_popup(target_id)

                    title_tag = f"tile_title_{target_id}"
                    if dpg.does_item_exist(title_tag):
                        dpg.delete_item(title_tag)
                    dpg.add_text(
                        "---",
                        parent=cw,
                        wrap=MEDIA_TITLE_WRAP,
                        color=palette_rgba(state.active_palette["text_bright"]),
                        tag=title_tag,
                    )
                    _text_color_bindings[title_tag] = "text_bright"

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
                    else:
                        loading_tag = f"loading_txt_{target_id}"
                        if dpg.does_item_exist(loading_tag):
                            dpg.delete_item(loading_tag)
                        is_failed = thumb_fail_count.get(target_id, 0) >= THUMB_FAIL_THRESHOLD
                        dpg.add_text(
                            THUMB_FAIL_LABEL if is_failed else " [ Loading... ]",
                            parent=g_id,
                            color=palette_rgba(
                                state.active_palette["warning"]
                                if is_failed
                                else state.active_palette["text_dim"]
                            ),
                            tag=loading_tag,
                        )
                        _text_color_bindings[loading_tag] = "text_dim"

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
            state.last_ui_signature = current_signature

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


# ==============================================================================
# MONITOR PLAYERS
# ==============================================================================


def new_monitor_player(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    state.monitor_player_counter += 1
    player_id = state.monitor_player_counter
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


def start_osc_server(ip: str, port: int) -> bool:
    """Start the local OSC listening server (main thread); True when it is up."""
    if state.is_server_running:
        return True
    try:
        disp = dispatcher.Dispatcher()
        disp.set_default_handler(incoming_osc_handler)
        state.local_osc_server = ViseqOSCUDPServer((ip, port), disp)
        state.local_server_thread = threading.Thread(
            target=state.local_osc_server.serve_forever, daemon=True
        )
        state.local_server_thread.start()
        state.is_server_running = True
        dpg.set_item_label("btn_server_toggle", "Stop Server")
        dpg.set_value("server_status", f"Server Status: Listening on {ip}:{port}")
        return True
    except Exception as e:
        dpg.set_value("server_status", f"Server Status: ERROR ({e})")
        return False


def toggle_local_server() -> None:
    if state.is_server_running:
        if state.local_osc_server and state.local_server_thread is not None:
            state.local_osc_server.shutdown()
            state.local_server_thread.join(timeout=1.0)
            state.local_osc_server = None
        state.is_server_running = False
        dpg.set_item_label("btn_server_toggle", "Start Server")
        dpg.set_value("server_status", "Server Status: Stopped")
    else:
        start_osc_server(str(dpg.get_value("listen_ip")), int(dpg.get_value("listen_port")))


def connect_osc_client(ip: str, port: int) -> bool:
    """Create the viOSC client (main thread); True when ready."""
    try:
        state.viosc_client = udp_client.SimpleUDPClient(ip, port)
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
    state.beat_source = mode
    for m in BEAT_SOURCE_LABELS:
        dpg.set_value(f"cb_beat_{m}", m == state.beat_source)
    is_manual = state.beat_source == BEAT_SOURCE_MANUAL
    dpg.configure_item("manual_bpm_input", show=is_manual)
    dpg.configure_item("btn_tap", show=is_manual)
    dpg.configure_item(
        "manual_bpm_text", show=is_manual
    )  # hide the stale readout outside manual mode
    if is_manual:
        state.current_bpm = float(dpg.get_value("manual_bpm_input"))
        dpg.set_value("testo_bpm", f"BPM: {state.current_bpm:.1f}")
        dpg.set_value("manual_bpm_text", f"{state.current_bpm:.0f} BPM")


def on_beat_source(sender: Any, app_data: Any, user_data: Any) -> None:
    """Select the sequencer beat source; exactly one checkbox stays active."""
    if not app_data:
        dpg.set_value(sender, True)  # a beat source must remain selected
        return
    midi_action_beat_source(user_data)


def on_manual_bpm(sender: Any, app_data: Any, user_data: Any) -> None:
    """Set the sequencer BPM from the manual numeric input."""
    state.current_bpm = float(app_data)
    dpg.set_value("testo_bpm", f"BPM: {state.current_bpm:.1f}")
    dpg.set_value("manual_bpm_text", f"{state.current_bpm:.0f} BPM")


def midi_action_transport_tap() -> None:
    """Register a tap for the manual BPM — shared by mouse and MIDI (e09)."""
    now = time.time()
    if tap_times and now - tap_times[-1] > 2.0:
        tap_times.clear()  # stale tap starts a new sequence
    tap_times.append(now)
    del tap_times[:-8]  # keep the most recent taps
    if len(tap_times) >= 2:
        intervals = [tap_times[i + 1] - tap_times[i] for i in range(len(tap_times) - 1)]
        bpm = 60.0 / (sum(intervals) / len(intervals))
        state.current_bpm = round(bpm, 2)
        dpg.set_value("manual_bpm_input", round(state.current_bpm))
        dpg.set_value("testo_bpm", f"BPM: {state.current_bpm:.1f}")
        dpg.set_value("manual_bpm_text", f"{state.current_bpm:.0f} BPM")


def tap_bpm(sender: Any, app_data: Any, user_data: Any) -> None:
    """Set the BPM from the average interval of the last taps (manual mode)."""
    midi_action_transport_tap()


def midi_beats_from_pulses(pulses: int) -> int:
    """Whole quarter-note beats contained in a MIDI clock pulse count (24 pulses/beat)."""
    return pulses // MIDI_CLOCK_PULSES_PER_BEAT


# ---------- e09: MIDI control engine ----------


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
    elif action == MIDI_ACTION_MAPPER_MAPPING:  # e18: drive a Mapper control
        midi_mapping_value(int(params.get("mapping_id", 0)), value)


def _midi_enqueue_execute(action: str, params: dict[str, Any], value: int) -> None:
    """Push one resolved MIDI action execution to the main thread (ui_task_queue)."""
    ui_task(lambda: midi_execute(action, params, value))


def _log_unmatched_midi(msg: Any, port_name: str) -> None:
    """Throttled diagnostic: log an incoming message that no binding resolved (e14).

    At most one line per second per port — enough to see the device's messages in the
    Logs window without flooding it.
    """
    now = time.time()
    if now - _last_unmatched_log.get(port_name, 0.0) < 1.0:
        return
    _last_unmatched_log[port_name] = now
    msg_type, number, value = _parse_midi_msg(msg)
    if msg_type is not None:
        append_log("MIDI", f"unmatched {msg_type} {number} (val {value}) on {port_name}")


def _log_first_midi_message(msg: Any, port_name: str) -> None:
    """Log the FIRST message seen on a port — tells input from 'no traffic' (e14 debug)."""
    if port_name in _midi_first_msg_logged:
        return
    _midi_first_msg_logged.add(port_name)
    msg_type, number, value = _parse_midi_msg(msg)
    if msg_type is not None:
        append_log("MIDI", f"first msg on {port_name}: {msg_type} {number} val {value}")


def handle_midi_message(msg: Any, port_name: str) -> None:
    """Route one incoming message (worker thread): learn capture first, then dispatch."""
    _log_first_midi_message(msg, port_name)
    if state.midi_learn_pending is not None:
        source = binding_source_from_message(msg, port_name)
        if source is not None:
            ui_task(lambda: midi_learn_complete(source, port_name))
            return
    controller = find_controller_by_port(port_name)
    if controller is not None:
        bindings: list[dict[str, Any]] | None = list(controller.get("bindings") or []) + list(
            controller.get("auto_bindings") or []
        )
    else:
        bindings = None  # legacy flat lists (pre-e14 paths/tests)
    resolved = False
    for action, params, value in resolve_midi_message(msg, port_name, bindings):
        resolved = True
        _midi_enqueue_execute(action, params, value)
    if not resolved and bindings is not None:
        _log_unmatched_midi(msg, port_name)


def _exit_midi_learn() -> None:
    """Turn MIDI Learn off and restore the button/status (cancel, complete, disable)."""
    state.midi_learn_mode = False
    state.midi_learn_pending = None
    if dpg.does_item_exist("midi_learn_btn"):
        dpg.set_item_label("midi_learn_btn", "Learn mapping...")
    if dpg.does_item_exist("midi_learn_status"):
        dpg.set_value("midi_learn_status", "MIDI Learn off")


def midi_learn_complete(binding: dict[str, Any], port_name: str | None = None) -> None:
    """Main thread: merge the captured source with the pending action and store the binding
    on the owning controller (legacy flat list when no controller owns the port).

    One-shot (e14 bug fix): learn mode exits after the capture, so the learnable
    sequencer controls (PLAY, beat sources, step cells) are never left hijacked.
    """
    if state.midi_learn_pending is None:
        return
    action, params = state.midi_learn_pending
    binding["action"] = action
    binding["params"] = params
    controller = find_controller_by_port(port_name) if port_name else None
    if controller is not None:
        controller.setdefault("bindings", []).append(binding)
    else:
        midi_bindings.append(binding)
    state.midi_learn_pending = None
    _exit_midi_learn()
    refresh_midi_mappings_ui()
    if action == MIDI_ACTION_MAPPER_MAPPING:  # e18: bind the learned control to the mapping
        mapper.set_mapping_midi(int(params.get("mapping_id", 0)), binding)
        _close_mapper_learn_window()
        refresh_mapper_ui()
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
    mouse click and a MIDI trigger share the exact same callback path (e09s02). A stale
    learn session (past MIDI_LEARN_TIMEOUT_SECONDS) expires and delegates, so the MIDI
    logic can never permanently disable the sequencer controls (e14 bug fix).
    """

    def wrapper(sender: Any, app_data: Any, user_data: Any) -> None:
        if state.midi_learn_mode:
            if time.time() - state.midi_learn_started_at > MIDI_LEARN_TIMEOUT_SECONDS:
                _exit_midi_learn()
                callback(sender, app_data, user_data)
                return
            state.midi_learn_pending = action_builder(user_data)
            if dpg.does_item_exist("midi_learn_status"):
                dpg.set_value("midi_learn_status", "Now press your MIDI button")
            return
        callback(sender, app_data, user_data)

    return wrapper


def toggle_midi_learn(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Toggle MIDI Learn mode from the MIDI window; the button doubles as Cancel (e09s02)."""
    if state.midi_learn_mode:
        _exit_midi_learn()
        return
    if not state.midi_enabled:
        if dpg.does_item_exist("midi_learn_status"):
            dpg.set_value("midi_learn_status", "Enable MIDI first (tick Enable MIDI above)")
        return
    state.midi_learn_mode = True
    state.midi_learn_pending = None
    state.midi_learn_started_at = time.time()
    if dpg.does_item_exist("midi_learn_btn"):
        dpg.set_item_label("midi_learn_btn", "Cancel learn")
    if dpg.does_item_exist("midi_learn_status"):
        dpg.set_value("midi_learn_status", "MIDI Learn: click a viseq control")


def on_midi_enable(sender: Any, app_data: Any, user_data: Any) -> None:
    """MIDI window Enable checkbox: persist and apply the engine toggle (e09s02)."""
    set_midi_enabled(bool(app_data))
    if not app_data and state.midi_learn_mode:  # disabling cancels an in-flight learn
        _exit_midi_learn()


def _midi_binding_label(binding: dict[str, Any]) -> str:
    """Human-readable row label for one mapping (e09s02)."""
    params = binding.get("params") or {}
    suffix = f" {params}" if params else ""
    return (
        f"{binding.get('device', '?')} {binding.get('type', '?')} "
        f"{binding.get('number', '?')} -> {binding.get('action', '?')}{suffix}"
    )


def refresh_midi_mappings_ui() -> None:
    """Rebuild the Bindings list for the selected controller (main thread) (e14s03)."""
    if not dpg.does_item_exist("midi_mappings_group"):
        return
    dpg.delete_item("midi_mappings_group", children_only=True)
    bindings = selected_bindings()
    for idx, binding in enumerate(bindings):
        with dpg.group(horizontal=True, parent="midi_mappings_group"):
            dpg.add_text(_midi_binding_label(binding))
            dpg.add_button(label="Delete", callback=delete_midi_binding, user_data=idx)


def delete_midi_binding(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Remove a binding by list index from the selected controller and refresh (e14s03)."""
    idx = int(user_data)
    bindings = selected_bindings()
    if 0 <= idx < len(bindings):
        del bindings[idx]
    refresh_midi_mappings_ui()


def refresh_midi_devices(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Re-scan MIDI inputs and update the Controllers Add combo (e14s03)."""
    if dpg.does_item_exist("midi_add_combo"):
        dpg.configure_item("midi_add_combo", items=available_controller_ports())


def add_controller_from_port(port_name: str) -> None:
    """Add a MIDI input port as a controller (profile auto-detected) and persist (e14s03)."""
    if not port_name or find_controller_by_port(port_name) is not None:
        return
    profile = match_controller_profile(port_name, controller_profiles())
    has_grid = bool(profile and profile.get("features", {}).get("grid"))
    controller: dict[str, Any] = {
        "port": port_name,
        "profile_id": profile["id"] if profile else "",
        "role": "grid" if has_grid and grid_controller() is None else None,
        "bindings": [],
        "output": None,
        "auto_bindings": [],
    }
    midi_controllers.append(controller)
    save_midi_controllers()
    render_controllers_ui()


def remove_controller(port_name: str) -> None:
    """Remove a controller (closes its output) and persist (e14s03)."""
    controller = find_controller_by_port(port_name)
    if controller is not None:
        controller_disconnect(controller)
        midi_controllers.remove(controller)
    save_midi_controllers()
    render_controllers_ui()


def assign_grid_role(port_name: str) -> None:
    """Designate one controller as the sequencer grid; the role is exclusive (e14s03)."""
    for controller in midi_controllers:
        controller["role"] = "grid" if controller["port"] == port_name else None
    save_midi_controllers()
    render_controllers_ui()


def render_controllers_ui() -> None:
    """Rebuild the Controllers list rows (main thread; call after any change) (e14s03)."""
    if not dpg.does_item_exist("midi_controllers_group"):
        return
    dpg.delete_item("midi_controllers_group", children_only=True)
    for controller in midi_controllers:
        profile = controller_profile_of(controller)
        profile_name = profile.get("name", "Generic") if profile else "Generic"
        role_mark = " [grid]" if controller.get("role") == "grid" else ""
        with dpg.group(horizontal=True, parent="midi_controllers_group"):
            dpg.add_text(f"{controller['port']} - {profile_name}{role_mark}")
            if (
                profile
                and profile.get("features", {}).get("grid")
                and (controller.get("role") != "grid")
            ):
                port = controller["port"]
                dpg.add_button(
                    label="Set as grid",
                    callback=lambda s, a, p=port: assign_grid_role(p),
                    user_data=port,
                )
            port = controller["port"]
            dpg.add_button(
                label="Remove",
                callback=lambda s, a, p=port: remove_controller(p),
                user_data=port,
            )
    if dpg.does_item_exist("midi_add_combo"):
        dpg.configure_item("midi_add_combo", items=available_controller_ports())
    refresh_midi_mappings_ui()


def show_midi_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the MIDI window from the menubar, with a fresh device list, controller
    list and the selected controller's mappings (render_controllers_ui refreshes
    the mappings too)."""
    refresh_midi_devices()
    render_controllers_ui()
    dpg.show_item("midi_window")
    dpg.focus_item("midi_window")  # e17: a shown window must come to the front


# ---------- e16: Mapper (OSC property mappings -> compact controls) ----------
def _mapper_columns() -> int:
    """Number of mapping cards per row, derived from the current window width."""
    width = dpg.get_item_width("mapper_window")
    if not width:
        width = MAPPER_WINDOW_WIDTH
    return max(1, int((width - 20) / MAPPER_CARD_STRIDE))


def _mapper_card_thumb(target_id: str) -> None:
    """The tiny source thumbnail on a card; a placeholder when none exists yet."""
    tex_tags = thumbnails_data.get(target_id)
    if tex_tags:
        tex_tag = tex_tags[0]
        if dpg.does_item_exist(tex_tag):
            dpg.add_image(texture_tag=tex_tag, width=MAPPER_THUMB_W, height=MAPPER_THUMB_H)
            return
    themed_text("no thumb", slot="text_dim")


def _mapper_caption_spacer(label: str, spec: dict[str, Any]) -> int:
    """Spacer width that right-aligns the value caption on a card (e20s01).

    The caption is ONE row: label + spacer + value. The spacer fills the gap so
    the longest possible "%.2f" string of the property (min and max can differ
    in sign/length, e.g. alpha -1.00..1.00 or posterize 1.00..256.00) ends
    flush at the card's right edge, measured with the live font width.
    """
    char_px = _char_width_px()
    value_px = char_px * max(len(f"{spec['min']:.2f}"), len(f"{spec['max']:.2f}"))
    return max(2, MAPPER_CARD_W - 24 - char_px * len(label) - value_px)


def _render_mapper_card(mapping: dict[str, Any], parent: Any) -> None:
    """One compact mapping card: thumbnail + X, one-line caption, the control.

    Row order (e20s01): the source thumbnail (full card width) with the X
    delete button, then the property caption (label left, value right on a
    single row), then the control. Tags are unchanged so the band/MIDI drive
    and the source menu keep working.
    """
    mid = mapping["id"]
    spec = mapper.MAPPER_PROPERTIES[mapping["property"]]
    with dpg.child_window(
        parent=parent,
        width=MAPPER_CARD_W,
        height=MAPPER_CARD_H,
        border=True,
        no_scrollbar=True,
        tag=f"mapper_card_{mid}",
    ):
        with dpg.group(horizontal=True):
            _mapper_card_thumb(mapping["target_id"])
            dpg.add_button(
                label="X",
                width=18,
                height=18,
                callback=delete_mapping,
                user_data=mid,
                tag=f"mapper_del_{mid}",
            )
        with dpg.group(horizontal=True):
            themed_text(spec["label"], slot="text_dim", tag=f"mapper_prop_{mid}")
            dpg.add_spacer(width=_mapper_caption_spacer(spec["label"], spec))
            themed_text(f"{mapping['value']:.2f}", slot="text_bright", tag=f"mapper_val_{mid}")
        if mapping["control"] == "slider":
            dpg.add_slider_float(
                min_value=spec["min"],
                max_value=spec["max"],
                default_value=mapping["value"],
                width=MAPPER_CARD_W - 24,
                callback=on_mapper_control,
                user_data=mid,
                tag=f"mapper_slider_{mid}",
            )
        elif mapping["control"] == "knob":
            dpg.add_knob_float(
                min_value=spec["min"],
                max_value=spec["max"],
                default_value=mapping["value"],
                width=44,
                callback=on_mapper_control,
                user_data=mid,
                tag=f"mapper_knob_{mid}",
            )
        else:
            dpg.add_button(
                label=f"{spec['label']}: {mapping['value']:.2f}",
                width=MAPPER_CARD_W - 24,
                callback=on_mapper_button,
                user_data=mid,
                tag=f"mapper_btn_{mid}",
            )
        _render_mapper_source_menu(mapping)


def _render_mapper_source_menu(mapping: dict[str, Any]) -> None:
    """Right-click menu on a mapper control: Band 2/3 / MIDI Learn / Clear (e18).

    The menu lives on the CONTROL widget (slider/knob/button); the ACTIVE source
    is marked with a checkmark. Band and MIDI sources are mutually exclusive
    (see mapper.set_mapping_band).
    """
    mid = mapping["id"]
    control_tag = f"mapper_{mapping['control']}_{mid}"
    with dpg.popup(control_tag, mousebutton=dpg.mvMouseButton_Right):
        dpg.add_menu_item(
            label="Map Band 2",
            check=True,
            default_value=(mapping.get("band") == 2),
            callback=set_mapping_band,
            user_data=(mid, 2),
        )
        dpg.add_menu_item(
            label="Map Band 3",
            check=True,
            default_value=(mapping.get("band") == 3),
            callback=set_mapping_band,
            user_data=(mid, 3),
        )
        dpg.add_menu_item(
            label="MIDI Learn...",
            check=True,
            default_value=(mapping.get("midi") is not None),
            callback=map_mapping_midi_learn,
            user_data=mid,
        )
        dpg.add_separator()
        dpg.add_menu_item(label="Clear source", callback=clear_mapping_source, user_data=mid)


def set_mapping_band(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Mapper control menu: bind the control to an audio band (e18)."""
    mapping_id, band_id = user_data
    mapper.set_mapping_band(mapping_id, band_id)
    refresh_mapper_ui()


def clear_mapping_source(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Mapper control menu: drop the band/MIDI source, manual control resumes (e18)."""
    mapper.clear_mapping_source(int(user_data))
    refresh_mapper_ui()


def _set_mapper_control_value(mapping_id: int, value: float) -> None:
    """Move a mapping's control + caption from an external source (band/MIDI, e18)."""
    _set_mapper_caption(mapping_id, value)
    for kind in ("slider", "knob"):
        tag = f"mapper_{kind}_{mapping_id}"
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, value)


def drive_mapper_band(band_id: int, level: float) -> None:
    """Push an audio-band level into every control mapped to that band (e18).

    Called by refresh_band_value (main thread, ~30 fps while the band is
    enabled); the level is remapped onto each mapping's property range.
    """
    for m in state.mapper_mappings:
        if m.get("band") == band_id:
            value = mapper.apply_unit_value(m["id"], level)
            _set_mapper_control_value(m["id"], value)


def _close_mapper_learn_window() -> None:
    """Delete the mapper MIDI-Learn modal (bound, cancelled or timed out)."""
    if dpg.does_item_exist("mapper_learn_window"):
        dpg.delete_item("mapper_learn_window")


def _cancel_mapper_learn(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Mapper MIDI-Learn modal Cancel: exit learn mode and close the window."""
    _exit_midi_learn()
    _close_mapper_learn_window()


def map_mapping_midi_learn(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Mapper control menu > MIDI Learn: the next controller move binds this control.

    Reuses the existing learn machinery: midi_learn_pending carries the
    mapper_mapping action; the worker completes it via midi_learn_complete,
    which stores the binding and closes this modal automatically.
    """
    mapping_id = int(user_data)
    if dpg.does_item_exist("mapper_learn_window"):
        dpg.delete_item("mapper_learn_window")
    with dpg.window(
        label="MIDI Learn",
        tag="mapper_learn_window",
        modal=True,
        width=360,
        height=140,
        no_resize=True,
    ):
        if state.midi_enabled:
            themed_text("Move a control on your MIDI controller...", slot="text")
            themed_text(
                "The control binds to this mapping and the window closes.",
                slot="text_dim",
            )
            state.midi_learn_mode = True
            state.midi_learn_pending = (MIDI_ACTION_MAPPER_MAPPING, {"mapping_id": mapping_id})
            state.midi_learn_started_at = time.time()
        else:
            themed_text("Enable MIDI in the MIDI window first, then try again.", slot="text")
        dpg.add_separator()
        dpg.add_button(label="Cancel", callback=_cancel_mapper_learn, width=120)
    dpg.show_item("mapper_learn_window")


def midi_mapping_value(mapping_id: int, midi_value: int) -> None:
    """Drive a mapper control from a learned MIDI value (0..127 -> range, e18)."""
    unit = max(0.0, min(1.0, midi_value / 127.0))
    value = mapper.apply_unit_value(mapping_id, unit)
    _set_mapper_control_value(mapping_id, value)


def tick_midi_learn_timeout() -> None:
    """Expire a stale MIDI Learn session (mapper learn included) past the timeout."""
    if state.midi_learn_mode and (
        time.time() - state.midi_learn_started_at > MIDI_LEARN_TIMEOUT_SECONDS
    ):
        _exit_midi_learn()
        _close_mapper_learn_window()


def refresh_mapper_ui() -> None:
    """Rebuild the Mapper window body from state.mapper_mappings (main thread)."""
    if not dpg.does_item_exist("mapper_mappings_group"):
        return
    dpg.delete_item("mapper_mappings_group", children_only=True)
    if not state.mapper_mappings:
        # explicit parent: at runtime (menu callback) DPG cannot deduce the
        # implicit container, so a parentless add_text would raise 1011
        themed_text(
            "No mappings yet — right-click a source in the Mediagrid.",
            slot="text_dim",
            wrap=MAPPER_WINDOW_WIDTH - 40,
            parent="mapper_mappings_group",
        )
        return
    # e20s02: cards sit in horizontal row groups (fixed-width child windows
    # align across rows) — the old table grid drew an outer frame and shipped
    # its own right-click menu that fought the per-card popup.
    cols = _mapper_columns()
    for i in range(0, len(state.mapper_mappings), cols):
        row = dpg.add_group(horizontal=True, parent="mapper_mappings_group")
        for mapping in state.mapper_mappings[i : i + cols]:
            _render_mapper_card(mapping, parent=row)


def show_mapper_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Windows menu: open the Mapper window with a fresh mappings body (e16)."""
    refresh_mapper_ui()
    dpg.show_item("mapper_window")
    dpg.focus_item("mapper_window")  # e17: a shown window must come to the front


def _set_mapper_caption(mid: int, value: float) -> None:
    """Refresh a card's value caption after a control change."""
    val_tag = f"mapper_val_{mid}"
    if dpg.does_item_exist(val_tag):
        dpg.set_value(val_tag, f"{value:.2f}")


def on_mapper_control(sender: Any, app_data: Any, user_data: Any) -> None:
    """Slider/knob change: send the clamped OSC value, refresh the caption."""
    mid = int(user_data)
    value = mapper.send_mapping_value(mid, float(app_data))
    _set_mapper_caption(mid, value)


def on_mapper_button(sender: Any, app_data: Any, user_data: Any) -> None:
    """Button press: toggle min/max, send OSC, refresh the card label + caption."""
    mid = int(user_data)
    value = mapper.send_button_mapping(mid)
    _set_mapper_caption(mid, value)
    mapping = mapper.find_mapping(mid)
    if mapping is not None:
        spec = mapper.MAPPER_PROPERTIES[mapping["property"]]
        btn_tag = f"mapper_btn_{mid}"
        if dpg.does_item_exist(btn_tag):
            dpg.configure_item(btn_tag, label=f"{spec['label']}: {value:.2f}")


def delete_mapping(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """X on a mapping card: remove the mapping and refresh the Mapper body."""
    mapper.remove_mapping(int(user_data))
    refresh_mapper_ui()


def open_new_mapping_dialog(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Mediagrid tile right-click > New Mapping...: modal asking property + control."""
    state.mapper_pending_target = str(user_data)
    if dpg.does_item_exist("mapper_new_dialog"):
        dpg.delete_item("mapper_new_dialog")
    with dpg.window(
        label="New Mapping",
        tag="mapper_new_dialog",
        modal=True,
        width=320,
        height=250,
        no_resize=True,
    ):
        themed_text(f"Source: {state.mapper_pending_target}", slot="text")
        dpg.add_separator()
        themed_text("Property", slot="text_dim")
        dpg.add_combo(
            items=list(mapper.MAPPER_PROPERTIES),
            default_value="brightness",
            width=260,
            tag="mapper_prop_combo",
        )
        themed_text("Control", slot="text_dim")
        dpg.add_combo(
            items=list(mapper.MAPPER_CONTROLS),
            default_value="slider",
            width=260,
            tag="mapper_control_combo",
        )
        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_button(label="Create", callback=mapper_dialog_confirm, width=120)
            dpg.add_button(label="Cancel", callback=mapper_dialog_cancel, width=120)
    dpg.show_item("mapper_new_dialog")


def mapper_dialog_confirm(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Dialog Create: add the mapping chosen in the combos and open the Mapper window."""
    if not dpg.does_item_exist("mapper_new_dialog"):
        return
    prop = str(dpg.get_value("mapper_prop_combo"))
    control = str(dpg.get_value("mapper_control_combo"))
    target = state.mapper_pending_target
    dpg.delete_item("mapper_new_dialog")
    if target is None:
        return
    mapper.add_mapping(target, prop, control)
    refresh_mapper_ui()
    dpg.show_item("mapper_window")


def mapper_dialog_cancel(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Dialog Cancel: close without creating a mapping."""
    if dpg.does_item_exist("mapper_new_dialog"):
        dpg.delete_item("mapper_new_dialog")


def midi_control_loop() -> None:
    """MIDI control worker (e14s02): poll every controller input port, route messages,
    push executions to the main thread via ui_task_queue (HIGH-1 — no direct dpg calls).
    """
    try:
        import mido
    except ImportError:
        return
    open_ports: dict[str, Any] = {}
    while True:
        if not state.midi_enabled:
            time.sleep(0.2)
            continue
        wanted = {c["port"] for c in midi_controllers}
        for port_name in [p for p in open_ports if p not in wanted]:
            _close_midi_input(open_ports.pop(port_name))
        for controller in midi_controllers:
            port_name = controller["port"]
            if port_name not in open_ports:
                try:
                    open_ports[port_name] = mido.open_input(port_name)
                    controller_connect(controller, mido)  # e14s02: LED output + grid bindings
                    append_log("MIDI", f"Control listening on {port_name}")
                except Exception as e:
                    log_error("MIDI", str(e))
                    time.sleep(2.0)
                    continue
            try:
                for msg in open_ports[port_name].iter_pending():
                    handle_midi_message(msg, port_name)
            except Exception as e:
                log_error("MIDI", str(e))
                _close_midi_input(open_ports.pop(port_name, None))
        time.sleep(0.002)


# ---------- e09s03: Novation Launchpad adapter ----------
# MULTI-CONTROLLER RUNTIME (e14s02) — profile-driven controllers, one designated
# sequencer grid; the legacy launchpad_* path stays until e14s04 removes it.
# ==============================================================================
# Persisted shape: {port, profile_id, role ("grid"|None), bindings} + runtime
# fields output (mido out port) and auto_bindings (grid bindings, never persisted).
# Semantic grid-LED names (mirror/flash call sites) mapped per profile palette.


def midi_clock_loop() -> None:
    """Listen for MIDI clock (0xF8, 24 pulses/beat) and fire the sequencer beat in MIDI mode.

    The clock listens on cfg clock_source when set, else the first available input;
    without any port it logs once and idles so the app keeps running.
    """
    try:
        import mido
    except ImportError:
        return
    while True:
        port_name = _clock_port_name()
        if not port_name:
            time.sleep(10)
            continue
        try:
            with mido.open_input(port_name) as port:
                append_log("MIDI", f"Clock listening on {port_name}")
                while True:
                    for msg in port.iter_pending():
                        if msg.type == "clock":
                            state.midi_pulses += 1
                            if state.midi_pulses >= MIDI_CLOCK_PULSES_PER_BEAT:
                                state.midi_pulses = 0
                                flash_led("led_midi")
                                if state.beat_source == BEAT_SOURCE_MIDI and state.is_playing:
                                    sync_event_beat.set()
                    time.sleep(0.001)
        except Exception as e:
            log_error("MIDI", str(e))
            time.sleep(10)


def show_settings_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the general settings window from the top menubar."""
    dpg.show_item("settings_window")
    dpg.focus_item("settings_window")  # e17: a shown window must come to the front


def show_logs_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the OSC logs window from the top menubar (Show > Logs)."""
    dpg.show_item("logs_window")
    dpg.focus_item("logs_window")  # e17: a shown window must come to the front


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
    dpg.focus_item("help_window")  # e17: a shown window must come to the front


# ---------- e17: window switching (Windows-menu list + Ctrl+Tab) ----------
def _window_menu_entries() -> list[tuple[str, str]]:
    """Workspace windows in switching order: (tag, menu label).

    Monitor Players are appended live so the list (and Ctrl+Tab) always match
    the windows that exist.
    """
    entries = [
        ("sequencer_window", "Sequencer"),
        ("audio_window", "Audio"),
        ("vimix_media_window", "Media"),
        ("logs_window", "Logs"),
        ("mapper_window", "Mapper"),
    ]
    entries += [(p["tag"], f"Monitor {p['id']}") for p in monitor_players]
    return entries


_window_menu_dynamic_tags: list[str] = []  # live list items, deleted on refresh


_window_menu_sig: tuple[Any, ...] | None = None  # last (active, monitor tags) seen


def refresh_window_menu() -> None:
    """Rebuild the Windows-menu window list and mark the ACTIVE window (e17).

    The list lives under the ``Windows`` menu (after its separator); each entry
    is a checkable item wired to ``switch_to_window``. Missing windows are
    skipped so a pruned Monitor Player never leaves a dead entry.
    """
    for tag in _window_menu_dynamic_tags:
        if dpg.does_item_exist(tag):
            dpg.delete_item(tag)
    _window_menu_dynamic_tags.clear()
    active = dpg.get_active_window()
    for tag, label in _window_menu_entries():
        if not dpg.does_item_exist(tag):
            continue
        item_tag = dpg.add_menu_item(
            label=label,
            check=True,
            default_value=(str(active) == str(tag)),
            callback=switch_to_window,
            user_data=tag,
            parent="menu_windows",
        )
        _window_menu_dynamic_tags.append(item_tag)


def tick_window_menu() -> None:
    """Per-frame gate: refresh the Windows-menu list only when it can have changed.

    The signature is (active window, monitor-player tags); anything else the list
    shows (the fixed windows) is static. One get_active_window per frame is the
    whole cost when nothing changed.
    """
    global _window_menu_sig
    sig = (dpg.get_active_window(), tuple(p["tag"] for p in monitor_players))
    if sig != _window_menu_sig:
        _window_menu_sig = sig
        refresh_window_menu()


def switch_to_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Show + focus a window (Windows-menu list click, Ctrl+Tab target)."""
    tag = user_data
    if dpg.does_item_exist(tag):
        dpg.show_item(tag)
        dpg.focus_item(tag)


def on_cycle_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Ctrl+Tab / Ctrl+Shift+Tab: cycle through the SHOWN workspace windows.

    DPG 2.3.1 key handlers have no modifier support, so the wrapper checks the
    modifier keys itself; Tab keeps its normal role while an input is focused.
    """
    if not dpg.is_key_down(dpg.mvKey_ModCtrl) or _any_input_focused():
        return
    forward = not dpg.is_key_down(dpg.mvKey_ModShift)
    entries = _window_menu_entries()
    if not entries:
        return
    active = dpg.get_active_window()
    try:
        idx = next(i for i, (tag, _) in enumerate(entries) if str(tag) == str(active))
    except StopIteration:
        idx = -1
    step = 1 if forward else -1
    for _ in range(len(entries)):
        idx = (idx + step) % len(entries)
        tag, _ = entries[idx]
        if dpg.does_item_exist(tag) and dpg.is_item_shown(tag):
            switch_to_window(None, None, tag)
            return


def open_github(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the author's GitHub profile in the default browser (Help window link)."""
    try:
        import webbrowser

        webbrowser.open(GITHUB_URL)
    except Exception as e:
        log_error("Help", str(e))


def callback_resync(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    state.current_step = -1
    for r in range(NUM_TRACKS):
        tracks_data[r]["active_fade"]["active"] = False
    sync_event_seq.set()
    sync_event_led.set()


def callback_nudge_backward(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    state.phase_nudge += 0.05


def callback_nudge_forward(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    state.phase_nudge -= 0.05


def refresh_band_value(bars: np.ndarray, band_id: int) -> None:
    """Recompute one band's level from its sliders; update text and overlay (main thread)."""
    if not bands_enabled[band_id]:
        return
    f_start = float(dpg.get_value(f"band{band_id}_start"))
    f_end = float(dpg.get_value(f"band{band_id}_end"))
    l_min = float(dpg.get_value(f"band{band_id}_min"))
    l_max = float(dpg.get_value(f"band{band_id}_max"))
    value = band_value_from_bars(bars, f_start, f_end, l_min, l_max, agg="blend")
    _set_band_variable(band_id, value)
    drive_mapper_band(band_id, value)  # e18: mapped controls follow the band
    # Beat trigger: any band rising to the threshold flashes its LED; only band 1 can
    # drive the sequencer beat (edge only) — bands 2/3 stay spectrum-only (e10s07)
    if value >= BAND_BEAT_THRESHOLD and band_prev_values[band_id] < BAND_BEAT_THRESHOLD:
        flash_led(f"led_band{band_id}")
        if band_id == 1 and state.beat_source == BEAT_SOURCE_BAND1:
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
    n = len(bars)
    bw = SPEC_DRAWLIST_W / n
    for i, level in enumerate(bars):
        h = level * (SPEC_DRAWLIST_H - 4)
        dpg.configure_item(
            f"spec_bar_{i}",
            pmin=(i * bw + 1, SPEC_DRAWLIST_H - h),
            pmax=((i + 1) * bw - 1, SPEC_DRAWLIST_H - 2),
        )
    state.spectrum_bars_cache = bars
    refresh_bands(bars)


def on_band_enable(sender: Any, app_data: Any, user_data: Any) -> None:
    """Show/hide a band's overlay and (re)compute it when the checkbox toggles."""
    band_id = int(user_data)
    bands_enabled[band_id] = bool(app_data)
    if bands_enabled[band_id]:
        refresh_band_value(state.spectrum_bars_cache, band_id)
    else:
        _set_band_variable(band_id, 0.0)
        dpg.set_value(f"band{band_id}_value_text", "—")
        dpg.configure_item(f"band{band_id}_rect", show=False)


def on_band_change(sender: Any, app_data: Any, user_data: Any) -> None:
    """Refresh a band when its selection sliders move."""
    refresh_band_value(state.spectrum_bars_cache, int(user_data))


def spectrum_analyzer_loop() -> None:
    """Compute the spectrum ~30x/s, AGC-normalize it, enqueue the redraw (HIGH-1)."""
    while True:
        if state.is_audio_analyzing:
            try:
                bars = compute_spectrum_bars(get_audio_snapshot())
                bars, state.spec_peak_hold = apply_spectrum_agc(bars, state.spec_peak_hold)
                ui_task(partial(update_spectrum_ui, bars))
            except Exception as e:
                log_error("Spectrum", str(e))
        time.sleep(1.0 / SPECTRUM_FPS)


def essentia_analyzer_loop() -> None:
    last_error = ""
    while True:
        if state.is_beat_tracking and state.beat_source == BEAT_SOURCE_ANALYSIS:
            try:
                audio_slice = essentia.array(get_audio_snapshot())
                if np.max(np.abs(audio_slice)) > 0.005:
                    if state.lowpass_enabled:
                        audio_slice = lowpass_filter(audio_slice)
                    bpm, _, confidence, _, _ = rhythm_extractor(audio_slice)
                    if confidence > 0.2 or state.beat_confidence == 0.0:
                        state.current_bpm = float(bpm)
                        state.beat_confidence = float(confidence)
                        state.bpm_last_detected = time.time()  # a real reading, not stale (e10s08)
                        enqueue_set_value(
                            "testo_bpm",
                            f"BPM: {state.current_bpm:.0f}",  # no confidence (e10s08)
                        )
            except Exception as e:
                # Log each distinct failure once, not every second
                err = f"{type(e).__name__}: {e}"
                if err != last_error:
                    last_error = err
                    log_error("BPM analysis", err)
        time.sleep(1.0)


def visual_metronome_loop() -> None:
    while True:
        if state.is_beat_tracking and state.current_bpm > 0 and not state.is_playing:
            if not _timed_bpm_live():
                time.sleep(0.05)
                continue  # no live tempo: don't flash a stale BPM (e10s08)
            base_sleep = 60.0 / state.current_bpm
            actual_sleep = max(0.0, base_sleep + state.phase_nudge)
            led_tag = BEAT_LED_TAGS.get(state.beat_source)
            if led_tag:
                flash_led(led_tag)
            sync_event_led.wait(actual_sleep)
            if sync_event_led.is_set():
                sync_event_led.clear()
            state.phase_nudge = 0.0
        else:
            time.sleep(0.1)


# ==============================================================================
# NEW ASYNC THREAD FOR HIGH-RESOLUTION FADES
# ==============================================================================
def fade_tick_loop() -> None:
    while True:
        if state.is_playing:
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


def sequencer_tick() -> None:
    while True:
        if state.is_playing:
            if beat_is_event_driven():
                # Band/MIDI modes: wait for the beat event. The wait is polled so a
                # beat-source switch or STOP always breaks through — an unbounded wait
                # strands the tick thread in a mode that no longer fires (BUG-2026-08-27T213000).
                if not sync_event_beat.wait(0.1):
                    continue  # no beat this poll: re-evaluate mode/stop
                sync_event_beat.clear()
                state.phase_nudge = 0.0
            else:
                if not _timed_bpm_live():
                    time.sleep(0.05)
                    continue  # no live tempo: never advance on a stale BPM (e10s08)
                base_sleep = 60.0 / state.current_bpm if state.current_bpm > 0 else 0.5
                actual_sleep = max(0.0, base_sleep + state.phase_nudge)
                state.phase_nudge = 0.0
                sync_event_seq.wait(actual_sleep)
                if sync_event_seq.is_set():
                    sync_event_seq.clear()

            prev_step = state.current_step
            state.current_step = (state.current_step + 1) % NUM_STEPS
            grid_flash_playhead()  # e14s02: beat flash on the new playhead column

            for r, track in enumerate(tracks_data):
                if prev_step != -1:
                    update_step_theme(r, prev_step, is_head=False)
                update_step_theme(r, state.current_step, is_head=True)

                step_data = track["steps"][state.current_step]

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
                                tag_v1 = f"rand_v1_{r}_{state.current_step}"
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
                                send_colorv_step(track, r, state.current_step)

                            elif step_data["type"] == "ColorR":
                                send_colorr_step(track, r, state.current_step)

                            elif step_data["type"] == "SeekR":
                                send_seekr_step(track, r, state.current_step)

                        except Exception as e:
                            print(f"[viseq OSC Error] {e}")

            led_tag = BEAT_LED_TAGS.get(state.beat_source)
            if led_tag:
                flash_led(led_tag)
        else:
            time.sleep(0.1)


def on_lowpass_toggle(sender: Any, app_data: Any, user_data: Any) -> None:
    state.lowpass_enabled = bool(app_data)


def toggle_audio_stream(sender: Any, app_data: Any, user_data: Any) -> None:
    global audio_stream
    if user_data == "vu_meter":
        state.is_audio_analyzing = app_data
    elif user_data == "beat_tracking":
        state.is_beat_tracking = app_data
    needs_stream = state.is_audio_analyzing or state.is_beat_tracking

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
    state.is_playing = not state.is_playing
    if not state.is_playing:
        for r in range(NUM_TRACKS):
            for c in range(NUM_STEPS):
                update_step_theme(r, c, is_head=False)
            tracks_data[r]["active_fade"]["active"] = False
        state.current_step = -1
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
    # e17: Ctrl+Tab / Ctrl+Shift+Tab cycle the workspace windows (the callback
    # checks the modifiers and the input-focus guard itself).
    dpg.add_key_press_handler(dpg.mvKey_Tab, callback=on_cycle_window)


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

with dpg.theme() as theme_seq_row_compact, dpg.theme_component(dpg.mvAll):
    # Tighter item spacing for the sequencer transport/beat-source row (e10s08):
    # the default 8 px between ~19 items pushed the row past the window width.
    dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 4, 4)

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
    # Single compact row: transport + all beat sources (abbreviated labels, e10s08).
    # The manual widgets (input/TAP/readout) stay hidden until manual mode is selected.
    with dpg.group(horizontal=True, tag="seq_transport_row"):
        dpg.bind_item_theme("seq_transport_row", theme_seq_row_compact)
        dpg.add_button(
            label="PLAY",
            tag="btn_play",
            callback=learnable(toggle_play, lambda ud: (MIDI_ACTION_TRANSPORT_PLAY, {})),
            width=60,
            height=26,
        )
        dpg.add_button(
            label="<",
            callback=learnable(callback_nudge_backward, lambda ud: (MIDI_ACTION_NUDGE_BACK, {})),
            width=28,
            height=26,
        )
        dpg.add_button(
            label="RESYNC",
            callback=learnable(callback_resync, lambda ud: (MIDI_ACTION_TRANSPORT_RESYNC, {})),
            width=50,
            height=26,
        )
        dpg.add_button(
            label=">",
            callback=learnable(callback_nudge_forward, lambda ud: (MIDI_ACTION_NUDGE_FORWARD, {})),
            width=28,
            height=26,
        )
        dpg.add_spacer(width=8)
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
        dpg.add_text("BPM: ---", tag="testo_bpm", color=(150, 255, 150, 255))
        dpg.add_spacer(width=6)
        dpg.add_checkbox(
            label=BEAT_SOURCE_LABELS[BEAT_SOURCE_BAND1],
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
        dpg.add_spacer(width=6)
        dpg.add_checkbox(
            label=BEAT_SOURCE_LABELS[BEAT_SOURCE_MIDI],
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
        dpg.add_spacer(width=6)
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
        dpg.add_spacer(width=4)
        dpg.add_input_int(
            default_value=120,
            min_value=30,
            max_value=300,
            width=70,
            tag="manual_bpm_input",
            callback=on_manual_bpm,
            show=False,
        )
        dpg.add_button(
            label="TAP",
            tag="btn_tap",
            callback=learnable(tap_bpm, lambda ud: (MIDI_ACTION_TRANSPORT_TAP, {})),
            width=32,
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
            dpg.add_text("-", tag=f"band{band_id}_value_text", color=(230, 230, 120, 255))
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
    label="General", width=340, height=320, pos=(370, 820), tag="settings_window", show=False
):
    # e11s04: Project section first — restore-last-project-at-boot replaces the
    # removed Windows layout save/restore section.
    themed_text("Project", slot="text")
    dpg.add_separator()
    dpg.add_checkbox(
        label="Restore last project at startup",
        tag="cb_restore_project_boot",
        default_value=True,
        callback=on_restore_project_boot_toggle,
    )
    dpg.add_spacer(height=8)
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
            default_value=palette_rgba(state.active_palette[slot]),
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
with dpg.window(label="Logs", width=950, height=150, pos=(720, 820), tag="logs_window", show=False):
    dpg.add_text("Waiting for OSC traffic...", tag="osc_log_text")

# WINDOW 6: HELP / ABOUT (hidden; opened from the menubar "Help", re-centered on open, e08)
with dpg.window(
    label="Info",
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
    themed_text("viSeq - Audio-Reactive VJ Controller for Vimix", slot="text_bright")
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

with dpg.window(label="MIDI", width=520, height=520, pos=(560, 320), tag="midi_window", show=False):
    dpg.add_checkbox(
        label="Enable MIDI",
        tag="midi_enable_cb",
        default_value=state.midi_enabled,
        callback=on_midi_enable,
    )
    dpg.add_separator()
    dpg.add_spacer(height=4)
    # e14s03: Controllers section — add any available input port, list the connected
    # controllers (profile auto-detected, grid role, remove), bindings per controller.
    themed_text("Controllers", slot="text")
    with dpg.group(horizontal=True):
        dpg.add_combo(items=available_controller_ports(), tag="midi_add_combo", width=320)
        dpg.add_button(
            label="Add",
            callback=lambda: add_controller_from_port(str(dpg.get_value("midi_add_combo"))),
            width=60,
        )
        dpg.add_button(label="Refresh", callback=refresh_midi_devices, width=80)
    with (
        dpg.child_window(height=120, tag="midi_controllers_scroll"),
        dpg.group(tag="midi_controllers_group"),
    ):
        pass
    dpg.add_separator()
    dpg.add_spacer(height=4)
    themed_text("Bindings", slot="text")
    dpg.add_button(
        label="Learn mapping...", tag="midi_learn_btn", callback=toggle_midi_learn, width=150
    )
    dpg.add_text("", tag="midi_learn_status")
    dpg.add_separator()
    dpg.add_spacer(height=4)
    with (
        dpg.child_window(height=160, tag="midi_mappings_scroll"),
        dpg.group(tag="midi_mappings_group"),
    ):
        pass
    dpg.add_spacer(height=4)
    dpg.add_button(label="Save", callback=save_midi_controllers, width=80)

# e16: Mapper window — compact grid of OSC property mapping cards; the body is
# rebuilt by refresh_mapper_ui() (menu open, create, delete, prune). Hidden at
# boot and never part of the saved layout (transient workspace, like Logs).
# e20s02: only the mapping cards — no header line, and the scroll
# container is borderless so no outer frame wraps the grid.
with (
    dpg.window(
        label="Mapper",
        width=MAPPER_WINDOW_WIDTH,
        height=MAPPER_WINDOW_HEIGHT,
        pos=(60, 60),
        tag="mapper_window",
        show=False,
    ),
    dpg.child_window(height=MAPPER_WINDOW_HEIGHT - 8, border=False, tag="mapper_scroll"),
    dpg.group(tag="mapper_mappings_group"),
):
    pass

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
# e19: closing the main window asks for confirmation — the viewport X routes to
# the exit modal instead of stopping the app (disable_close keeps rendering on;
# the modal's Exit button calls stop_dearpygui).
dpg.set_exit_callback(show_exit_confirm)
dpg.configure_viewport("__viewport", disable_close=True)
apply_boot_config()  # e06: apply the saved theme + (optionally) the saved window layout
with dpg.viewport_menu_bar():
    with dpg.menu(label="viSeq"):  # e11s03: first menubar menu — project file flows
        dpg.add_menu_item(label="New project", callback=show_new_project_confirm)  # e15s01
        dpg.add_menu_item(label="Open project", callback=show_open_project_dialog)
        with dpg.menu(label="Last project", tag="menu_last_project"):
            pass  # children rebuilt by rebuild_last_project_menu() (boot + after every save/open)
        dpg.add_separator()
        dpg.add_menu_item(label="Save project", callback=show_save_project_dialog)
        dpg.add_menu_item(label="Exit", callback=exit_app)
    with dpg.menu(label="Windows", tag="menu_windows"):  # e12s01 + e17 (window list)
        dpg.add_menu_item(label="New Monitor Player", callback=new_monitor_player)
        dpg.add_menu_item(label="Show Mapper", callback=show_mapper_window)  # e16
        dpg.add_menu_item(label="Show Logs", callback=show_logs_window)
        dpg.add_menu_item(label="Show Info", callback=show_help_window)
        dpg.add_separator(parent="menu_windows")  # e17: open windows below the actions
        # the live window list is rebuilt by refresh_window_menu() (e17)
    with dpg.menu(label="Settings"):  # e12s01: config panels under one menu
        dpg.add_menu_item(label="General", callback=show_settings_window)
        dpg.add_menu_item(label="MIDI", callback=show_midi_window)

# e11s03/e13s02: project file dialogs are created ON DEMAND by
# show_open_project_dialog / show_save_project_dialog (_recreate_project_dialog)
# with .viseq/.* filters — DPG shows only directories without extension filters,
# and a fresh dialog guarantees the default path exists.
rebuild_last_project_menu()  # e11s03: populate the Last-project submenu for boot
dpg.setup_dearpygui()
dpg.show_viewport()
autostart_osc()  # boot: auto-connect OSC client + start listening server (no manual clicks)

try:
    while dpg.is_dearpygui_running():
        if dpg.does_item_exist("vimix_media_window"):
            w = dpg.get_item_width("vimix_media_window")
            current_cols = max(1, int((w - 20) / 145))
            if current_cols != state.last_num_cols:
                state.last_num_cols = current_cols
                if state.global_vimix_state.get("sources"):
                    update_vimix_sources_ui(json.dumps(state.global_vimix_state))

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

        tick_window_menu()  # e17: keep the Windows-menu list + active mark fresh

        tick_midi_learn_timeout()  # e18: expire stale MIDI Learn sessions (incl. mapper)

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
    if state.local_osc_server is not None:
        with contextlib.suppress(Exception):
            state.local_osc_server.shutdown()
    dpg.destroy_context()
