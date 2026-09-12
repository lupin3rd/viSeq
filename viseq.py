import contextlib
import copy
import json
import math
import os
import queue
import random  # noqa: F401 — module attribute (test harness patches viseq.random)
import shutil
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import dearpygui.dearpygui as dpg
import essentia
import numpy as np
import sounddevice as sd
from PIL import Image
from pythonosc import dispatcher, udp_client

import viseqapp  # noqa: F401  scaffold hook (REFACTOR_LATEST.md commit 1): proves the package import path works at boot
from viseqapp import actions, catalog, cue, leap, mapper, midimonitor, preview, routeengine, state
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
from viseqapp.config import _sanitize_palette, load_config, save_config, user_config_dir
from viseqapp.constants import (
    BAND_BEAT_THRESHOLD,
    BEAT_SOURCE_ANALYSIS,
    BEAT_SOURCE_BAND1,
    BEAT_SOURCE_LABELS,
    BEAT_SOURCE_MANUAL,
    BEAT_SOURCE_MIDI,
    DEFAULT_MANUAL_BPM,
    DEFAULT_PALETTE,
    DEST_MIDI,
    DEST_OSC,
    FRAME_SLEEP_ANIMATED,
    FRAME_SLEEP_IDLE,
    HELP_ASCII_LOGO,
    HELP_LOGO_INDENT,
    HELP_WINDOW_HEIGHT,
    HELP_WINDOW_WIDTH,
    LAYOUT_ALWAYS_HIDDEN_TAGS,
    LAYOUT_WINDOW_TAGS,
    LOG_HISTORY_LIMIT,
    MAPPER_ADD_H,
    MAPPER_ADD_SLOT_W,
    MAPPER_ADD_W,
    MAPPER_BAND_DRAG_W,
    MAPPER_CB_W,
    MAPPER_CTRL_H,
    MAPPER_DRAG_W,
    MAPPER_KNOB_H,
    MAPPER_LEARN_SLOTS,
    MAPPER_LINE_NO_DIGIT_PX,
    MAPPER_LINE_NO_FONT_SIZE,
    MAPPER_LINE_NO_TEXT_H,
    MAPPER_LINE_NO_W,
    MAPPER_MARKER_H,
    MAPPER_MARKER_W,
    MAPPER_MAX_MAPPINGS,
    MAPPER_MINI_W,
    MAPPER_RESET_H,
    MAPPER_RESET_W,
    MAPPER_ROW_GAP,
    MAPPER_ROW_PAD_V,
    MAPPER_ROW_THUMB_H,
    MAPPER_ROW_THUMB_W,
    MAPPER_SMALL_CHAR_PX,
    MAPPER_TEXT_H,
    MAPPER_WINDOW_HEIGHT,
    MAPPER_WINDOW_WIDTH,
    MAPPER_X_H,
    MAPPER_X_W,
    MARKER_GROUP_GAP,
    MEDIA_ALPHA_SLIDER_W,
    MEDIA_BADGE_H,
    MEDIA_BADGE_W,
    MEDIA_TILE_H,
    MEDIA_TILE_PAD,
    MEDIA_TITLE_CHAR_PX,
    MEDIA_TITLE_CHARS_PER_LINE,
    MEDIA_TITLE_ELLIPSIS,
    MEDIA_TITLE_FONT_SIZE,
    MEDIA_TITLE_GAP,
    MEDIA_TITLE_MAX_LINES,
    MEDIA_TITLE_RESERVE_PX,
    MEDIA_TITLE_WRAP,
    MIDI_ACTION_BEAT_SOURCE,
    MIDI_ACTION_ENABLE_CORRECTION,
    MIDI_ACTION_MAPPER_BAND,
    MIDI_ACTION_MAPPER_CUE_OPEN,
    MIDI_ACTION_MAPPER_ENABLE,
    MIDI_ACTION_MAPPER_LINE,
    MIDI_ACTION_MAPPER_MAPPING,
    MIDI_ACTION_MAPPER_RESET,
    MIDI_ACTION_MONITOR_TOGGLE,
    MIDI_ACTION_NUDGE_BACK,
    MIDI_ACTION_NUDGE_FORWARD,
    MIDI_ACTION_REGEN_SELECTED,
    MIDI_ACTION_ROUTE_TOGGLE,
    MIDI_ACTION_SEQ_ROW_ASSIGN,
    MIDI_ACTION_SEQ_ROW_DISABLE,
    MIDI_ACTION_SEQ_ROW_ENABLE,
    MIDI_ACTION_SEQ_TOGGLE,
    MIDI_ACTION_SOURCE_NEXT,
    MIDI_ACTION_SOURCE_PREV,
    MIDI_ACTION_TRACK_ASSIGN,
    MIDI_ACTION_TRANSPORT_PLAY,
    MIDI_ACTION_TRANSPORT_RESYNC,
    MIDI_ACTION_TRANSPORT_TAP,
    MIDI_CC_TRIGGER_THRESHOLD,
    MIDI_CLOCK_PULSES_PER_BEAT,
    MIDI_KIND_CC,
    MIDI_KIND_NOTE,
    MIDI_LEARN_TIMEOUT_SECONDS,
    MIDI_MONITOR_OUTCOME_LEARN,
    MIDI_MONITOR_OUTCOME_MATCH,
    MIDI_MONITOR_OUTCOME_NOBIND,
    MIDI_MONITOR_OUTCOME_NOMATCH,
    MIDI_MONITOR_REFRESH_INTERVAL,
    MIDI_OPEN_RETRY_COOLDOWN_SECONDS,
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
    ORIGIN_CLOCK,
    ORIGIN_CONST,
    ORIGIN_CONTROL,
    ORIGIN_STATE,
    PREVIEW_PORT,
    PREVIEW_SPEED_DEFAULT,
    PREVIEW_SPEED_MAX,
    PREVIEW_SPEED_MIN,
    PREVIEW_SPEED_STEP,
    PROJECT_FILE_EXTENSION,
    PROJECT_FORMAT,
    PROJECT_VERSION,
    RECENT_PROJECTS_MAX,
    ROUTE_DEFAULT_CADENCE_MS,
    ROUTE_MIN_CADENCE_MS,
    ROUTE_STEPS_CONTINUOUS,
    ROUTE_TICK_INTERVAL_S,
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
from viseqapp.leap import (
    leap_init_from_config,
    normalize_tracking_event,
    set_leap_enabled,
    set_leap_visualizer,
)
from viseqapp.midi import (
    _clock_port_name,
    _close_midi_input,
    _parse_midi_msg,
    apply_project_mapper_bindings,
    available_controller_ports,
    binding_source_from_message,
    controller_connect,
    controller_disconnect,
    controller_profile_of,
    controller_profiles,
    ensure_route_output,
    find_controller_by_port,
    grid_controller,
    grid_flash_playhead,
    grid_mirror_step,
    midi_init_from_config,
    midi_open_retry_due,
    project_mapper_bindings,
    resolve_midi_message,
    save_midi_controllers,
    scan_midi_inputs,
    selected_bindings,
    send_route_midi,
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
    execute_step,
    more_step_offerings,
    parse_step_token,
    send_colorr_step,  # noqa: F401 — facade re-export (test harness drives these)
    send_colorv_step,  # noqa: F401 — facade re-export (test harness drives these)
    send_seekr_step,  # noqa: F401 — facade re-export (test harness drives these)
    step_modes_covered_by_legacy,
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
# The per-user config lives under the XDG config dir (e21s01); the project
# files folder and the eager boot dirs are e21s01 task 5 (AppImage-safe: the
# app dir is read-only inside the bundle).

# viseq application version — single source of truth (matches specs/release-plan.yaml, e08s02).
# e13s01: this is the first real release of viSeq (user decision).
# e20s03: 0.2.0 — viseqapp refactor + controller profiles + new project + Mapper family.
# 0.5.0 — source Preview (viOSC 0.3.0), cue lists, persistent MIDI-learn Mapper, Save as.
# 0.4.0 — Leap Motion mapper source, per-mapping reset, project save + OSC config persist,
# Mapper tile/row workflows (thumb assign, Add-to-Mapper submenu, line numbers).
# 0.3.0 — Mapper family (rows/remap/enable/cycle), compact Vimix-sources grid, windows, XDG.
APP_VERSION: str = "0.5.1"

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

# Compact monospace font for the Mediagrid tile titles (ProggyTiny: the pixel-font
# family of DPG's default ProggyClean, but smaller; its line height equals the font
# size, so a two-line title stays tight). Bundled under viseqapp/assets so packaged
# builds carry it; if missing the title falls back to the default 13 px font.
_TILE_TITLE_FONT_PATHS: tuple[str, ...] = (
    str(Path(__file__).resolve().parent / "viseqapp" / "assets" / "ProggyTiny.ttf"),
    str(Path(__file__).resolve().parent / "assets" / "ProggyTiny.ttf"),
)
_tile_title_font: Any = None

# e32s02: a larger ProggyTiny for the Mapper row line numbers (same guarded pattern;
# missing asset -> None -> the numbers fall back to the 10 px mapper font).
_mapper_line_no_font: Any = None

# --- e09: MIDI control engine (single mido stack; notes + CCs, user-configurable bindings) ---


# MIDI control runtime mirrors of cfg["midi"] (e09). The worker thread reads these; the
# main thread writes them. Bindings: [{device, channel, type("note"/"cc"/"pitch"), number,
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
PROJECTS_DIR = os.path.join(user_config_dir(), "projects")
# The pre-e21 projects default (app dir); one-time migration source (e21s01).
LEGACY_PROJECTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")
# Step fields that belong in a project file; the last_rand_* keys are runtime-only.


def _migrate_legacy_projects() -> None:
    """One-time copy of legacy app-dir .viseq projects into PROJECTS_DIR (e21s01).

    Copy-only and skip-existing: user documents are never overwritten or
    deleted; non-.viseq files and a missing legacy dir are ignored.
    """
    if os.path.abspath(LEGACY_PROJECTS_DIR) == os.path.abspath(PROJECTS_DIR):
        return
    try:
        names = sorted(os.listdir(LEGACY_PROJECTS_DIR))
    except OSError:
        return
    for name in names:
        if not name.lower().endswith(PROJECT_FILE_EXTENSION):
            continue
        target = os.path.join(PROJECTS_DIR, name)
        if os.path.exists(target):
            continue
        try:
            shutil.copy2(os.path.join(LEGACY_PROJECTS_DIR, name), target)
        except OSError as e:
            log_error("Projects", f"migrate {name}: {e}")


def ensure_user_dirs() -> None:
    """Create the XDG user dirs at boot and migrate legacy projects (e21s01)."""
    os.makedirs(user_config_dir(), exist_ok=True)
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    _migrate_legacy_projects()


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
        "spectrum": state.is_audio_analyzing,  # e28s03: analysis checkboxes
        "bpm": state.is_beat_tracking,
    }


def _capture_mapper_state() -> dict[str, Any]:
    """Project the live mapper into its persisted section (e28s01)."""
    return {
        "mappings": [mapper.capture_mapping(m) for m in state.mapper_mappings],
    }


def _sanitize_mapper_mappings(mappings: Any) -> list[dict[str, Any]]:
    """Heal the mapper mappings list: bounded, invalid rows dropped (e28s01)."""
    if not isinstance(mappings, list):
        return []
    clean: list[dict[str, Any]] = []
    for raw in mappings[:MAPPER_MAX_MAPPINGS]:
        healed = mapper.sanitize_mapping(raw)
        if healed is not None:
            clean.append(healed)
    return clean


def capture_project_state() -> dict[str, Any]:
    """Snapshot layout + theme + sequencer + mapper into a project dict (e11s01/e28s01)."""
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
        "mapper": _capture_mapper_state(),
        # 2026-09-07: the project also carries the MIDI rows that drive ITS
        # Mapper mappings, so opening the project restores its MIDI routing.
        "midi": {"mapper_bindings": project_mapper_bindings()},
    }


# --- e37: current-project identity + unsaved-changes marker (title bar) ---
# The app tracks which .viseq document the live session belongs to and whether
# the next Save would write something different. Window layout is deliberately
# excluded from the content fingerprint: moving/resizing windows is workspace
# preference, not document content (user agreement 2026-09-07). The mapping
# 'value' fields are excluded too — band/MIDI/Leap drives rewrite them
# constantly during playback, so like the last_rand_* step keys they are
# runtime state, never a dirt source (they still ride along on every save).
UNSAVED_PROJECT_LABEL = "Untitled"
PROJECT_DIRTY_POLL_SECONDS = 0.5


def _project_content_fingerprint() -> str:
    """Canonical JSON of the live project CONTENT (layout + live values out, e37s03)."""
    content = capture_project_state()
    content.pop("layout", None)
    for mapping in content.get("mapper", {}).get("mappings", []):
        mapping.pop("value", None)
    return json.dumps(content, sort_keys=True, separators=(",", ":"))


def current_project_label() -> str:
    """Title-bar name of the current project: file basename or Untitled (e37)."""
    path = state.current_project_path
    if path:
        return os.path.basename(path)
    return UNSAVED_PROJECT_LABEL


def refresh_window_title() -> None:
    """Rewrite the X11 viewport title with the project name + dirty marker (e37s03)."""
    marker = " *" if state.project_dirty else ""
    dpg.set_viewport_title(f"viSeq - {current_project_label()}{marker}")


def _adopt_content_as_saved() -> None:
    """Record the live content as the saved baseline (save/open/new/boot, e37s01).

    The baseline is derived from the LIVE state after the flow ran — never by
    re-reading the file — so it matches exactly what the next Save would write.
    Does not touch dpg (boot calls it before the viewport exists).
    """
    state.project_dirty = False
    state.saved_content_fingerprint = _project_content_fingerprint()


def _sync_project_dirty() -> bool:
    """Poll the live content vs the saved baseline; True when the flag flipped."""
    dirty = _project_content_fingerprint() != state.saved_content_fingerprint
    if dirty == state.project_dirty:
        return False
    state.project_dirty = dirty
    refresh_window_title()
    return True


_last_project_dirty_poll = 0.0


def tick_project_dirty(now: float) -> None:
    """Main-loop cadence (~0.5 s): flip the dirty marker when content changed (e37s03)."""
    global _last_project_dirty_poll
    if now - _last_project_dirty_poll < PROJECT_DIRTY_POLL_SECONDS:
        return
    _last_project_dirty_poll = now
    _sync_project_dirty()


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
    # e28s03: analysis checkboxes restore last — the device combo above is set
    # first because opening the input stream reads the selected device.
    if "spectrum" in audio or "bpm" in audio:
        set_audio_analysis_flags(bool(audio.get("spectrum", False)), bool(audio.get("bpm", False)))


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


def apply_project_state(doc: dict[str, Any]) -> None:
    """Re-apply a project dict onto the live app (mapper, layout, theme, sequencer) (e11s01/e28s01).

    e28s01: the mapper state is restored BEFORE the window layout applies, so
    the Mapper body exists by the time a saved layout may show the window.
    """
    mapper_section = doc.get("mapper")
    if isinstance(mapper_section, dict):
        mapper.restore_mappings(mapper_section.get("mappings"))
        refresh_mapper_ui()
    midi_section = doc.get("midi")
    if isinstance(midi_section, dict):  # 2026-09-07: restore the project's MIDI routing
        live_ids = {int(m["id"]) for m in state.mapper_mappings}
        apply_project_mapper_bindings(midi_section.get("mapper_bindings"), live_ids)
    apply_window_layout(doc.get("layout", {}).get("windows", []))
    theme = doc.get("theme")
    if isinstance(theme, dict):
        _apply_theme_config(theme)
    seq = doc.get("sequencer")
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
        "mapper": {"mappings": []},
    }


def apply_new_project() -> None:
    """Reset the live sequencer to pristine defaults and rebuild its UI (e15s01).

    Tracks are replaced wholesale (clearing pending fades and the runtime
    last_rand_* keys), then the project-apply path rebuilds every cell, slot,
    beat-source checkbox and audio widget. e28s01: the Mapper is project
    content, so New project clears its mappings too and refreshes the body.
    """
    for row in range(NUM_TRACKS):
        tracks_data[row] = _pristine_track()
    _apply_sequencer_state(pristine_project_state()["sequencer"])
    mapper.restore_mappings([])
    refresh_mapper_ui()


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
    if base.get("type") == "FlagX":
        # e36s07: flag is a VALUE step now; the legacy fire token always meant
        # "next", which the value form preserves as -1.
        base["type"] = "FlagV"
        base["v1"] = -1.0
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
    clean = {
        "device": str(audio.get("device", "")),
        "lowpass": bool(audio.get("lowpass", True)),
        "bands": bands,
    }
    # e28s03: analysis flags are only healed onto the document when the source
    # file carried them — a pre-e28 audio section leaves the live analysis alone.
    if "spectrum" in audio:
        clean["spectrum"] = bool(audio.get("spectrum"))
    if "bpm" in audio:
        clean["bpm"] = bool(audio.get("bpm"))
    return clean


def _sanitize_project_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a loaded project document into the capture shape (e11s01/e28s01)."""
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
    clean = {
        "layout": {"windows": layout["windows"]},
        "theme": theme,
        "sequencer": {
            "beat_source": beat if beat in BEAT_SOURCE_LABELS else BEAT_SOURCE_ANALYSIS,
            "manual_bpm": _to_float(seq.get("manual_bpm"), DEFAULT_MANUAL_BPM),
            "tracks": _sanitize_tracks(seq.get("tracks")),
            "audio": _sanitize_audio_state(seq.get("audio")),
        },
    }
    # e28s01: the mapper section is only present in the healed document when the
    # source file carried one — a pre-e28 project leaves the live mapper alone.
    raw_mapper = raw.get("mapper")
    if isinstance(raw_mapper, dict):
        clean["mapper"] = {"mappings": _sanitize_mapper_mappings(raw_mapper.get("mappings"))}
    # 2026-09-07: project-scoped Midi rows pass through (bounded; missing/older
    # projects simply carry none and leave the global routing untouched).
    raw_midi = raw.get("midi")
    if isinstance(raw_midi, dict) and isinstance(raw_midi.get("mapper_bindings"), list):
        rows = [r for r in raw_midi["mapper_bindings"] if isinstance(r, dict)]
        clean["midi"] = {"mapper_bindings": rows[: MAPPER_MAX_MAPPINGS * 8]}
    return clean


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
    """Capture + write a project, adopt it as current; False + logged on failure (e11s03/e37s01)."""
    path = _ensure_project_extension(path)
    if not save_project_to_file(path, capture_project_state()):
        return False
    state.current_project_path = path
    _adopt_content_as_saved()
    refresh_window_title()
    cfg = load_config()
    remember_recent_project(cfg, path)
    save_config(cfg)
    rebuild_last_project_menu()
    return True


def open_project_file(path: str) -> bool:
    """Load + apply a project, adopt it as current, remember it (e11s03/e37s01)."""
    doc = load_project_file(path)
    if doc is None:
        return False
    apply_project_state(doc)
    state.current_project_path = path
    _adopt_content_as_saved()
    refresh_window_title()
    cfg = load_config()
    cfg["theme"] = doc["theme"]
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
    """Show the Save-as file dialog, prefilled with the current project name (e11s03/e37s02)."""
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    default = (
        os.path.basename(state.current_project_path)
        if state.current_project_path
        else "project.viseq"
    )
    _recreate_project_dialog("save_project_dialog", on_save_project_picked, default)


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


def save_current_project(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """viSeq > Save: write the current project; the first save opens Save as (e37s02)."""
    if state.current_project_path:
        save_project_file(state.current_project_path)
    else:
        show_save_project_dialog()


def exit_app(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """viSeq > Exit menu voice: the dirty-gated close request (e19/e37s04)."""
    request_exit()


# e19/e37s04: exit confirmation — the modal is shared by viSeq > Exit and the
# OS main-window X (set_exit_callback + disable_close), but it opens ONLY when
# the project is dirty; a clean session quits immediately. ``_exiting_app``
# guards the shutdown-time re-invocation of the exit callback (destroy_context
# queues it again while tearing down, when no modal may be created).
EXIT_CONFIRM_TAG = "exit_confirm_modal"


_exiting_app = False


def request_exit(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Close request (menu Exit or the OS window X): ask when dirty, else quit (e37s04)."""
    if _exiting_app:
        return  # already confirmed — destroy_context re-invokes the exit callback
    _sync_project_dirty()  # fresh dirt: the ~0.5 s cadence may lag a just-made edit
    if state.project_dirty:
        show_exit_confirm()
    else:
        confirm_exit()


def show_exit_confirm(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the Exit-confirmation modal (called only when the project is dirty, e37s04)."""
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
    """Modal New project: close the confirmation and reset the sequencer (e15s01).

    e37s01: a new project is unnamed — the reset also clears the current-path
    identity and records the pristine content as the saved baseline.
    """
    if dpg.does_item_exist(NEW_PROJECT_CONFIRM_TAG):
        dpg.delete_item(NEW_PROJECT_CONFIRM_TAG)
    apply_new_project()
    state.current_project_path = None
    _adopt_content_as_saved()
    refresh_window_title()


def request_new_project(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """viSeq > New project: reset immediately when clean; ask when dirty (e37s04)."""
    _sync_project_dirty()  # fresh dirt: the ~0.5 s cadence may lag a just-made edit
    if state.project_dirty:
        show_new_project_confirm()
    else:
        confirm_new_project()


def apply_boot_config() -> None:
    """Boot: apply the fallback theme, then restore the last project when flagged (e11s04)."""
    cfg = load_config()
    midi_init_from_config(cfg)  # e09: MIDI control mirrors (enabled, port, bindings)
    leap_init_from_config(cfg)  # e26: Leap Motion engine mirror (enabled flag)
    if dpg.does_item_exist("midi_enable_cb"):
        # The MIDI window is built before the config loads, so the Enable checkbox
        # starts unchecked even when the engine is on — sync it (BUG-2026-08-29T102156).
        dpg.set_value("midi_enable_cb", state.midi_enabled)
    if dpg.does_item_exist("leap_enable_cb"):
        # e26: same boot-sync for the Leap Motion Enable checkbox (built before config).
        dpg.set_value("leap_enable_cb", state.leap_enabled)
    if dpg.does_item_exist("leap_viz_cb"):
        # e26s04: boot-sync the visualizer toggle (built before config) and fold
        # the panel when the persisted flag asks for it (engine must be on).
        dpg.set_value("leap_viz_cb", state.leap_visualizer)
        dpg.configure_item("leap_viz_cb", enabled=state.leap_enabled)
        if state.leap_enabled and state.leap_visualizer:
            _apply_leap_viz_layout(True)
    _apply_theme_config(cfg["theme"])
    if dpg.does_item_exist("cb_restore_project_boot"):
        dpg.set_value("cb_restore_project_boot", cfg["projects"]["restore_last_on_boot"])
    # e28s04: the Settings OSC fields prefill from the config (the Settings window
    # is built before the config loads, so the persisted endpoints land here).
    endpoints = _osc_endpoints_from_config(cfg)
    for tag, value in (
        ("viosc_ip", endpoints["client_ip"]),
        ("viosc_port", endpoints["client_port"]),
        ("listen_ip", endpoints["listen_ip"]),
        ("listen_port", endpoints["listen_port"]),
    ):
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, value)
    if should_restore_last_project_on_boot(cfg):
        recent = recent_project_paths(cfg)
        if recent:
            project_state = load_project_file(recent[0])
            if project_state is not None:
                apply_project_state(project_state)
                state.current_project_path = recent[0]  # e37s01: boot-restored identity
    _adopt_content_as_saved()  # e37: the boot session (restored or pristine) is the clean baseline


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
    if state.preview_active is not None and state.preview_playing:
        return FRAME_SLEEP_ANIMATED  # e38: a playing preview keeps full rate
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


def assign_target_to_track(row: int, target_id: str | None) -> None:
    """Point a sequencer row at a source; None is a no-op (e30s01).

    The one assign choke point shared by the row clip-slot click / MIDI learn
    (via midi_action_track_assign, e10s06 selection-first) and the tile
    context-menu "Add to Step Sequencer" line items (the right-clicked
    source, e30s01): it writes the row's target id + OSC base address and
    swaps the clip-slot thumbnail.
    """
    if target_id is None:
        return
    tracks_data[row]["target_id"] = target_id
    tracks_data[row]["base_address"] = f"/vimix/{target_id}"
    update_track_slot_ui(row)


def midi_action_track_assign(row: int) -> None:
    """Assign the currently selected media to track row (e10s06: viseq selection first)."""
    assign_target_to_track(row, get_current_target_id())


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
    parsed = parse_step_token(step_type)
    if parsed is not None and parsed[0] == "flag":
        # e36s07: a new Flag step defaults to -1 (next)
        tracks_data[row]["steps"][col]["v1"] = -1.0
    update_step_ui(row, col)


# e36s04: lazy 'More properties...' step picker (a modal built on demand, so
# the per-cell popups stay flat and the import-time menubar capture stays clean)
_STEP_PICKER_WIN = "step_picker_window"


def open_step_picker(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Right-click a step cell > More properties...: pick any (property x mode)
    step of the catalog in a scrollable modal."""
    row, col = int(user_data[0]), int(user_data[1])
    if dpg.does_item_exist(_STEP_PICKER_WIN):
        dpg.delete_item(_STEP_PICKER_WIN)
    with dpg.window(
        label="Step picker",
        tag=_STEP_PICKER_WIN,
        modal=True,
        width=320,
        height=420,
        no_resize=True,
    ):
        themed_text("More properties...", slot="text_dim")
        with dpg.child_window(height=330, border=True):
            for offering in more_step_offerings():
                for _mode, token, menu_label in offering["modes"]:
                    dpg.add_button(
                        label=f"{offering['label']} \u00b7 {menu_label}",
                        width=280,
                        callback=_step_picker_apply,
                        user_data=(row, col, token),
                    )
        with dpg.group(horizontal=True):
            dpg.add_button(label="Close", callback=_step_picker_close, width=280)
    dpg.show_item(_STEP_PICKER_WIN)


def _step_picker_apply(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Apply the picked step type and close the picker."""
    set_step_type(None, None, user_data)
    if dpg.does_item_exist(_STEP_PICKER_WIN):
        dpg.delete_item(_STEP_PICKER_WIN)


def _step_picker_close(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    if dpg.does_item_exist(_STEP_PICKER_WIN):
        dpg.delete_item(_STEP_PICKER_WIN)


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


def set_step_row_active(row: int, active: bool) -> None:
    """Enable/disable every step of one sequencer row (mouse + MIDI share this).

    e36s07: refreshes each cell's checkbox and theme in place; an out-of-range
    row is a no-op so a stale binding can never raise into the UI.
    """
    if row < 0 or row >= NUM_TRACKS:
        return
    for col in range(NUM_STEPS):
        tracks_data[row]["steps"][col]["active"] = bool(active)
        if dpg.does_item_exist(f"seq_cb_{row}_{col}"):
            dpg.set_value(f"seq_cb_{row}_{col}", bool(active))
        update_step_theme(row, col)


def enable_step_row(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Cell popup > Enable row: arm every step of the clicked row."""
    set_step_row_active(int(user_data), True)


def disable_step_row(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Cell popup > Disable row: disarm every step of the clicked row."""
    set_step_row_active(int(user_data), False)


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


def _clear_step_cell(row: int, col: int) -> None:
    """Delete a step cell's content so a rebuild cannot collide.

    DPG 2.3.1 quirk: ``delete_item(cell, children_only=True)`` does NOT release
    the aliases of items nested inside a ``dpg.popup`` — a second rebuild of
    the same cell then raises 'Alias already exists' (error 1000). Worse, the
    popup survives the deletion of its anchor group as an orphan, so it must be
    deleted EXPLICITLY by its own tag first; then every direct child item of
    the cell is removed.
    """
    cell_tag = f"seq_cell_{row}_{col}"
    popup_tag = f"seq_pop_{row}_{col}"
    if dpg.does_item_exist(popup_tag):
        dpg.delete_item(popup_tag)  # the orphaned popup keeps its aliases alive
    try:
        children = dpg.get_item_children(cell_tag, 1) or []
    except Exception:  # defensive: fall back to the container delete
        dpg.delete_item(cell_tag, children_only=True)
        return
    for child in children:
        if child is not None:
            dpg.delete_item(child)


def update_step_ui(row: int, col: int) -> None:
    cell_tag = f"seq_cell_{row}_{col}"
    step_data = tracks_data[row]["steps"][col]

    if not dpg.does_item_exist(cell_tag):
        return

    _clear_step_cell(row, col)

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

        with dpg.popup(cb, mousebutton=dpg.mvMouseButton_Right, tag=f"seq_pop_{row}_{col}"):
            # 2026-09-06 (user): arm/cancel MIDI Learn from any right-click menu
            _add_context_learn_item(tag=f"ctx_learn_cell_{row}_{col}")
            dpg.add_separator()
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
            # e36s04: the extended (property x mode) steps open in a lazy modal
            # picker — built on demand so the import-time menubar capture stays
            # clean (no nested menus inside the per-cell popups)
            dpg.add_menu_item(
                label="More properties...",
                callback=open_step_picker,
                user_data=(row, col),
            )
            dpg.add_separator()
            dpg.add_menu_item(label="Copy Step", callback=copy_step, user_data=(row, col))
            dpg.add_menu_item(label="Paste Step", callback=paste_step, user_data=(row, col))
            dpg.add_menu_item(
                label="Paste to Row", callback=paste_step_to_row, user_data=(row, col)
            )
            # e36s07: whole-row arm/disarm, kept at the bottom of the menu
            dpg.add_separator()
            dpg.add_menu_item(label="Enable row", callback=enable_step_row, user_data=row)
            dpg.add_menu_item(label="Disable row", callback=disable_step_row, user_data=row)

    parsed_type = parse_step_token(step_data["type"])  # e36s04 (property, mode)
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

    elif parsed_type is not None and not step_modes_covered_by_legacy(
        parsed_type[0], parsed_type[1]
    ):
        # e36s04: generic editor for the new (property, mode) steps
        prop, mode = parsed_type
        entry = catalog.PROPERTY_CATALOG[prop]
        comp0 = entry["components"][0]
        lo, hi = float(comp0["min"]), float(comp0["max"])
        if mode == "value" and prop == "flag":
            # e36s07: Flag is a VALUE step carrying the target flag id
            # (-1 = next); ids are integers, so use an integer editor.
            dpg.add_spacer(parent=cell_tag, height=5)
            dpg.add_drag_int(
                parent=cell_tag,
                width=70,
                default_value=int(step_data["v1"]),
                min_value=int(lo),
                max_value=int(hi),
                speed=1,
                format="%d",
                tag=f"seq_flag_{row}_{col}",
                callback=update_step_val,
                user_data=(row, col, "v1"),
            )
        elif mode == "value" or (mode == "fire" and entry["family"] == catalog.FAMILY_TOGGLE):
            dpg.add_spacer(parent=cell_tag, height=5)
            dpg.add_drag_float(
                parent=cell_tag,
                width=70,
                default_value=step_data["v1"],
                min_value=lo if mode == "value" else 0.0,
                max_value=hi if mode == "value" else 1.0,
                speed=0.01,
                format="%.2f",
                callback=update_step_val,
                user_data=(row, col, "v1"),
            )
        elif mode == "random":
            dpg.add_spacer(parent=cell_tag, height=5)
            dpg.add_text(
                f"{step_data.get('last_rand_v1', 0.0):.2f}",
                color=(150, 255, 150, 255),
                tag=f"rand_v1_{row}_{col}",
                parent=cell_tag,
                indent=20,
            )
        elif mode == "fade":
            dpg.add_spacer(parent=cell_tag, height=2)
            with dpg.group(horizontal=True, parent=cell_tag):
                dpg.add_drag_float(
                    width=34,
                    default_value=step_data["v1"],
                    min_value=lo,
                    max_value=hi,
                    speed=0.01,
                    format="%.1f",
                    callback=update_step_val,
                    user_data=(row, col, "v1"),
                )
                dpg.add_drag_float(
                    width=34,
                    default_value=step_data["v2"],
                    min_value=lo,
                    max_value=hi,
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
        elif mode == "cycle":
            dpg.add_spacer(parent=cell_tag, height=5)
            options = entry.get("options") or []
            raw = step_data.get("last_idx")
            text = "—"
            if raw is not None and 0 <= int(raw) < len(options):
                text = str(options[int(raw)])
            elif raw is not None:
                text = str(int(raw))
            dpg.add_text(
                text,
                color=(150, 200, 255, 255),
                tag=f"rand_v1_{row}_{col}",
                parent=cell_tag,
                indent=20,
            )
        else:  # mode == "fire" and trigger family: no value to edit
            dpg.add_spacer(parent=cell_tag, height=5)
            dpg.add_text(
                "fire",
                color=(200, 160, 120, 255),
                tag=f"rand_v1_{row}_{col}",
                parent=cell_tag,
                indent=20,
            )

    update_step_theme(row, col, is_head=(state.is_playing and state.current_step == col))


def on_tile_add_to_sequencer(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Tile context menu > Add to Step Sequencer > line N (e30s01).

    Assigns the RIGHT-CLICKED tile source to the chosen row through the shared
    assign core — unlike the clip-slot click, no grid selection is needed: the
    menu carries the source explicitly (user_data = (target_id, row)).
    """
    target_id, row = user_data
    assign_target_to_track(int(row), target_id)


def on_tile_add_to_mapper_line(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Add to Mapper > line N: re-point that mapper row onto the clicked source (e31s01).

    The row's CURRENT source is resolved at click time (mapper.row_targets(),
    first-appearance window order) — the popup can outlive mapper changes — and
    re-pointed onto the right-clicked source with the e29 retarget core; a stale
    index or a row already carrying the clicked source is a no-op (no refresh).
    """
    target_id, line_index = user_data
    rows = mapper.row_targets()
    if line_index >= len(rows):
        return
    if mapper.retarget_source(rows[line_index], target_id):
        refresh_mapper_ui()


def _add_tile_context_items(target_id: str) -> None:
    """The three right-click actions of a Mediagrid tile (e16, e30s01, e31s01).

    Must run inside a ``with dpg.window(popup=True, ...)`` block so the items
    are parented to that popup window. Every tile calls this — the actions
    must never drift apart. Menu: Regenerate Thumbnails, a separator, Add to
    Step Sequencer (a hover submenu with one 'line N' item per sequencer
    row), and Add to Mapper — a submenu whose FIRST item is 'new' (the
    classic mapping dialog) followed by one 'line N' item per mapper row that
    exists right now (window order; re-points the row onto this source).
    e33s04 (user rule): the popup carries NO learn markers — a binding must
    never anchor to a volatile source. The selection-relative actions live on
    the stable surfaces instead: mapper line markers on the Mapper rows and
    the regen marker in the sources-window learn bar. 2026-09-06 (user): the
    popup DOES carry one trailing MODE item (_add_context_learn_item) that
    arms/cancels MIDI Learn — a mode toggle is not a binding target, so the
    no-marker rule above stands.
    e38s03: the popup leads with "Preview..." for media sources that are not
    known images — a tile-anchored action like the others (no learn marker).
    """
    # e38s03: content preview of the source's file (video-only per user
    # decision; unknown-kind media classify on activation)
    if _preview_offered(_source_props(target_id)):
        dpg.add_menu_item(label="Preview...", callback=start_source_preview, user_data=target_id)
        dpg.add_separator()
    dpg.add_menu_item(
        label="Regenerate Thumbnails",
        callback=regen_thumb_callback,
        user_data=target_id,
    )
    dpg.add_separator()
    with dpg.menu(label="Add to Step Sequencer"):
        for row in range(NUM_TRACKS):
            dpg.add_menu_item(
                label=f"line {row + 1}",  # 1-based human row label (e30s01)
                callback=on_tile_add_to_sequencer,
                user_data=(target_id, row),
            )
    with dpg.menu(label="Add to Mapper"):
        dpg.add_menu_item(
            label="new",
            callback=open_new_mapping_dialog,
            user_data=target_id,
        )
        for line_index in range(len(mapper.row_targets())):
            dpg.add_menu_item(
                label=f"line {line_index + 1}",  # 1-based window row label (e31s01)
                callback=on_tile_add_to_mapper_line,
                user_data=(target_id, line_index),
            )
    # e33s04: after the three actions — a separator, then per-source color correction
    dpg.add_separator()
    dpg.add_menu_item(
        label="Enable color correction",
        callback=on_tile_enable_color_correction,
        user_data=target_id,
    )
    # 2026-09-06 (user): arm/cancel MIDI Learn from the popup. A MODE item, not
    # a binding target — the e33s04 no-marker rule above stands unchanged.
    dpg.add_separator()
    _add_context_learn_item()


def on_tile_enable_color_correction(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Tile context menu > Enable color correction: turn ON the source's CC block.

    The address /vimix/<target>/correction is the frozen-contract path the
    Mapper catalog already uses ('correction' 0..1, from the vimix wiki); the
    menu item arms it (1.0). The mouse path is tile-anchored; the MIDI twin is
    selection-relative (enable_correction) per the volatile-source rule.
    """
    target_id = user_data
    addr = f"/vimix/{target_id}/correction"
    osc_client.send_message(addr, 1.0)
    append_log("OUT", f"{addr} [1.00]")


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
    """Right-click on a Mediagrid tile: show the tile's action popup (e16).

    e32s01 (BUG-2026-09-04T180936): the popup is REBUILT at open time — its
    Add to Mapper submenu must list the mapper rows that exist right now, and
    mapper changes (add/delete/retarget) never rebuild the grid that used to
    own the popup. e27s02 (BUG-2026-09-03T175000): the rebuilt popup must be
    positioned at the cursor first — an unpositioned DPG popup window opens
    at the top-left of the viewport instead of under the tile. The
    client-space mouse (get_mouse_pos(local=False)) is the coordinate space
    set_item_pos uses.
    """
    target_id = user_data
    _create_tile_popup(target_id)  # fresh items: current mapper rows (e32s01)
    popup_tag = _tile_popup_tag(target_id)
    dpg.set_item_pos(popup_tag, dpg.get_mouse_pos(local=False))
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

    # BUG-2026-09-03T212905: a Mapper row rendered before its source thumb
    # existed shows 'no thumb'; once the FIRST frame lands, rebuild the body
    # (like the sequencer slot below) so the image appears. Later frames only
    # append — the frame cycle animates them in place.
    if (
        is_first
        and dpg.does_item_exist(f"mapper_row_thumb_{target_id}")
        and not dpg.does_item_exist(f"mapper_row_img_{target_id}")
    ):
        refresh_mapper_ui()

    if is_first and not dpg.does_item_exist(img_tag) and dpg.does_item_exist(container_tag):
        if dpg.does_item_exist(loading_tag):
            dpg.delete_item(loading_tag)
        draw_tag = f"thumb_draw_{target_id}"
        badge_tag = f"tile_badge_bg_{target_id}"
        if dpg.does_item_exist(draw_tag):
            # the image goes BEHIND the index-badge overlay (before= insertion);
            # draw commands can't host handler registries, so the drawlist stays
            # the thumbnail's clickable surface (bound at tile build, e16).
            if dpg.does_item_exist(badge_tag):
                dpg.draw_image(
                    texture_tag=tex_tag,
                    pmin=(0, 0),
                    pmax=(115, 65),
                    parent=draw_tag,
                    before=badge_tag,
                    tag=img_tag,
                )
            else:
                dpg.draw_image(
                    texture_tag=tex_tag,
                    pmin=(0, 0),
                    pmax=(115, 65),
                    parent=draw_tag,
                    tag=img_tag,
                )
            click_reg_tag = media_tile_click_registry_tag(target_id)
            if dpg.does_item_exist(click_reg_tag):
                dpg.bind_item_handler_registry(draw_tag, click_reg_tag)

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

    The Mediagrid, the sequencer, every monitor player window and the Mapper
    each show per-source thumbnails; cycling runs while at least one of them
    is open so the animation follows the media wherever it is applied
    (e25s01: an open Mapper alone keeps the cycle running).
    """
    for tag in ("vimix_media_window", "sequencer_window", "mapper_window"):
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
    # e25s01: the Mapper row thumbnail cycles on the same cadence
    mapper_img_tag = f"mapper_row_img_{target_id}"
    if dpg.does_item_exist(mapper_img_tag):
        dpg.configure_item(mapper_img_tag, texture_tag=tex_tag)
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
    counter crosses the threshold the existing "Loading..." draw-text label is
    re-worded and re-colored without waiting for a grid rebuild
    (BUG-2026-08-27T201742: the rebuild-only rendering kept the tile on
    "Loading..." forever).
    """
    loading_tag = f"loading_txt_{target_id}"
    if not dpg.does_item_exist(loading_tag):
        return  # no pending label (thumbnail already shown, or grid not built)
    dpg.configure_item(
        loading_tag,
        text=THUMB_FAIL_LABEL,
        color=palette_rgba(state.active_palette["warning"]),
    )


def _char_width_px() -> int:
    """Width of a single character in the live default font (e10s06, e20s01).

    The default font is monospace (ProggyClean, 7 px); measuring 'M' (the
    widest glyph) keeps the budget conservative if a proportional font is ever
    loaded. Falls back to a constant until the font atlas is built
    (get_text_size -> None). Used by the mapper caption alignment; the tile
    title uses its own fixed ProggyTiny budget (MEDIA_TITLE_CHARS_PER_LINE).
    """
    try:
        size = dpg.get_text_size("M")
        if size and size[0]:
            return max(1, int(size[0]))
    except Exception:
        pass
    return MEDIA_TITLE_CHAR_PX


def _title_line_count(text: str) -> int:
    """Number of wrapped lines a tile title occupies (1 or 2; truncation caps it at 2)."""
    if not text:
        return 1
    return min(
        MEDIA_TITLE_MAX_LINES,
        max(1, -(-len(text) // MEDIA_TITLE_CHARS_PER_LINE)),
    )


def _title_reserve_padding(text: str) -> int:
    """Spacer height that makes every tile title occupy exactly two lines.

    1-line titles get one extra step so the thumbnail row sits at the same y in
    every tile — uniform tiles regardless of the name length. MEDIA_TITLE_RESERVE_PX
    is the NET layout step of a wrapped line (measured on DPG 2.3.1: a ProggyTiny-9
    line adds 9 px of glyphs but the trailing item spacing shrinks by 3 px).
    """
    return (MEDIA_TITLE_MAX_LINES - _title_line_count(text)) * MEDIA_TITLE_RESERVE_PX


def truncate_media_title(name: str) -> str:
    """Fit a media name into at most two Mediagrid title lines (e10s06).

    The budget is PER WRAPPED LINE (MEDIA_TITLE_CHARS_PER_LINE), not a
    total-width budget: the wrap breaks every MEDIA_TITLE_WRAP px, so a
    longer string still needs more lines. The longest prefix that keeps
    prefix+ellipsis within two lines is returned; the full name stays in the
    raw table and in target_id — only the display is truncated.
    """
    if not name:
        return name
    text = str(name)
    max_chars = MEDIA_TITLE_CHARS_PER_LINE * MEDIA_TITLE_MAX_LINES
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
    select_media_source(user_data)


def on_tile_alpha_slider(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Drag a tile's thin vertical alpha slider: set the source alpha (0..1).

    The address /vimix/<target>/alpha is the same frozen-contract path the
    sequencer (AlphaV/R/F) and the mapper use — no new OSC surface. The next
    viOSC state push renders the new value back into the slider.
    """
    target_id = user_data
    alpha = float(app_data)
    addr = f"/vimix/{target_id}/alpha"
    osc_client.send_message(addr, alpha)
    append_log("OUT", f"{addr} [{alpha:.2f}]")


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

        # e36s06: seed anchored vector caches (color/corner) from the live feed
        # so component mappings start from real values instead of neutral ones.
        mapper.seed_anchors_from_live_state(sources)

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
        mapper.prune_anchors(live_ids)  # e36s06: the anchor cache follows source churn
        removed_mappings = mapper.prune_mappings(live_ids)
        if removed_mappings:
            for removed in removed_mappings:
                _drop_mapping_editor_and_runs(int(removed["id"]))  # e35s05
            refresh_mapper_ui()  # a removed source takes its mappings with it (e16)
        if state.viseq_selected_source is not None and state.viseq_selected_source not in live_ids:
            state.viseq_selected_source = None  # a pruned source can't stay selected (e10s06)
        if state.preview_active is not None and state.preview_active not in live_ids:
            close_source_preview()  # a pruned source can't keep a preview stream (e38s03)

        current_source = state.global_vimix_state["current_source"]
        data_dict = state.global_vimix_state["sources"]

        # grid display order = numeric source 'index' (fallback: dict key); the
        # shared key feeds both the grid build and the source-browsing cycle
        sorted_keys = sorted(data_dict.keys(), key=lambda k: _source_numeric_sort_key(data_dict, k))
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
                    if _tile_title_font is not None:
                        dpg.bind_item_font(title_tag, _tile_title_font)

                    # Every tile reserves exactly two title lines: the computed display
                    # name drives the extra spacer, so 1-line and 2-line titles leave the
                    # thumbnail row at the same y (uniform tiles), then a small gap.
                    display_name = str(name) if name else f"Idx: {idx}"
                    dpg.add_spacer(
                        parent=cw,
                        height=MEDIA_TITLE_GAP
                        + _title_reserve_padding(truncate_media_title(display_name)),
                    )
                    container_tag = f"thumb_container_{target_id}"
                    if dpg.does_item_exist(container_tag):
                        dpg.delete_item(container_tag)

                    # Compact media row: the thumbnail (with the index badge overlaid
                    # on its top-left corner) plus a thin vertical alpha slider, same
                    # height as the thumbnail — the badge+alpha line under the photo is
                    # gone, so the tile is shorter and the grid denser. horizontal_spacing=0
                    # keeps the slider flush against the image's right edge.
                    with dpg.group(horizontal=True, parent=cw, horizontal_spacing=0):
                        g_id = dpg.add_group(tag=container_tag)
                        draw_tag = f"thumb_draw_{target_id}"
                        img_tag = f"img_{target_id}"
                        loading_tag = None  # set only in the no-thumbs branch below
                        with dpg.drawlist(width=115, height=65, parent=g_id, tag=draw_tag):
                            if target_id in thumbnails_data:
                                tex_tag = thumbnails_data[target_id][0]
                                if dpg.does_item_exist(img_tag):
                                    dpg.delete_item(img_tag)
                                dpg.draw_image(
                                    texture_tag=tex_tag,
                                    pmin=(0, 0),
                                    pmax=(115, 65),
                                    parent=draw_tag,
                                    tag=img_tag,
                                )
                            # index badge overlay: top-left corner of the thumbnail (drawn
                            # after the image so it always sits on top)
                            index_tag = f"tile_index_{target_id}"
                            themed_draw_rectangle(
                                (2, 2),
                                (2 + MEDIA_BADGE_W, 2 + MEDIA_BADGE_H),
                                slot="badge_bg",
                                color=(0, 0, 0, 0),
                                rounding=2,
                                tag=f"tile_badge_bg_{target_id}",
                            )
                            idx_val = data_dict[idx].get("index")
                            idx_str = str(idx_val) if idx_val is not None else str(idx)
                            dpg.draw_text(
                                (2 + 10, 2 + 3),
                                idx_str,
                                color=palette_rgba(state.active_palette["text_bright"]),
                                size=13,
                                tag=index_tag,
                            )
                            _text_color_bindings[index_tag] = "text_bright"
                            if target_id not in thumbnails_data:
                                loading_tag = f"loading_txt_{target_id}"
                                is_failed = (
                                    thumb_fail_count.get(target_id, 0) >= THUMB_FAIL_THRESHOLD
                                )
                                dpg.draw_text(
                                    (8, 26),
                                    THUMB_FAIL_LABEL if is_failed else " [ Loading... ]",
                                    color=palette_rgba(
                                        state.active_palette["warning"]
                                        if is_failed
                                        else state.active_palette["text_dim"]
                                    ),
                                    size=13,
                                    tag=loading_tag,
                                )
                                _text_color_bindings[loading_tag] = "text_dim"

                        # thin vertical alpha slider, same height as the thumbnail
                        alpha_tag = f"tile_alpha_{target_id}"
                        dpg.add_slider_float(
                            min_value=0.0,
                            max_value=1.0,
                            default_value=0.0,
                            vertical=True,
                            width=MEDIA_ALPHA_SLIDER_W,
                            height=65,
                            clamped=True,
                            format="",  # no numeric readout: the slider position IS the value
                            callback=on_tile_alpha_slider,
                            user_data=target_id,
                            tag=alpha_tag,
                        )
                        dpg.bind_item_theme(alpha_tag, theme_alpha_slider)

                    # e10s06: the tile's clickable children select the media on left
                    # click (child windows can't host clicked handlers in DPG 2.x). Draw
                    # commands can't host handler registries either, so the drawlist
                    # carries the thumbnail clicks; the alpha slider stays out — dragging
                    # it only sets alpha, it never steals the selection.
                    _bind_tile_click_targets(
                        click_reg_tag,
                        title_tag,
                        container_tag,
                        draw_tag,
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
            state.last_ui_signature = current_signature

        # Value per-source updates: every cell write goes through the per-cell cache, so a
        # push that changes nothing performs zero dpg calls (perf e07 P0; measured ~20
        # calls/source/push before).
        for idx in sorted_keys:
            props = data_dict[idx]
            name = props.get("name")
            target_id = str(name) if name else str(idx)
            alpha_val = props.get("alpha")
            if isinstance(alpha_val, (int, float)):
                alpha_display = float(max(0.0, min(1.0, float(alpha_val))))
            else:
                alpha_display = 0.0
            _set_media_cell(f"tile_alpha_{target_id}", alpha_display)

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
            # 2026-09-06 (user): arm/cancel MIDI Learn from any right-click menu
            _add_context_learn_item(tag=f"ctx_learn_mon_{player_id}")
            dpg.add_separator()
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


# e38: SOURCE VIDEO PREVIEW — dedicated, resizable "Preview" window
# ==============================================================================
# 2026-09-07 (user, after rig UAT): the preview moved OUT of the "Vimix sources"
# window into its own top-level "Preview" window the user can position freely;
# resizing the window reflows the video + transport inside it. The transport
# (viseqapp/preview.py) is dpg-free (HIGH-1); this block owns the window UI on
# the main thread: the "Preview..." tile action (media sources that are not
# known images), the per-frame raw-texture apply and the clean-stop paths
# (Close / X / source switch / source prune / worker error). The e33 rule is
# not triggered: the tile action is tile-anchored like Regenerate Thumbnails
# — not a MIDI binding target.

PREVIEW_WINDOW_TAG = "preview_window"
PREVIEW_RESIZE_REG_TAG = "preview_resize_reg"
PREVIEW_VIDEO_SLOT_TAG = "preview_video_slot"
PREVIEW_TEXTURE_TAG = "preview_tex"
PREVIEW_IMAGE_TAG = "preview_img"
PREVIEW_TIME_TAG = "preview_time"
PREVIEW_SEEK_TAG = "preview_seek"
PREVIEW_PLAYBTN_TAG = "preview_play_btn"
PREVIEW_SPEED_TAG = "preview_speed"
PREVIEW_SPEED_RESET_TAG = "preview_speed_reset"
PREVIEW_SPEED_LABEL_TAG = "preview_speed_label"
PREVIEW_CLOSE_TAG = "preview_close_btn"
PREVIEW_WAIT_TAG = "preview_wait_text"
PREVIEW_STATUS_TAG = "preview_status_text"
PREVIEW_MSG_TEXT_TAG = "preview_msg_text"
PREVIEW_TRANSPORT_TAG = "preview_transport"
PREVIEW_FLOW_ROW_TAGS = ("preview_flow_0", "preview_flow_1", "preview_flow_2")

# Transport layout (BUG-2026-09-12): the seek bar + controls never exceed the
# video width; the buttons flow onto new lines when the video gets narrow, and
# the window height hugs the content so no dead space is left below.
PREVIEW_H_PAD = 20  # px horizontal padding of the video + transport content
PREVIEW_H_SPACING = 8  # px horizontal spacing used by the button flow
# Heights measured on DearPyGui 2.3.1 with the default style (the Preview window
# binds no padding theme): window chrome (titlebar + padding) + the seek row +
# the status line + the two item gaps = 104 px for a one-row transport; every
# additional wrapped control row adds 23 px.
PREVIEW_CONTENT_CHROME_H = 104
PREVIEW_FLOW_ROW_STEP_H = 23
PREVIEW_SCREEN_MARGIN_H = 48  # keep the fitted window inside the viewport
PREVIEW_SEEK_MIN_W = 80
PREVIEW_BTN_PAUSE_W = 70
PREVIEW_TIME_RESERVE_W = 92  # px reserved for the timecode label
PREVIEW_SPEED_LABEL_W = 46  # px reserved for the "Speed" label
PREVIEW_SPEED_FIELD_W = 64
PREVIEW_BTN_RESET_W = 30
PREVIEW_BTN_CLOSE_W = 60

# The transport controls in reading order with their fixed widths; the flow
# packer uses this to decide where the lines break.
PREVIEW_TRANSPORT_CONTROLS: tuple[tuple[str, int], ...] = (
    (PREVIEW_PLAYBTN_TAG, PREVIEW_BTN_PAUSE_W),
    (PREVIEW_TIME_TAG, PREVIEW_TIME_RESERVE_W),
    (PREVIEW_SPEED_LABEL_TAG, PREVIEW_SPEED_LABEL_W),
    (PREVIEW_SPEED_TAG, PREVIEW_SPEED_FIELD_W),
    (PREVIEW_SPEED_RESET_TAG, PREVIEW_BTN_RESET_W),
    (PREVIEW_CLOSE_TAG, PREVIEW_BTN_CLOSE_W),
)

# Default window geometry when no remembered rect exists yet (a session rect
# is remembered while the app runs and reused for the next preview).
PREVIEW_WIN_W = 560
PREVIEW_WIN_H = 440
PREVIEW_WIN_X = 640
PREVIEW_WIN_Y = 120

_preview_player: Any = None  # composition-root-owned PreviewPlayer instance
_preview_tex_dims: tuple[int, int] | None = None
_preview_win_rect: tuple[int, int, int, int] | None = None
_preview_layout: tuple[int, int, int] | None = None  # last applied (disp_w, disp_h, content_w)
_preview_error_shown = False


def _preview_endpoints(cfg: dict[str, Any]) -> dict[str, Any]:
    """Effective preview endpoint: host follows the OSC client (the viOSC
    machine, machine A), port from cfg['preview'] (default PREVIEW_PORT)."""
    osc = _osc_endpoints_from_config(cfg)
    raw = cfg.get("preview")
    pcfg = raw if isinstance(raw, dict) else {}
    return {"host": osc["client_ip"], "port": int(pcfg.get("port") or PREVIEW_PORT)}


def _source_props(target_id: str) -> dict[str, Any] | None:
    for props in state.global_vimix_state.get("sources", {}).values():
        if str(props.get("name")) == str(target_id):
            return props
    return None


def _preview_offered(props: dict[str, Any] | None) -> bool:
    """Preview popup gating: offered for media sources (uri) that are not known
    images — i.e. video, or still-unclassified media (classified on activation)."""
    if not props or not props.get("uri"):
        return False
    return props.get("media_kind") != "image"


def _preview_reason(target_id: str, props: dict[str, Any] | None, meta_provider: Any) -> str:
    """Why a preview can start ('ok') or the explicit message. Images and
    non-media never start a transport — the reason is surfaced instead."""
    if not props or not props.get("uri"):
        return f"'{target_id}' is not a media source"
    kind = props.get("media_kind")
    if kind == "image":
        return f"'{target_id}' is an image source — preview is video-only"
    if kind is None:
        meta = meta_provider()
        if meta is None:
            return f"'{target_id}': preview unavailable (no video metadata)"
        if meta.get("kind") != "video":
            return f"'{target_id}' is not a video source"
    return "ok"


def _fmt_preview_time(secs: float) -> str:
    s = max(0, int(secs))
    return f"{s // 60}:{s % 60:02d}"


def _preview_status(message: str) -> None:
    """Surface a preview message (state + log + status line) — the explicit
    error surface: never a silent no-op, never a fake transport."""
    state.preview_error = message
    append_log("PREVIEW", message)
    if dpg.does_item_exist(PREVIEW_STATUS_TAG):
        dpg.set_value(PREVIEW_STATUS_TAG, message)


def _preview_remember_rect() -> None:
    """Keep the last window geometry for the next preview session."""
    global _preview_win_rect
    if not dpg.does_item_exist(PREVIEW_WINDOW_TAG):
        return
    w = int(dpg.get_item_width(PREVIEW_WINDOW_TAG) or 0)
    h = int(dpg.get_item_height(PREVIEW_WINDOW_TAG) or 0)
    if w < 200 or h < 120:
        return
    x, y = dpg.get_item_pos(PREVIEW_WINDOW_TAG)
    _preview_win_rect = (int(x), int(y), w, h)


def _pack_preview_controls(max_w: int) -> list[list[str]]:
    """Greedily pack the transport controls into rows no wider than ``max_w``.

    Pure (no dpg): a control that does not fit flows onto the next line, so the
    transport never exceeds the video width (BUG-2026-09-12).
    """
    rows: list[list[str]] = [[] for _ in PREVIEW_FLOW_ROW_TAGS]
    row = 0
    used = 0
    for tag, width in PREVIEW_TRANSPORT_CONTROLS:
        if rows[row] and used + PREVIEW_H_SPACING + width > max_w and row + 1 < len(rows):
            row += 1
            used = 0
        if rows[row]:
            used += PREVIEW_H_SPACING
        rows[row].append(tag)
        used += width
    return rows


def _layout_preview_transport(content_w: int) -> None:
    """Reflow the seek bar + transport controls to ``content_w`` (the video
    width): the slider spans the width, the buttons wrap onto new lines."""
    if dpg.does_item_exist(PREVIEW_SEEK_TAG):
        dpg.configure_item(PREVIEW_SEEK_TAG, width=max(PREVIEW_SEEK_MIN_W, content_w))
    rows = _pack_preview_controls(content_w)
    for row_tag, tags in zip(PREVIEW_FLOW_ROW_TAGS, rows, strict=True):
        for tag in tags:
            if dpg.does_item_exist(tag):
                dpg.move_item(tag, parent=row_tag)
        if dpg.does_item_exist(row_tag):
            dpg.configure_item(row_tag, show=bool(tags))


def _preview_available_h() -> int:
    """Usable window height inside the viewport; 0 when it cannot be read."""
    try:
        total = int(dpg.get_viewport_client_height())
    except (TypeError, ValueError, AttributeError):
        return 0
    return max(0, total - PREVIEW_SCREEN_MARGIN_H)


def _layout_preview_content() -> None:
    """Reflow the video + transport to the current window size.

    The video fills the window width and keeps its aspect; the transport never
    exceeds the video width (the seek bar spans it, the buttons wrap); the
    window height then HUGS the content, so no dead space is left below the
    transport. The applied geometry is cached so a per-frame call never reflows
    (and never disturbs a slider drag); the height is re-fit only when it moved.
    """
    global _preview_layout
    if not dpg.does_item_exist(PREVIEW_WINDOW_TAG):
        return
    w = max(260, int(dpg.get_item_width(PREVIEW_WINDOW_TAG) or 0) or PREVIEW_WIN_W)
    if _preview_tex_dims is None:
        content_w = max(200, w - PREVIEW_H_PAD)
        if (0, 0, content_w) != _preview_layout:
            _preview_layout = (0, 0, content_w)
            _layout_preview_transport(content_w)
        return
    tex_w, tex_h = _preview_tex_dims
    disp_w = max(200, w - PREVIEW_H_PAD)
    disp_h = int(disp_w * tex_h / tex_w)
    rows = 1
    window_h = 0
    avail = _preview_available_h()
    for _ in range(3):
        rows = sum(1 for r in _pack_preview_controls(disp_w) if r)
        window_h = disp_h + PREVIEW_CONTENT_CHROME_H + (rows - 1) * PREVIEW_FLOW_ROW_STEP_H
        if not avail or window_h <= avail:
            break
        # taller than the screen: shrink the video (letterbox on the sides)
        video_avail = max(
            120, avail - PREVIEW_CONTENT_CHROME_H - (rows - 1) * PREVIEW_FLOW_ROW_STEP_H
        )
        if video_avail >= disp_h:
            break
        disp_h = video_avail
        disp_w = max(200, int(disp_h * tex_w / tex_h))
    if (disp_w, disp_h, disp_w) != _preview_layout:
        _preview_layout = (disp_w, disp_h, disp_w)
        if dpg.does_item_exist(PREVIEW_IMAGE_TAG):
            dpg.configure_item(PREVIEW_IMAGE_TAG, width=disp_w, height=disp_h)
        _layout_preview_transport(disp_w)
        if dpg.does_item_exist(PREVIEW_STATUS_TAG):
            dpg.configure_item(PREVIEW_STATUS_TAG, wrap=max(200, disp_w))
    h_now = int(dpg.get_item_height(PREVIEW_WINDOW_TAG) or 0)
    if h_now and window_h and window_h != h_now:
        dpg.configure_item(PREVIEW_WINDOW_TAG, height=window_h)


def _on_preview_window_resize(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Window resized: reflow the inner video + transport (main thread)."""
    _preview_remember_rect()
    _layout_preview_content()


def _open_preview_window(target_id: str, message: str | None = None) -> None:
    """Create (fresh) the dedicated Preview window with its content.

    ``message`` given: an error window (no transport) so a refused preview is
    visible and closable, never a silent no-op.
    """
    global _preview_layout
    _preview_layout = None  # a fresh window must be laid out once
    if _preview_win_rect is not None:
        x, y, w, h = _preview_win_rect
    else:
        x, y, w, h = PREVIEW_WIN_X, PREVIEW_WIN_Y, PREVIEW_WIN_W, PREVIEW_WIN_H
    if message is not None:
        title = "Preview"
        with dpg.window(label=title, tag=PREVIEW_WINDOW_TAG, width=420, height=120, pos=(x, y)):
            themed_text(message, slot="warning", tag=PREVIEW_MSG_TEXT_TAG, wrap=380)
            dpg.add_button(label="Close", width=90, callback=close_source_preview)
    else:
        with dpg.window(
            # ASCII separator: window titles use the default font, which has no glyph for "—"
            label=f"Preview - {target_id}",
            tag=PREVIEW_WINDOW_TAG,
            width=w,
            height=h,
            pos=(x, y),
            no_scrollbar=True,  # the height hugs the content: nothing ever scrolls
        ):
            with dpg.group(tag=PREVIEW_VIDEO_SLOT_TAG):
                dpg.add_text("Connecting...", tag=PREVIEW_WAIT_TAG)
            with dpg.group(tag=PREVIEW_TRANSPORT_TAG):
                dpg.add_slider_float(
                    default_value=0.0,
                    min_value=0.0,
                    max_value=1.0,
                    width=max(PREVIEW_SEEK_MIN_W, w - PREVIEW_H_PAD),
                    tag=PREVIEW_SEEK_TAG,
                    callback=on_preview_seek,
                )
                # controls live in the first flow row; _layout_preview_content
                # moves them onto the next row when they no longer fit the video
                with dpg.group(horizontal=True, tag=PREVIEW_FLOW_ROW_TAGS[0]):
                    dpg.add_button(
                        label="Pause",
                        width=PREVIEW_BTN_PAUSE_W,
                        tag=PREVIEW_PLAYBTN_TAG,
                        callback=on_preview_play_button,
                    )
                    themed_text("0:00 / 0:00", slot="text", tag=PREVIEW_TIME_TAG)
                    themed_text("Speed", slot="text_dim", tag=PREVIEW_SPEED_LABEL_TAG)
                    dpg.add_drag_float(
                        default_value=PREVIEW_SPEED_DEFAULT,
                        min_value=PREVIEW_SPEED_MIN,
                        max_value=PREVIEW_SPEED_MAX,
                        speed=PREVIEW_SPEED_STEP,
                        format="%.2fx",
                        width=PREVIEW_SPEED_FIELD_W,
                        tag=PREVIEW_SPEED_TAG,
                        callback=on_preview_speed,
                    )
                    dpg.add_button(
                        label="1x",
                        width=PREVIEW_BTN_RESET_W,
                        tag=PREVIEW_SPEED_RESET_TAG,
                        callback=on_preview_speed_reset,
                    )
                    dpg.add_button(
                        label="Close",
                        width=PREVIEW_BTN_CLOSE_W,
                        tag=PREVIEW_CLOSE_TAG,
                        callback=close_source_preview,
                    )
                for row_tag in PREVIEW_FLOW_ROW_TAGS[1:]:
                    dpg.add_group(horizontal=True, tag=row_tag)
            themed_text("", slot="text_dim", tag=PREVIEW_STATUS_TAG)
        # first reflow to the intended geometry (the window may not report its
        # size yet); the first decoded frame refines it to the video width
        _layout_preview_content()
    # window resize handler: inner content follows the window size
    if dpg.does_item_exist(PREVIEW_RESIZE_REG_TAG):
        dpg.delete_item(PREVIEW_RESIZE_REG_TAG)
    with dpg.item_handler_registry(tag=PREVIEW_RESIZE_REG_TAG):
        dpg.add_item_resize_handler(callback=_on_preview_window_resize)
    dpg.bind_item_handler_registry(PREVIEW_WINDOW_TAG, PREVIEW_RESIZE_REG_TAG)


def _preview_apply_frame(rgba: Any) -> None:
    """Apply one decoded frame (RGBA float32, h x w x 4) to the raw texture. The
    texture is created once per (session, size); later frames set_value only."""
    global _preview_tex_dims
    height, width = rgba.shape[0], rgba.shape[1]
    dims = (width, height)
    tex_tag = PREVIEW_TEXTURE_TAG
    if _preview_tex_dims != dims or not dpg.does_item_exist(tex_tag):
        for stale in (PREVIEW_IMAGE_TAG, tex_tag):
            if dpg.does_item_exist(stale):
                dpg.delete_item(stale)
        dpg.add_raw_texture(
            width=width,
            height=height,
            default_value=rgba.reshape(-1),
            tag=tex_tag,
            parent="texture_registry",
        )
        dpg.add_image(
            tex_tag,
            tag=PREVIEW_IMAGE_TAG,
            parent=PREVIEW_VIDEO_SLOT_TAG,
            width=max(200, PREVIEW_WIN_W - 20),
            height=180,
        )
        if dpg.does_item_exist(PREVIEW_WAIT_TAG):
            dpg.delete_item(PREVIEW_WAIT_TAG)
        _preview_tex_dims = dims
    else:
        dpg.set_value(tex_tag, rgba.reshape(-1))
    _layout_preview_content()


def close_source_preview(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Clean stop: remember the geometry, close the worker, drop the window."""
    global _preview_player, _preview_tex_dims, _preview_layout
    _preview_remember_rect()
    player = _preview_player
    _preview_player = None
    if player is not None:
        player.close()
    if dpg.does_item_exist(PREVIEW_RESIZE_REG_TAG):
        dpg.delete_item(PREVIEW_RESIZE_REG_TAG)
    if dpg.does_item_exist(PREVIEW_WINDOW_TAG):
        dpg.delete_item(PREVIEW_WINDOW_TAG)  # drops the texture/image children too
    _preview_tex_dims = None
    _preview_layout = None
    state.preview_active = None
    state.preview_playing = False
    state.preview_error = None
    while True:  # drop frames still queued for the closed session
        try:
            state.preview_frames.get_nowait()
        except queue.Empty:
            break


def start_source_preview(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the dedicated Preview window for a source (tile 'Preview...' action).
    One preview at a time; images/non-media show an explicit message window,
    never a transport; unknown-kind media classify via the meta endpoint first."""
    global _preview_player, _preview_error_shown
    target_id = str(user_data)
    props = _source_props(target_id)
    endpoints = _preview_endpoints(load_config())

    def classify() -> Any:
        return preview.fetch_media_meta(endpoints["host"], endpoints["port"], target_id)

    reason = _preview_reason(target_id, props, classify)
    if reason != "ok":
        close_source_preview()
        _preview_status(reason)
        _open_preview_window(target_id, message=reason)
        return
    close_source_preview()
    _preview_error_shown = False
    state.preview_error = None
    state.preview_active = target_id
    state.preview_playing = True
    _open_preview_window(target_id)
    player = preview.PreviewPlayer(target_id, host=endpoints["host"], port=endpoints["port"])
    _preview_player = player
    player.start()


def on_preview_play_button(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Play/Pause toggle of the preview transport (main thread)."""
    if _preview_player is None:
        return
    if state.preview_playing:
        _preview_player.pause()
        state.preview_playing = False
        if dpg.does_item_exist(PREVIEW_PLAYBTN_TAG):
            dpg.set_item_label(PREVIEW_PLAYBTN_TAG, "Play")
    else:
        _preview_player.resume()
        state.preview_playing = True
        if dpg.does_item_exist(PREVIEW_PLAYBTN_TAG):
            dpg.set_item_label(PREVIEW_PLAYBTN_TAG, "Pause")


def on_preview_speed(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Speed field changed (e38 BUG-2026-09-12): clamp, drive the transport,
    and reflect the clamped value back into the widget."""
    rate = preview.clamp_preview_speed(app_data)
    if _preview_player is not None:
        _preview_player.set_speed(rate)
    if dpg.does_item_exist(PREVIEW_SPEED_TAG):
        dpg.set_value(PREVIEW_SPEED_TAG, rate)


def on_preview_speed_reset(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """One-click reset of the playback rate to real time (the default)."""
    if _preview_player is not None:
        _preview_player.set_speed(PREVIEW_SPEED_DEFAULT)
    if dpg.does_item_exist(PREVIEW_SPEED_TAG):
        dpg.set_value(PREVIEW_SPEED_TAG, PREVIEW_SPEED_DEFAULT)


def on_preview_seek(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Scrub: seek the transport to a fraction of the playable duration."""
    if _preview_player is not None:
        _preview_player.seek(float(app_data))


def tick_source_preview(_now: float) -> None:
    """Main-loop tick: drain preview frames onto the texture, keep the transport
    in sync, surface worker errors once, stop when the window is X-closed
    (main thread only, HIGH-1)."""
    if state.preview_active is not None and not dpg.does_item_exist(PREVIEW_WINDOW_TAG):
        close_source_preview()  # the user X-closed the window: stop the stream
        return
    if state.preview_active is None:
        while True:
            try:
                state.preview_frames.get_nowait()
            except queue.Empty:
                break
        return
    global _preview_error_shown
    player = _preview_player
    if player is None:
        return
    newest = None
    while True:
        try:
            name, frame = state.preview_frames.get_nowait()
        except queue.Empty:
            break
        if str(name) == str(state.preview_active):
            newest = frame
    if newest is not None:
        _preview_apply_frame(newest)
    if player.done and player.error and not _preview_error_shown:
        _preview_error_shown = True
        _preview_status(player.error)
    if state.preview_playing and not dpg.is_item_active(PREVIEW_SEEK_TAG):
        duration = player.duration
        if duration > 0:
            fraction = max(0.0, min(1.0, player.position() / duration))
            if dpg.does_item_exist(PREVIEW_SEEK_TAG):
                dpg.set_value(PREVIEW_SEEK_TAG, fraction)
    if dpg.does_item_exist(PREVIEW_TIME_TAG):
        dpg.set_value(
            PREVIEW_TIME_TAG,
            f"{_fmt_preview_time(player.position())} / {_fmt_preview_time(player.duration)}",
        )


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


def _osc_endpoints_from_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """The effective OSC endpoints: cfg['osc'] merged over the constants (e28s04)."""
    raw = cfg.get("osc")
    osc_cfg = raw if isinstance(raw, dict) else {}
    return {
        "client_ip": str(osc_cfg.get("client_ip") or VIOSC_IP),
        "client_port": int(osc_cfg.get("client_port") or VIOSC_PORT),
        "listen_ip": str(osc_cfg.get("listen_ip") or VIOSC_IP),
        "listen_port": int(osc_cfg.get("listen_port") or VIOSC_LISTEN_PORT),
    }


def persist_osc_endpoints(
    client_ip: str | None = None,
    client_port: int | None = None,
    listen_ip: str | None = None,
    listen_port: int | None = None,
) -> None:
    """Persist OSC endpoint fields into cfg['osc'] (e28s04); None leaves a field."""
    cfg = load_config()
    osc = cfg["osc"]
    if client_ip is not None:
        osc["client_ip"] = client_ip
    if client_port is not None:
        osc["client_port"] = client_port
    if listen_ip is not None:
        osc["listen_ip"] = listen_ip
    if listen_port is not None:
        osc["listen_port"] = listen_port
    save_config(cfg)


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
        ip = str(dpg.get_value("listen_ip"))
        port = int(dpg.get_value("listen_port"))
        if start_osc_server(ip, port):
            persist_osc_endpoints(listen_ip=ip, listen_port=port)  # e28s04


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
    ip = str(dpg.get_value("viosc_ip"))
    port = int(dpg.get_value("viosc_port"))
    if connect_osc_client(ip, port):
        persist_osc_endpoints(client_ip=ip, client_port=port)  # e28s04


def autostart_osc() -> None:
    """Boot wiring: auto-connect the viOSC client and start the listening server
    on the persisted endpoints (e28s04); constants when the config has none."""
    endpoints = _osc_endpoints_from_config(load_config())
    connect_osc_client(endpoints["client_ip"], endpoints["client_port"])
    start_osc_server(endpoints["listen_ip"], endpoints["listen_port"])


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

# e33s02: momentary Mapper actions trigger at CC value >= MIDI_CC_TRIGGER_THRESHOLD
# (the shared threshold convention; lower values are a deliberate no-op).


def _log_stale_midi_target(action: str, detail: str) -> None:
    """Throttled diagnostic: a binding referenced an entity that no longer exists.

    A captured binding can outlive its target (the mapping was deleted, the
    mapper row vanished): the dispatch must be a logged no-op, never a crash.
    One line per second per action+detail pair, so different stale reasons
    under the same action still surface while a repeated press cannot flood.
    """
    now = time.time()
    key = f"{action}:{detail}"
    if now - _last_unknown_action_log.get(key, 0.0) < 1.0:
        return
    _last_unknown_action_log[key] = now
    append_log("MIDI", f"{action}: {detail}")


def _exec_mapper_enable(params: dict[str, Any], value: int) -> None:
    """e33s02: toggle a mapping's armed flag (note / CC >= 64); stale id no-op."""
    mid = int(params.get("mapping_id", -1))
    mapping = mapper.find_mapping(mid)
    if mapping is None:
        _log_stale_midi_target(MIDI_ACTION_MAPPER_ENABLE, f"no mapping {mid}")
        return
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    enabled = not bool(mapping["enabled"])
    mapper.set_mapping_enabled(mid, enabled)
    if dpg.does_item_exist(f"mapper_enable_{mid}"):
        dpg.set_value(f"mapper_enable_{mid}", enabled)  # the card checkbox follows in place


def _exec_mapper_reset(params: dict[str, Any], value: int) -> None:
    """e33s02: reset a mapping to its neutral default (same core as the R button)."""
    mid = int(params.get("mapping_id", -1))
    if mapper.find_mapping(mid) is None:
        _log_stale_midi_target(MIDI_ACTION_MAPPER_RESET, f"no mapping {mid}")
        return
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    reset_mapping(None, None, mid)  # neutral + widget re-sync, mouse-path identical


def _exec_mapper_line(params: dict[str, Any], value: int) -> None:
    """e33s03: assign the media selected in the Mediagrid to the mapper row (line).

    Variant B (user-confirmed): the LINE is bound, the SOURCE is read at
    trigger time — the row resolves via mapper.row_targets() and re-targets
    onto state.viseq_selected_source, mirroring the e29 row-thumb click rules:
    no selection, same-source row or a stale line index are no-ops (a stale
    line is logged).
    """
    line = int(params.get("line", -1))
    rows = mapper.row_targets()
    if line < 0 or line >= len(rows):
        _log_stale_midi_target(MIDI_ACTION_MAPPER_LINE, f"no line {line}")
        return
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    target = rows[line]
    selected = state.viseq_selected_source
    if selected is None or selected == target:
        return
    if mapper.retarget_source(target, selected):
        refresh_mapper_ui()


def _exec_mapper_band(params: dict[str, Any], value: int) -> None:
    """e33s03: bind a mapping to an audio band (same core as the card menu)."""
    mid = int(params.get("mapping_id", -1))
    if mapper.find_mapping(mid) is None:
        _log_stale_midi_target(MIDI_ACTION_MAPPER_BAND, f"no mapping {mid}")
        return
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    set_mapping_band(None, None, (mid, int(params.get("band", 0))))  # mouse-path identical


def _exec_mapper_cue_open(params: dict[str, Any], value: int) -> None:
    """e34s05: open a cue-list mapping's window (same core as the card button).

    The e33 rule: the new cue-list control ships MIDI-mappable — a momentary
    press (CC >= 64) opens the mapping's cue-list window; a stale mapping id is
    a logged no-op.
    """
    mid = int(params.get("mapping_id", -1))
    if mapper.find_mapping(mid) is None:
        _log_stale_midi_target(MIDI_ACTION_MAPPER_CUE_OPEN, f"no mapping {mid}")
        return
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    open_cue_list_window(None, None, mid)


def _source_numeric_sort_key(data: dict[str, Any], key: str) -> int:
    """Numeric sort key of a vimix source (its 'index' field, else the dict key).

    Shared by the Mediagrid rebuild and the source-browsing cycle so the two
    can never disagree on the grid order (e33s04). Malformed values sort last.
    """
    idx = data[key].get("index")
    if idx is not None:
        with contextlib.suppress(TypeError, ValueError):
            return int(idx)
    with contextlib.suppress(TypeError, ValueError):
        return int(key)
    return 0


def _source_target_ids_in_grid_order() -> list[str]:
    """Mediagrid source ids in the display order (numeric index/key, e33s04)."""
    data = state.global_vimix_state.get("sources") or {}
    ids = []
    for key in sorted(data, key=lambda k: _source_numeric_sort_key(data, k)):
        name = data[key].get("name")
        ids.append(str(name) if name else str(key))
    return ids


def select_media_source(target_id: str | None) -> None:
    """Make a source the viseq-side primary selection (same path as a tile click).

    e33s04: the shared core for on_media_tile_click and the source-browsing
    actions — set the selection and re-apply the tile themes in place.
    """
    if target_id is None or target_id == state.viseq_selected_source:
        return
    state.viseq_selected_source = target_id
    refresh_tile_selection_themes()


def _exec_source_next(params: dict[str, Any], value: int) -> None:
    """e33s04: move the Mediagrid selection to the next source (grid order, wrap)."""
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    _cycle_media_selection(+1)


def _exec_source_prev(params: dict[str, Any], value: int) -> None:
    """e33s04: move the Mediagrid selection to the previous source (grid order, wrap)."""
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    _cycle_media_selection(-1)


def _cycle_media_selection(direction: int) -> None:
    """Step the Mediagrid selection by one in the grid order (e33s04).

    Wraps around the ends; with no current selection, next picks the first and
    prev the last; zero or one source is a no-op. The CC >= threshold gate is
    applied by the momentary dispatch helpers.
    """
    ids = _source_target_ids_in_grid_order()
    if len(ids) < 2:
        return
    current = state.viseq_selected_source
    if current is None or current not in ids:
        select_media_source(ids[0] if direction > 0 else ids[-1])
        return
    index = ids.index(current)
    select_media_source(ids[(index + direction) % len(ids)])


def _exec_regen_selected(params: dict[str, Any], value: int) -> None:
    """e33s04: regenerate the thumbnails of the SELECTED source at trigger time.

    The binding never anchors to a source (the user swaps sources constantly):
    the action applies to state.viseq_selected_source when the button fires.
    """
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    selected = state.viseq_selected_source
    if selected is None:
        _log_stale_midi_target(MIDI_ACTION_REGEN_SELECTED, "no selection")
        return
    regen_thumb_callback(None, None, selected)


def _exec_seq_row_assign(params: dict[str, Any], value: int) -> None:
    """e33s04: assign the SELECTED source to a sequencer row (slot binding).

    The row is a stable slot (1..8), the source comes from the Mediagrid
    selection at trigger time — never anchored to a volatile source.
    """
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    row = int(params.get("row", -1))
    if row < 0 or row >= NUM_TRACKS:
        _log_stale_midi_target(MIDI_ACTION_SEQ_ROW_ASSIGN, f"no track {row}")
        return
    selected = state.viseq_selected_source
    if selected is None:
        _log_stale_midi_target(MIDI_ACTION_SEQ_ROW_ASSIGN, "no selection")
        return
    assign_target_to_track(row, selected)


def _exec_seq_row_active_true(params: dict[str, Any], value: int) -> None:
    """e36s07: MIDI enable of a whole sequencer row ({row} slot param)."""
    _exec_seq_row_active(params, value, True)


def _exec_seq_row_active_false(params: dict[str, Any], value: int) -> None:
    """e36s07: MIDI disable of a whole sequencer row ({row} slot param)."""
    _exec_seq_row_active(params, value, False)


def _exec_seq_row_active(params: dict[str, Any], value: int, active: bool) -> None:
    """Shared MIDI body: the row is a stable slot; low CC and stale rows no-op."""
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    row = int(params.get("row", -1))
    if row < 0 or row >= NUM_TRACKS:
        _log_stale_midi_target(
            MIDI_ACTION_SEQ_ROW_ENABLE if active else MIDI_ACTION_SEQ_ROW_DISABLE,
            f"no track {row}",
        )
        return
    set_step_row_active(row, active)


def _exec_enable_correction(params: dict[str, Any], value: int) -> None:
    """e33s04: arm the color-correction block of the SELECTED source (1.0).

    The mouse twin (tile menu) is tile-anchored; the MIDI action is

    selection-relative per the volatile-source rule.

    """
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    selected = state.viseq_selected_source
    if selected is None:
        _log_stale_midi_target(MIDI_ACTION_ENABLE_CORRECTION, "no selection")
        return
    addr = f"/vimix/{selected}/correction"
    osc_client.send_message(addr, 1.0)
    append_log("OUT", f"{addr} [1.00]")


# e33s01: one dispatcher per registered action (viseqapp/actions.py owns the
# metadata). Lambdas close over the module helpers, which resolve at call time.
_MIDI_EXECUTORS: dict[str, Callable[[dict[str, Any], int], None]] = {
    MIDI_ACTION_SEQ_TOGGLE: lambda p, v: midi_action_seq_toggle(
        int(p.get("row", 0)), int(p.get("col", 0))
    ),
    MIDI_ACTION_TRANSPORT_PLAY: lambda p, v: toggle_play(),
    MIDI_ACTION_TRANSPORT_RESYNC: lambda p, v: callback_resync(),
    MIDI_ACTION_TRANSPORT_TAP: lambda p, v: midi_action_transport_tap(),
    MIDI_ACTION_NUDGE_BACK: lambda p, v: callback_nudge_backward(),
    MIDI_ACTION_NUDGE_FORWARD: lambda p, v: callback_nudge_forward(),
    MIDI_ACTION_BEAT_SOURCE: lambda p, v: midi_action_beat_source(str(p.get("mode", ""))),
    MIDI_ACTION_TRACK_ASSIGN: lambda p, v: midi_action_track_assign(int(p.get("row", 0))),
    # e18: a learned MIDI control drives a Mapper mapping (raw 0..127 value)
    MIDI_ACTION_MAPPER_MAPPING: lambda p, v: midi_mapping_value(int(p.get("mapping_id", 0)), v),
    # e33s02: card caption learn markers (armed flag, reset)
    MIDI_ACTION_MAPPER_ENABLE: _exec_mapper_enable,
    MIDI_ACTION_MAPPER_RESET: _exec_mapper_reset,
    # e33s03: row line assign (variant B) + audio-band source
    MIDI_ACTION_MAPPER_LINE: _exec_mapper_line,
    MIDI_ACTION_MAPPER_BAND: _exec_mapper_band,
    MIDI_ACTION_MAPPER_CUE_OPEN: _exec_mapper_cue_open,
    # e33s04: source browsing (Mediagrid selection cycle)
    MIDI_ACTION_SOURCE_NEXT: _exec_source_next,
    MIDI_ACTION_SOURCE_PREV: _exec_source_prev,
    # e33s04: selection-relative actions — never anchored to a volatile source
    MIDI_ACTION_REGEN_SELECTED: _exec_regen_selected,
    MIDI_ACTION_SEQ_ROW_ASSIGN: _exec_seq_row_assign,
    MIDI_ACTION_SEQ_ROW_ENABLE: _exec_seq_row_active_true,
    MIDI_ACTION_SEQ_ROW_DISABLE: _exec_seq_row_active_false,
    MIDI_ACTION_ENABLE_CORRECTION: _exec_enable_correction,
    # e39s01: the MIDI Monitor window toggle (mappable per the e33 rule)
    MIDI_ACTION_MONITOR_TOGGLE: lambda p, v: _exec_monitor_toggle(p, v),
    # e40s01: arm/disarm a Route's Enabled gate
    MIDI_ACTION_ROUTE_TOGGLE: lambda p, v: _exec_route_toggle(p, v),
}

_last_unknown_action_log: dict[str, float] = {}  # action id -> last log time (throttle)


def _log_unknown_midi_action(action: str) -> None:
    """Throttled diagnostic: a binding referenced an action the registry does not know.

    A stale binding (its action removed from the catalog, or a config edited by
    hand) must be visible in the Logs window without flooding it — one line per
    second per action id, same pattern as _log_unmatched_midi.
    """
    now = time.time()
    if now - _last_unknown_action_log.get(action, 0.0) < 1.0:
        return
    _last_unknown_action_log[action] = now
    append_log("MIDI", f"unknown action {action}")


def midi_execute(action: str, params: dict[str, Any], value: int) -> None:
    """Execute a resolved MIDI action on the main thread (called via ui_task_queue, e09).

    e33s01: the action vocabulary lives in the registry (viseqapp/actions.py) and
    the dispatch in the _MIDI_EXECUTORS map — adding an action is one spec plus
    one entry here, never a new if/elif branch. An id the registry does not know
    (stale binding) is a logged no-op.
    """
    executor = _MIDI_EXECUTORS.get(action)
    if executor is None:
        _log_unknown_midi_action(action)
        return
    executor(params, value)


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


def _monitor_outcome_for(action: str, params: dict[str, Any], value: int) -> tuple[str, str]:
    """The MIDI Monitor's (outcome, detail) for one resolved binding (e39s01).

    A Mapper value binding reports the mapping-level result through the pure
    preview (SENT / HOLD / MUTED) WITHOUT sending OSC; every other action
    reports MATCH with its id and raw value.
    """
    if action == MIDI_ACTION_MAPPER_MAPPING:
        mapping_id = int(params.get("mapping_id", 0))
        mapping = mapper.find_mapping(mapping_id)
        if mapping is not None:
            effective, tag = mapper.preview_mapping_value(mapping, float(value))
            prop = str(mapping.get("property", "?"))
            return tag, f"mapping #{mapping_id} {prop} -> {effective:.2f}"
    return MIDI_MONITOR_OUTCOME_MATCH, f"{action} v={value}"


def _record_monitor_rx(
    port_name: str,
    msg_type: str | None,
    channel: int,
    number: int,
    value: int,
    outcome: str,
    detail: str,
) -> None:
    """Report one parsed message to the MIDI Monitor (e39s01) — observation only.

    Release edges (note_off / note_on velocity 0) parse to msg_type None and are
    not messages: they are skipped.
    """
    if msg_type is None:
        return
    midimonitor.record_rx(
        port_name, str(msg_type), int(channel), int(number), float(value), outcome, detail
    )


def handle_midi_message(msg: Any, port_name: str) -> None:
    """Route one incoming message (main thread): learn capture first, then dispatch.

    e39s01: every parsed message is also reported to the MIDI Monitor with its
    resolution outcome — observation only, the dispatch below is unchanged.
    """
    _log_first_midi_message(msg, port_name)
    msg_type, number, raw = _parse_midi_msg(msg)
    channel = int(getattr(msg, "channel", 0))
    if state.midi_learn_pending is not None:
        source = binding_source_from_message(msg, port_name)
        if source is not None:
            _record_monitor_rx(
                port_name, msg_type, channel, number, raw, MIDI_MONITOR_OUTCOME_LEARN, "captured"
            )
            ui_task(lambda: midi_learn_complete(source, port_name))
            return
    controller = find_controller_by_port(port_name)
    if controller is not None:
        bindings: list[dict[str, Any]] | None = list(controller.get("bindings") or []) + list(
            controller.get("auto_bindings") or []
        )
    else:
        bindings = None  # legacy flat lists (pre-e14 paths/tests)
    outcome = MIDI_MONITOR_OUTCOME_NOBIND if bindings is None else MIDI_MONITOR_OUTCOME_NOMATCH
    details: list[str] = []
    for action, params, value in resolve_midi_message(msg, port_name, bindings):
        _midi_enqueue_execute(action, params, value)
        outcome, detail = _monitor_outcome_for(action, params, value)
        details.append(detail)
    if not details and bindings is not None:
        _log_unmatched_midi(msg, port_name)
    _record_monitor_rx(port_name, msg_type, channel, number, raw, outcome, "; ".join(details))


def _exit_midi_learn() -> None:
    """Turn MIDI Learn off and restore the button/status (cancel, complete, disable)."""
    state.midi_learn_mode = False
    state.midi_learn_pending = None
    if state.midi_learn_armed_tag:  # an armed M returns to red before the surfaces drop it
        _style_learn_marker(state.midi_learn_armed_tag, False)
    state.midi_learn_armed_tag = None
    if dpg.does_item_exist("midi_learn_btn"):
        dpg.set_item_label("midi_learn_btn", "Learn mapping...")
    if dpg.does_item_exist("midi_learn_status"):
        dpg.set_value("midi_learn_status", "MIDI Learn off")
    _refresh_learn_surfaces()
    _sync_context_learn_labels()  # static menus (step cell, monitor) follow the mode


def _refresh_learn_surfaces() -> None:
    """Rebuild the surfaces whose learn markers are conditional on the mode (e33s02).

    Markers are rendered only while state.midi_learn_mode is on, so entering or
    leaving learn mode must rebuild the Mapper body (its refresh already guards
    on the body existing), create/drop the Mediagrid learn bar (e33s04) and the
    sequencer transport learn strip.
    """
    if dpg.does_item_exist("mapper_mappings_group"):
        refresh_mapper_ui()
    _sync_media_learn_bar()
    _sync_sequencer_learn_strip()
    _sync_seq_row_learn_strip()
    _sync_monitor_learn_marker()


def learn_marker(
    action_id: str,
    params: dict[str, Any],
    parent: Any,
    tag: str | None = None,
    tooltip: str | None = None,
) -> str:
    """Add ONE uniform learn marker button (e33s02): click captures the action.

    The marker is a small button labeled by a dot whose tooltip names the action
    (registry label); its click stores (action_id, params) as the pending learn
    capture instead of executing anything. Callers render markers ONLY while
    state.midi_learn_mode is on — the surfaces rebuild on learn transitions via
    _refresh_learn_surfaces, so markers never need show/hide juggling. The
    parent is EXPLICIT: a parentless add_button outside a with-block hangs the
    render thread in real DearPyGui (reproduced 2026-09-05). The tooltip may be
    overridden when the params distinguish slots (e.g. 'Mapper line 3').
    """
    marker_kwargs: dict[str, Any] = {
        "label": "M",
        "width": MAPPER_MARKER_W,
        "height": MAPPER_MARKER_H,
        "callback": on_learn_marker_click,
        "user_data": (action_id, params),
        "parent": parent,
    }
    if tag is not None:
        marker_kwargs["tag"] = tag  # tag=None would raise 'Must be int' in real DPG
    marker_tag = dpg.add_button(**marker_kwargs)
    dpg.bind_item_theme(marker_tag, theme_learn_marker)  # red 'M' (e33)
    tooltip_text = tooltip or f"Map: {actions.action_label(action_id)}"
    with dpg.tooltip(parent=marker_tag):
        dpg.add_text(tooltip_text)
    return marker_tag


def _style_learn_marker(tag: str | None, armed: bool) -> None:
    """Point one learn marker at the red (idle) or amber (armed) theme (2026-09-07)."""
    if not tag or not dpg.does_item_exist(tag):
        return
    dpg.bind_item_theme(tag, theme_learn_marker_armed if armed else theme_learn_marker)


def on_learn_marker_click(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """A learn marker was clicked: capture its (action_id, params) as the pending binding.

    The mouse behavior of the mapped control itself is never hijacked — markers
    are separate buttons; this handler only records what the next MIDI message
    will bind. Outside learn mode a stray click captures nothing.
    """
    if not state.midi_learn_mode:
        return
    action_id, params = user_data
    state.midi_learn_pending = (action_id, params)
    state.midi_learn_started_at = time.time()
    # The clicked M turns AMBER so the user sees which control the next MIDI
    # message binds; the previously armed M returns to red (user, 2026-09-07).
    if state.midi_learn_armed_tag and state.midi_learn_armed_tag != sender:
        _style_learn_marker(state.midi_learn_armed_tag, False)
    state.midi_learn_armed_tag = sender
    _style_learn_marker(sender, True)
    if dpg.does_item_exist("midi_learn_status"):
        dpg.set_value("midi_learn_status", "Now press your MIDI button")


def _sync_media_learn_bar() -> None:
    """Create or drop the Mediagrid source-browsing learn bar (e33s04).

    While MIDI Learn is on, the sources window shows ONE grouped marker bar
    above the grid — the user asked for fixed slots with separators: three
    generic markers (next/prev/regen), a dash, the 8 step-sequencer slots, a
    dash, the 4 Mapper slots. Slot bindings are selection-relative and
    pre-bindable even when fewer rows exist (a missing line is a logged
    no-op). Leaving learn mode removes the bar; rebuilt from scratch each time.
    """
    bar_tag = "media_learn_bar"
    if dpg.does_item_exist(bar_tag):
        dpg.delete_item(bar_tag)
    if not state.midi_learn_mode:
        return
    if not dpg.does_item_exist("vimix_media_group"):
        return
    bar_kwargs: dict[str, Any] = {
        "parent": "vimix_media_group",
        "horizontal": True,
        "tag": bar_tag,
    }
    if dpg.does_item_exist("media_grid"):
        bar_kwargs["before"] = "media_grid"  # stay above the tiles
    with dpg.group(**bar_kwargs) as bar:
        learn_marker(MIDI_ACTION_SOURCE_NEXT, {}, parent=bar, tag="media_mk_next")
        learn_marker(MIDI_ACTION_SOURCE_PREV, {}, parent=bar, tag="media_mk_prev")
        learn_marker(MIDI_ACTION_REGEN_SELECTED, {}, parent=bar, tag="media_mk_regen")
        learn_marker(MIDI_ACTION_ENABLE_CORRECTION, {}, parent=bar, tag="media_mk_cc")
        _learn_group_gap(bar)
        for slot in range(1, NUM_TRACKS + 1):
            learn_marker(
                MIDI_ACTION_SEQ_ROW_ASSIGN,
                {"row": slot - 1},
                parent=bar,
                tag=f"media_mk_seq_{slot}",
                tooltip=f"Map: sequencer line {slot} (selected source)",
            )
        _learn_group_gap(bar)
        for slot in range(1, MAPPER_LEARN_SLOTS + 1):
            learn_marker(
                MIDI_ACTION_MAPPER_LINE,
                {"line": slot - 1},
                parent=bar,
                tag=f"media_mk_map_{slot}",
                tooltip=f"Map: mapper line {slot} (selected source)",
            )


def _learn_group_gap(parent: Any) -> None:
    """A wider gap BETWEEN the learn-bar groups (e33s04)."""
    dpg.add_spacer(width=MARKER_GROUP_GAP, parent=parent)


def _sync_sequencer_learn_strip() -> None:
    """Create or drop the sequencer transport learn strip (e33s04).

    While MIDI Learn is on, a strip below the transport row offers one red-M
    marker per transport/beat-source control (Play, Resync, nudges, Tap, the
    four beat sources) — the uniform marker replacement for their old
    capture-on-click learn. Leaving learn mode removes the strip.
    """
    strip_tag = "seq_transport_learn"
    if dpg.does_item_exist(strip_tag):
        dpg.delete_item(strip_tag)
    if not state.midi_learn_mode:
        return
    if not dpg.does_item_exist("seq_table"):
        return
    with dpg.group(
        parent="sequencer_window",
        horizontal=True,
        tag=strip_tag,
        before="seq_table",
    ) as strip:
        _strip_marker(strip, MIDI_ACTION_TRANSPORT_PLAY, {}, "seq_mk_play")
        _strip_marker(strip, MIDI_ACTION_TRANSPORT_RESYNC, {}, "seq_mk_resync")
        _strip_marker(strip, MIDI_ACTION_NUDGE_BACK, {}, "seq_mk_nudge_back")
        _strip_marker(strip, MIDI_ACTION_NUDGE_FORWARD, {}, "seq_mk_nudge_forward")
        _strip_marker(strip, MIDI_ACTION_TRANSPORT_TAP, {}, "seq_mk_tap")
        _learn_group_gap(strip)
        for mode in (
            BEAT_SOURCE_ANALYSIS,
            BEAT_SOURCE_BAND1,
            BEAT_SOURCE_MIDI,
            BEAT_SOURCE_MANUAL,
        ):
            label = BEAT_SOURCE_LABELS.get(mode, mode)
            learn_marker(
                MIDI_ACTION_BEAT_SOURCE,
                {"mode": mode},
                parent=strip,
                tag=f"seq_mk_beat_{mode}",
                tooltip=f"Map: beat source ({label})",
            )


def _strip_marker(strip: Any, action_id: str, params: dict[str, Any], tag: str) -> None:
    """One transport learn marker inside the sequencer strip."""
    learn_marker(action_id, params, parent=strip, tag=tag)


def _sync_seq_row_learn_strip() -> None:
    """Create or drop the sequencer ROW learn strip (e36s07).

    While MIDI Learn is on, a strip below the transport markers offers one red-M
    marker per row for Enable row and one for Disable row (params carry the
    stable row slot), so the e33 rule holds for the new context actions. Leaving
    learn mode removes the strip.
    """
    strip_tag = "seq_row_learn"
    if dpg.does_item_exist(strip_tag):
        dpg.delete_item(strip_tag)
    if not state.midi_learn_mode:
        return
    if not dpg.does_item_exist("seq_table"):
        return
    with dpg.group(
        parent="sequencer_window",
        horizontal=True,
        tag=strip_tag,
        before="seq_table",
    ) as strip:
        for slot in range(1, NUM_TRACKS + 1):
            learn_marker(
                MIDI_ACTION_SEQ_ROW_ENABLE,
                {"row": slot - 1},
                parent=strip,
                tag=f"seq_row_mk_en_{slot}",
                tooltip=f"Map: enable sequencer line {slot}",
            )
        _learn_group_gap(strip)
        for slot in range(1, NUM_TRACKS + 1):
            learn_marker(
                MIDI_ACTION_SEQ_ROW_DISABLE,
                {"row": slot - 1},
                parent=strip,
                tag=f"seq_row_mk_dis_{slot}",
                tooltip=f"Map: disable sequencer line {slot}",
            )


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
        save_midi_controllers()  # 2026-09-07: a learned mapping persists immediately
    else:
        midi_bindings.append(binding)
    state.midi_learn_pending = None
    _exit_midi_learn()
    refresh_midi_mappings_ui()
    if action == MIDI_ACTION_MAPPER_MAPPING:  # e18: bind the learned control to the mapping
        mapper.set_mapping_midi(int(params.get("mapping_id", 0)), binding)
        refresh_mapper_ui()  # show the input-range line of the bound source
    if dpg.does_item_exist("midi_learn_btn"):
        dpg.set_item_label("midi_learn_btn", "Learn mapping...")
    if dpg.does_item_exist("midi_learn_status"):
        dpg.set_value(
            "midi_learn_status",
            "Bound: "
            f"{actions.action_label(action)} <- "
            f"{binding['device']} {binding['type']} {binding['number']}",
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
    _refresh_learn_surfaces()  # e33s02: show the learn markers on the Mapper body
    _sync_context_learn_labels()  # static menus (step cell, monitor) follow the mode


def on_midi_enable(sender: Any, app_data: Any, user_data: Any) -> None:
    """MIDI window Enable checkbox: persist and apply the engine toggle (e09s02)."""
    set_midi_enabled(bool(app_data))
    if not app_data and state.midi_learn_mode:  # disabling cancels an in-flight learn
        _exit_midi_learn()


# 2026-09-06 (user): context-menu arm items on STATIC menus (step cell, monitor
# player head) need their label synced with the mode — their popups are not
# rebuilt per open, so toggle tracks their tags and re-labels them in place.
_context_learn_item_tags: set[str] = set()


def _sync_context_learn_labels() -> None:
    """Re-label the registered static context learn items to follow the mode.

    Menus rebuilt per open (Mapper card menu, Mediagrid tile popup) read the
    mode at build time and never register; the static step-cell and monitor
    menus register a stable tag here so arm/cancel stays correct everywhere.
    """
    label = "Cancel MIDI Learn" if state.midi_learn_mode else "MIDI Learn..."
    for tag in _context_learn_item_tags:
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, label=label)


def _add_context_learn_item(tag: str | None = None) -> None:
    """One context-menu item that arms/cancels MIDI Learn mode (user, 2026-09-06).

    The right-click surfaces (Mapper card menu, Mediagrid tile popup) reuse the
    one toggle_midi_learn path of the MIDI window button, so arming the mode
    never requires opening that window. The label reflects the state at build
    time: 'MIDI Learn...' arms; 'Cancel MIDI Learn' exits. It is a MODE item —
    it never anchors a binding to the right-clicked entity (the e33s04 rule on
    volatile sources stands: capture happens on the markers that then appear).
    Static menus (step cell, monitor head) pass a stable tag: the label is then
    kept current by _sync_context_learn_labels on every mode transition.
    """
    if tag is not None:
        _context_learn_item_tags.add(tag)
    item_kwargs: dict[str, Any] = {
        "label": "Cancel MIDI Learn" if state.midi_learn_mode else "MIDI Learn...",
        "callback": toggle_midi_learn,
    }
    if tag is not None:
        item_kwargs["tag"] = tag  # tag=None would raise 'Must be int' in real DPG
    dpg.add_menu_item(**item_kwargs)


def _midi_binding_label(binding: dict[str, Any]) -> str:
    """Human-readable row label for one mapping (e09s02)."""
    params = binding.get("params") or {}
    suffix = f" {params}" if params else ""
    return (
        f"{binding.get('device', '?')} {binding.get('type', '?')} "
        f"{binding.get('number', '?')} -> "
        f"{actions.action_label(str(binding.get('action', '?')))}{suffix}"
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
        save_midi_controllers()  # 2026-09-07: deletions persist immediately
    refresh_midi_mappings_ui()


def refresh_midi_devices(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Re-scan MIDI inputs, update the Controllers Add combo and the port-status
    line (e14s03; BUG-2026-09-07 — a failed scan is shown, never silently empty)."""
    names, error = scan_midi_inputs()
    if dpg.does_item_exist("midi_add_combo"):
        used = {c["port"] for c in midi_controllers}
        dpg.configure_item("midi_add_combo", items=[n for n in names if n not in used])
    _update_midi_ports_status(names, error)


def _update_midi_ports_status(names: list[str], error: str | None) -> None:
    """Write the input-scan outcome to the MIDI window status line (BUG-2026-09-07)."""
    if not dpg.does_item_exist("midi_ports_status"):
        return
    text = f"Port scan failed: {error}" if error else f"{len(names)} MIDI input(s) available"
    dpg.set_value("midi_ports_status", text)


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
    # BUG-2026-09-07: combo + port-status line follow the scan
    refresh_midi_devices()
    refresh_midi_mappings_ui()


def show_midi_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the MIDI window from the menubar, with a fresh device list, controller
    list and the selected controller's mappings (render_controllers_ui refreshes
    the mappings too)."""
    refresh_midi_devices()
    render_controllers_ui()
    dpg.show_item("midi_window")
    dpg.focus_item("midi_window")  # e17: a shown window must come to the front


# --- MIDI MONITOR (e39s01) ---------------------------------------------------
# Diagnostic window: the incoming MIDI stream WITH the resolution outcome plus a
# per-control calibration table. NOT in LAYOUT_WINDOW_TAGS — a saved layout must
# never pop a debug window at boot; geometry is session-only.
MIDI_MONITOR_WINDOW_WIDTH = 980
MIDI_MONITOR_WINDOW_HEIGHT = 620
MIDI_MONITOR_TEXT_HEIGHT = 250
MIDI_MONITOR_ALL_PORTS = "All ports"

_midi_monitor_paused = False
_midi_monitor_port: str | None = None
_midi_monitor_ports: list[str] = []
_midi_monitor_filter_control: tuple[str, str, int, int] | None = None
_midi_monitor_control_items: dict[str, tuple[str, str, int, int]] = {}
_midi_monitor_mapping_items: dict[str, int] = {}
_midi_monitor_last_revision = -1
_midi_monitor_last_refresh = 0.0


def _midi_monitor_port_options() -> list[str]:
    """The port-filter options: 'All ports' plus every port seen or configured."""
    ports = {str(row["port"]) for row in midimonitor.snapshot_controls()}
    ports.update(str(c.get("port", "")) for c in midi_controllers if c.get("port"))
    return [MIDI_MONITOR_ALL_PORTS, *sorted(p for p in ports if p)]


def _monitor_sync_ports() -> None:
    """Keep the port combo in sync with the ports seen/configured (e39s01).

    Tracks the last rendered option list in a module global instead of reading
    the widget: a reconfigure happens only when the set actually changed.
    """
    global _midi_monitor_ports
    options = _midi_monitor_port_options()
    if options == _midi_monitor_ports:
        return
    _midi_monitor_ports = options
    if dpg.does_item_exist("midi_monitor_port"):
        dpg.configure_item("midi_monitor_port", items=options)


def _control_label(control: tuple[str, str, int, int]) -> str:
    """Picker label of one control: 'cc ch0 #7 @port'."""
    port, msg_type, channel, number = control
    return f"{msg_type} ch{int(channel)} #{int(number)} @{port}"


def _mapping_label(mapping: dict[str, Any]) -> str:
    """Picker label of one Mapper mapping: '#3 speed (clipA)'."""
    return f"#{int(mapping['id'])} {mapping.get('property', '?')} ({mapping.get('target_id', '?')})"


def _monitor_sync_controls() -> None:
    """Keep the control + mapping pickers in sync (e39s03).

    Tracks the last rendered items in module globals and reconfigures only when
    the sets changed, so a selected value is never reset per refresh.
    """
    global _midi_monitor_control_items, _midi_monitor_mapping_items
    controls = {
        _control_label(tuple(row["key"])): tuple(row["key"])
        for row in midimonitor.snapshot_controls()
    }
    if controls != _midi_monitor_control_items:
        _midi_monitor_control_items = controls
        if dpg.does_item_exist("midi_monitor_control_combo"):
            dpg.configure_item("midi_monitor_control_combo", items=list(controls))
    mappings = {_mapping_label(m): int(m["id"]) for m in state.mapper_mappings}
    if mappings != _midi_monitor_mapping_items:
        _midi_monitor_mapping_items = mappings
        if dpg.does_item_exist("midi_monitor_mapping_combo"):
            dpg.configure_item("midi_monitor_mapping_combo", items=list(mappings))


def show_midi_monitor(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open (or raise) the MIDI Monitor diagnostic window (e39s01)."""
    if dpg.does_item_exist("midi_monitor_window"):
        dpg.show_item("midi_monitor_window")
        dpg.focus_item("midi_monitor_window")
        return
    _build_midi_monitor_window()


def _build_midi_monitor_window() -> None:
    """Build the monitor window (toolbar + stream/controls panes, e39s01)."""
    with dpg.window(
        label="MIDI Monitor",
        tag="midi_monitor_window",
        width=MIDI_MONITOR_WINDOW_WIDTH,
        height=MIDI_MONITOR_WINDOW_HEIGHT,
        pos=(60, 60),
    ):
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="Pause", tag="midi_monitor_pause", callback=toggle_midi_monitor_pause
            )
            dpg.add_button(label="Clear", callback=clear_midi_monitor)
            dpg.add_button(label="Reset stats", callback=reset_midi_monitor_stats)
            dpg.add_button(label="Copy report", callback=copy_midi_monitor_report)
            themed_text("Port", slot="text_dim")
            dpg.add_combo(
                items=_midi_monitor_port_options(),
                default_value=MIDI_MONITOR_ALL_PORTS,
                width=220,
                tag="midi_monitor_port",
                callback=on_midi_monitor_port,
            )
            dpg.add_group(tag="midi_monitor_learn_slot", horizontal=True)
        with dpg.group(horizontal=True):
            themed_text("Control", slot="text_dim")
            dpg.add_combo(items=[], width=240, tag="midi_monitor_control_combo")
            dpg.add_button(label="Filter", callback=filter_midi_monitor_control)
            dpg.add_button(label="Copy id", callback=copy_midi_monitor_control_id)
            themed_text("Mapping", slot="text_dim")
            dpg.add_combo(items=[], width=240, tag="midi_monitor_mapping_combo")
            dpg.add_button(label="Assign", callback=assign_midi_monitor_control)
        themed_text("Stream (newest first)", slot="text_dim")
        dpg.add_input_text(
            tag="midi_monitor_stream_text",
            multiline=True,
            readonly=True,
            width=-1,
            height=MIDI_MONITOR_TEXT_HEIGHT,
            default_value="No MIDI input yet.",
        )
        themed_text("Controls", slot="text_dim")
        dpg.add_input_text(
            tag="midi_monitor_controls_text",
            multiline=True,
            readonly=True,
            width=-1,
            height=MIDI_MONITOR_TEXT_HEIGHT,
            default_value="No control seen yet.",
        )
    _sync_monitor_learn_marker()
    refresh_midi_monitor()


def toggle_midi_monitor_window(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Show / close the MIDI Monitor window (MIDI_MONITOR_TOGGLE executor, e39s01)."""
    if dpg.does_item_exist("midi_monitor_window"):
        dpg.delete_item("midi_monitor_window")
        return
    show_midi_monitor()


def _exec_monitor_toggle(params: dict[str, Any], value: int) -> None:
    """e39s01: a momentary press (CC >= 64 / note) shows or closes the monitor."""
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    toggle_midi_monitor_window()


def _exec_route_toggle(params: dict[str, Any], value: int) -> None:
    """e40s01: a momentary press arms/disarms a Route (its Enabled gate).

    The arm checkbox follows in place when its row is on screen; a stale
    route id is a logged no-op.
    """
    if value < MIDI_CC_TRIGGER_THRESHOLD:
        return
    route = mapper.find_mapping(int(params.get("route_id", 0)))
    if route is None:
        _log_stale_midi_target(MIDI_ACTION_ROUTE_TOGGLE, f"no route {params.get('route_id')}")
        return
    enabled = not bool(route.get("enabled", False))
    mapper.set_mapping_enabled(int(route["id"]), enabled)
    if dpg.does_item_exist(f"route_enable_{route['id']}"):
        dpg.set_value(f"route_enable_{route['id']}", enabled)


def _sync_monitor_learn_marker() -> None:
    """(Re)render the monitor toggle's learn marker (e39s01, e33 rule).

    Markers exist only while learn mode is on; _refresh_learn_surfaces calls
    this on every learn transition (the slot lives in the window toolbar).
    """
    if not dpg.does_item_exist("midi_monitor_learn_slot"):
        return
    dpg.delete_item("midi_monitor_learn_slot", children_only=True)
    if state.midi_learn_mode:
        learn_marker(
            MIDI_ACTION_MONITOR_TOGGLE,
            {},
            parent="midi_monitor_learn_slot",
            tag="midi_monitor_mk_toggle",
            tooltip="Map: MIDI Monitor window",
        )


def on_midi_monitor_port(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Port-filter combo: 'All ports' clears the filter, any port narrows both panes."""
    global _midi_monitor_port
    value = str(app_data or MIDI_MONITOR_ALL_PORTS)
    _midi_monitor_port = None if value == MIDI_MONITOR_ALL_PORTS else value
    refresh_midi_monitor()


def toggle_midi_monitor_pause(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Pause freezes the view (recording continues); resume repaints at once."""
    global _midi_monitor_paused
    _midi_monitor_paused = not _midi_monitor_paused
    if dpg.does_item_exist("midi_monitor_pause"):
        dpg.set_item_label("midi_monitor_pause", "Resume" if _midi_monitor_paused else "Pause")
    if not _midi_monitor_paused:
        refresh_midi_monitor()


def clear_midi_monitor(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Drop both panes' data (Clear button, e39s01)."""
    midimonitor.clear()
    refresh_midi_monitor()


def reset_midi_monitor_stats(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Zero the per-control envelopes/counters but keep the rows (e39s02)."""
    midimonitor.reset_controls()
    refresh_midi_monitor()


def _midi_monitor_texts() -> tuple[str, str]:
    """The current (stream, controls) pane texts under the active filters."""
    port = _midi_monitor_port
    stream = midimonitor.format_stream(
        midimonitor.snapshot_stream(port=port, control=_midi_monitor_filter_control)
    )
    return stream, midimonitor.format_controls(midimonitor.snapshot_controls(port=port))


def refresh_midi_monitor() -> None:
    """Write both pane texts and remember the engine revision (e39s01)."""
    global _midi_monitor_last_revision, _midi_monitor_last_refresh
    if not dpg.does_item_exist("midi_monitor_window"):
        return
    _midi_monitor_last_revision = midimonitor.revision()
    _midi_monitor_last_refresh = time.monotonic()
    _monitor_sync_ports()
    _monitor_sync_controls()
    stream_text, controls_text = _midi_monitor_texts()
    dpg.set_value("midi_monitor_stream_text", stream_text)
    dpg.set_value("midi_monitor_controls_text", controls_text)


def copy_midi_monitor_report(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Put the stream + controls snapshot on the clipboard (Copy report)."""
    stream_text, controls_text = _midi_monitor_texts()
    dpg.set_clipboard_text(midimonitor.format_report(stream_text, controls_text))


def _midi_monitor_selected_control() -> tuple[str, str, int, int] | None:
    """The control picked in the picker (None when nothing is selected)."""
    return _midi_monitor_control_items.get(str(dpg.get_value("midi_monitor_control_combo")))


def filter_midi_monitor_control(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Toggle the stream filter onto the picked control (e39s03)."""
    global _midi_monitor_filter_control
    control = _midi_monitor_selected_control()
    if control is None:
        return
    _midi_monitor_filter_control = None if control == _midi_monitor_filter_control else control
    refresh_midi_monitor()


def copy_midi_monitor_control_id(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Put the picked control's id on the clipboard (e39s03)."""
    control = _midi_monitor_selected_control()
    if control is None:
        return
    dpg.set_clipboard_text(" ".join(str(part) for part in control))


def assign_midi_monitor_control(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Bind the picked control to the picked Mapper mapping (e39s03)."""
    control = _midi_monitor_selected_control()
    mapping_id = _midi_monitor_mapping_items.get(str(dpg.get_value("midi_monitor_mapping_combo")))
    if control is None or mapping_id is None:
        return
    assign_control_to_mapping(control, mapping_id)


def assign_control_to_mapping(control: tuple[str, str, int, int], mapping_id: int) -> bool:
    """Bind an already-seen control to a Mapper mapping immediately (e39s03).

    The reverse of a learn session: the monitor already knows the source
    (device/channel/type/number), so the dispatch binding lands on the owning
    controller (or the legacy flat list) and the mapping's stored MIDI source
    plus its 0..127 input range are set at once. An unknown mapping id is a
    logged no-op.
    """
    port, msg_type, channel, number = control
    if mapper.find_mapping(mapping_id) is None:
        log_error("MIDI Monitor", f"no mapping {mapping_id}")
        return False
    source: dict[str, Any] = {
        "device": port,
        "channel": int(channel),
        "type": msg_type,
        "number": int(number),
    }
    binding = {**source, "action": MIDI_ACTION_MAPPER_MAPPING, "params": {"mapping_id": mapping_id}}
    controller = find_controller_by_port(port)
    if controller is not None:
        controller.setdefault("bindings", []).append(binding)
    else:
        midi_bindings.append(binding)
    save_midi_controllers()
    mapper.set_mapping_midi(mapping_id, binding)
    refresh_midi_mappings_ui()
    refresh_mapper_ui()
    return True


def tick_midi_monitor() -> None:
    """Refresh the MIDI Monitor panes at the capped cadence (e39s01).

    Cheap no-op when the window is closed or the engine revision is unchanged;
    a spinning wheel coalesces to at most one repaint per
    MIDI_MONITOR_REFRESH_INTERVAL.
    """
    if not dpg.does_item_exist("midi_monitor_window") or _midi_monitor_paused:
        return
    if midimonitor.revision() == _midi_monitor_last_revision:
        return
    if time.monotonic() - _midi_monitor_last_refresh < MIDI_MONITOR_REFRESH_INTERVAL:
        return
    refresh_midi_monitor()


# --- ROUTES (e40s01) ---------------------------------------------------------
# The main-loop emission tick: refresh the enabled State Routes, remap and emit
# their Destination values with an epsilon dedupe. The Vimix Destination is
# driven by its own control path; the OSC Destination arrives in e40s03.

_routes_last_tick = 0.0


def _route_props_lookup(target_id: str) -> dict[str, Any] | None:
    """The live properties of a Route's source (the viOSC state table)."""
    _, props = find_source_by_name(target_id)
    return props


def _sync_route_subscriptions() -> None:
    """Hold one /viosc/monitor subscription per (source, properties union).

    Re-issues only when the desired set changes (the tick runs every frame) and
    stops the subscriptions no longer needed — the transport coalescing of
    ADR-route-model decision 2.
    """
    desired = routeengine.state_subscriptions(state.mapper_mappings)
    if desired == state.route_subscriptions:
        return
    for target, props in desired.items():
        addr = f"/viosc/monitor/{target}"
        osc_client.send_message(addr, list(props))
        append_log("OUT", f"{addr} {props}")
    for target in set(state.route_subscriptions) - set(desired):
        addr = f"/viosc/monitor/{target}"
        osc_client.send_message(addr, [])
        append_log("OUT", f"{addr} (stop)")
    state.route_subscriptions = desired


def _emit_route(route: dict[str, Any], value: float) -> None:
    """Send one Route's Destination value (e40s01: MIDI; OSC is e40s03)."""
    if mapper.destination_of(route) != DEST_MIDI:
        return
    spec = route.get("destination_spec") or {}
    controller = find_controller_by_port(str(spec.get("controller_port") or ""))
    if controller is None or not ensure_route_output(controller):
        return
    send_route_midi(
        controller,
        str(spec.get("type") or MIDI_KIND_CC),
        int(spec.get("channel", 0)),
        int(spec.get("number", 0)),
        round(value),
    )


def tick_routes(now: float | None = None) -> None:
    """Emit the changed Route values, capped at ROUTE_TICK_INTERVAL_S (e40s01)."""
    global _routes_last_tick
    if now is None:
        now = time.monotonic()
    if now - _routes_last_tick < ROUTE_TICK_INTERVAL_S:
        return
    _routes_last_tick = now
    _sync_route_subscriptions()
    live_ids = {int(m["id"]) for m in state.mapper_mappings}
    routeengine.prune_book(state.route_book, state.route_values, live_ids)
    for route, value in routeengine.plan_emissions(
        state.mapper_mappings, _route_props_lookup, state.route_book, state.route_values, now
    ):
        _emit_route(route, value)


def show_leap_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the Leap Motion window from Settings > Leap Motion (e26s01)."""
    dpg.show_item("leap_window")
    dpg.focus_item("leap_window")  # e17: a shown window must come to the front


def on_leap_enable(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Leap window Enable checkbox: persist and apply the engine toggle (e26s01).

    The worker loop reacts to state.leap_enabled on its own cadence. Disabling
    also folds the visualizer (no engine -> no frames); re-enabling restores it
    when the visualizer flag is still on (e26s04).
    """
    enabled = bool(app_data)
    set_leap_enabled(enabled)
    if dpg.does_item_exist("leap_viz_cb"):
        dpg.configure_item("leap_viz_cb", enabled=enabled)
    if not enabled:
        _apply_leap_viz_layout(False)
    elif state.leap_visualizer:
        _apply_leap_viz_layout(True)


def on_leap_visualizer(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Visualizer toggle (e26s04): persist + fold/unfold the panel in place.

    The worker watches state.leap_visualizer and sets/clears the LeapC Images
    policy on its own cadence; no connection work happens here.
    """
    set_leap_visualizer(bool(app_data))
    _apply_leap_viz_layout(bool(app_data))


def _apply_leap_viz_layout(shown: bool) -> None:
    """Fold/unfold the visualizer panel inside the COMPACT window (e26s04).

    The Leap Motion window never widens (user decision): showing the panel
    shrinks the monitor child so the fixed 560x680 size never overflows.
    """
    if shown:
        dpg.show_item("leap_viz_panel")
        dpg.configure_item("leap_monitor_scroll", height=LEAP_MONITOR_VIZ_H)
    else:
        dpg.hide_item("leap_viz_panel")
        dpg.configure_item("leap_monitor_scroll", height=LEAP_MONITOR_H)


def _leap_monitor_set_text(tag: str, text: str) -> None:
    """Update one leap-window text tag only when its rendered text changed (e26s02)."""
    if state.leap_monitor_cache.get(tag) != text:
        state.leap_monitor_cache[tag] = text
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, text)


def tick_leap_visualizer() -> None:
    """Upload the latest visualizer frame on the main loop (e26s04).

    No-op while the window is missing/hidden or the engine/visualizer is off.
    The poll thread publishes frames (state.leap_viz_frame + seq) only while
    enabled; this tick lazily creates ONE raw texture on the first frame and
    set_value only when a NEW frame arrived — a still feed costs nothing.
    """
    if not dpg.does_item_exist("leap_window"):
        return
    if not dpg.is_item_shown("leap_window"):
        return
    if not (state.leap_enabled and state.leap_visualizer):
        return
    with state.leap_lock:
        seq = state.leap_viz_seq
        frame = state.leap_viz_frame
    if frame is None or seq == state.leap_viz_uploaded_seq:
        return
    _leap_viz_ensure_texture(frame)
    dpg.set_value("leap_viz_tex", frame.reshape(-1))
    state.leap_viz_uploaded_seq = seq
    if dpg.does_item_exist("leap_viz_wait_text"):
        dpg.hide_item("leap_viz_wait_text")


def _leap_viz_ensure_texture(frame: Any) -> None:
    """Create the raw texture + image ONCE, on the first published frame (e26s04).

    Texture format follows the thumbnail pipeline (RGBA float32 0..1); unlike
    thumbnails this texture is created once and updated via set_value (raw
    texture), never deleted/recreated per frame. The one-shot guard is a state
    flag (does_item_exist reads true in the headless stub), main-thread only.
    """
    if state.leap_viz_tex_created:
        return
    h, w = int(frame.shape[0]), int(frame.shape[1])
    dpg.add_raw_texture(
        width=w,
        height=h,
        default_value=frame.reshape(-1),
        tag="leap_viz_tex",
        parent="texture_registry",
    )
    dpg.add_image("leap_viz_tex", tag="leap_viz_img", parent="leap_viz_panel")
    state.leap_viz_tex_created = True


def tick_leap_monitor() -> None:
    """Refresh the Leap Motion window on the main loop (e26s02): status line +
    live two-hand values from the worker snapshot, writing only on change.

    No-op while the window does not exist or is hidden. Values are read under
    state.leap_lock (shallow copy) and rendered via leap.format_value; an
    absent hand or a disabled engine shows the placeholder (cache-gated, so a
    still hand or an idle window costs nothing).
    """
    if not dpg.does_item_exist("leap_window"):
        return
    if not dpg.is_item_shown("leap_window"):
        return
    enabled = state.leap_enabled
    _leap_monitor_set_text("leap_status_text", leap.leap_status_label(enabled, state.leap_status))
    if enabled:
        with state.leap_lock:
            snapshot = dict(state.leap_values)
    else:
        snapshot = {}
    for hand in leap.LEAP_HANDS:
        for field in leap.LEAP_FIELDS:
            raw = snapshot.get(f"{hand}.{field}") if enabled else None
            text = leap.LEAP_MONITOR_PLACEHOLDER if raw is None else leap.format_value(field, raw)
            _leap_monitor_set_text(f"leap_mon_{hand}_{field}", text)


# ---------- e16/e22: Mapper (OSC property mappings -> per-source rows) ----------
# e22s01: the body is one horizontal row per SOURCE — the source thumbnail at
# the sequencer slot size, then that source's mapping mini-cards to the right.


def _mapper_font() -> Any:
    """The small ProggyTiny font the Mapper texts/controls use (e23 compact).

    Same 10 px font as the Vimix-sources tile titles; None when the bundled
    asset is missing (then the default font is used and the compact geometry
    still fits — labels are the widest at ~7 px/char)."""
    return _tile_title_font


def _bind_mapper_font(tag: str) -> None:
    """Bind the compact mapper font to an item tag when it exists."""
    font = _mapper_font()
    if font is not None and dpg.does_item_exist(tag):
        dpg.bind_item_font(tag, font)


def _mapper_caption_spacer(label: str, spec: dict[str, Any], has_reset: bool = True) -> int:
    """Spacer width that right-aligns the enable checkbox + X on a caption (e24).

    The caption is ONE row: label + spacer + enable checkbox + X. Labels render
    in the 10 px ProggyTiny mapper font (MAPPER_SMALL_CHAR_PX = 6 px/char), so
    the budget uses that advance and subtracts the checkbox + X blocks + item
    gaps: every catalog property label fits on a single caption row. A
    cue-list card drops the R button (UAT e35) — the spacer must not reserve
    its width, or its enable + X would sit left of the right edge.
    """
    return max(
        2,
        MAPPER_MINI_W
        - 24
        - MAPPER_SMALL_CHAR_PX * len(label)
        - (MAPPER_RESET_W if has_reset else 0)
        - MAPPER_CB_W
        - MAPPER_X_W,
    )


def _mapper_row_height(mappings: list[dict[str, Any]]) -> int:
    """Compact uniform height for one source row (e23 bugfix, e34s02 model).

    Fits the tallest mini-card in the row over the models that are present:
    the legacy vertical anatomy (caption + 17 px slider + the 'output:' line +
    the 'input:' line when a source is bound — sliders, answer A) and the
    compact band anatomy (caption + the 44 px knob/button band, its readouts
    INSIDE the band). A row mixing both takes the taller model; the card rows
    are height-constant across learn transitions — the card markers render in
    dedicated strip lines UNDER each card line (e34s03), outside the cards.
    Measured on DPG 2.3.1 with the compact mapper theme (10 px ProggyTiny
    font, WindowPadding 4, FramePadding y 2, ItemSpacing y 2).
    """
    pad, x_h, gap, text = (
        MAPPER_ROW_PAD_V,
        MAPPER_X_H,
        MAPPER_ROW_GAP,
        MAPPER_TEXT_H,
    )
    any_source = any(
        m.get("band") is not None or m.get("midi") is not None or m.get("leap") is not None
        for m in mappings
    )
    height = MAPPER_ROW_THUMB_H + 2  # the row always fits the 70 px thumbnail
    if any(m["control"] in ("knob", "button", "cue list") for m in mappings):
        height = max(height, pad + x_h + gap + MAPPER_KNOB_H)  # compact band
    if any(m["control"] == "cue list" for m in mappings):
        # UAT e35: the cue readout column also hosts the 'N of Total' progress
        # text UNDER the 'Cue list...' button — the card needs one extra line.
        height = max(height, pad + x_h + gap + MAPPER_KNOB_H + text)
    if any(m["control"] == "slider" for m in mappings):
        slider_h = pad + x_h + gap + MAPPER_CTRL_H + gap + text
        if any_source:
            slider_h += gap + text
        height = max(height, slider_h)
    return height


def _mapper_line_number(row_no: int, target_id: str, parent: Any, height: int) -> None:
    """Small 1-based row number left of the row thumbnail (e32s02).

    A narrow borderless slot as tall as the row whose ProggyTiny digit is
    vertically centered — the same spacer technique the thumbnail slot uses —
    and horizontally centered in the narrow column, so every row reads
    ' 1  [thumb] cards' with the digit balanced between the row edge and the
    thumbnail (the number matches the tile menu's "Add to Mapper > line N";
    both number mapper.row_targets() order).
    """
    digits = len(str(row_no))
    # monospace ProggyTiny digits: center the digit(s) in the fixed column;
    # two digits fill it flush (e32s02 tuning)
    indent = max(0, (MAPPER_LINE_NO_W - digits * MAPPER_LINE_NO_DIGIT_PX) // 2)
    with dpg.child_window(
        parent=parent,
        width=MAPPER_LINE_NO_W,
        height=height,
        border=False,
        no_scrollbar=True,
        tag=f"mapper_line_no_{target_id}",
    ):
        dpg.add_spacer(height=max(0, (height - MAPPER_LINE_NO_TEXT_H) // 2))
        themed_text(
            str(row_no),
            slot="text_dim",
            indent=indent,
            tag=f"mapper_line_no_txt_{target_id}",
        )
    font = _mapper_line_no_font or _mapper_font()
    if font is not None and dpg.does_item_exist(f"mapper_line_no_txt_{target_id}"):
        dpg.bind_item_font(f"mapper_line_no_txt_{target_id}", font)


def _mapper_row_thumb(target_id: str, parent: Any, height: int) -> None:
    """The source thumbnail slot at the start of a mapper row (e22s01).

    A fixed-width slot as tall as the row with the sequencer-size thumbnail
    (110x70) vertically centered inside, so the image aligns with the mini-card
    content next to it; when no texture exists yet the slot shows a "no thumb"
    placeholder (same footprint, rows stay aligned). e29s01: the slot is a
    sequencer-style apply box — a left click on its inner items (child windows
    cannot host clicked handlers on DPG 2.3.1) re-targets the row onto the
    media selected in the grid; the "no thumb" placeholder is clickable too.
    """
    with dpg.child_window(
        parent=parent,
        width=MAPPER_ROW_THUMB_W,
        height=height,
        border=False,
        no_scrollbar=True,
        tag=f"mapper_row_thumb_{target_id}",
    ):
        dpg.add_spacer(height=max(0, (height - MAPPER_ROW_THUMB_H) // 2))
        click_reg_tag = f"mapper_row_click_reg_{target_id}"
        if dpg.does_item_exist(click_reg_tag):
            dpg.delete_item(click_reg_tag)  # a rebuild must not leak registries
        with dpg.item_handler_registry(tag=click_reg_tag):
            dpg.add_item_clicked_handler(
                0,  # left click applies the selected source (e29s01)
                callback=lambda *_, t=target_id: on_mapper_row_thumb_click(None, None, t),
            )
        tex_tags = thumbnails_data.get(target_id)
        if tex_tags:
            tex_tag = tex_tags[0]
            if dpg.does_item_exist(tex_tag):
                dpg.add_image(
                    texture_tag=tex_tag,
                    width=MAPPER_ROW_THUMB_W,
                    height=MAPPER_ROW_THUMB_H,
                    tag=f"mapper_row_img_{target_id}",  # e25: stable tag for the frame cycle
                )
                dpg.bind_item_handler_registry(f"mapper_row_img_{target_id}", click_reg_tag)
                return
        no_tag = f"mapper_row_nothumb_{target_id}"  # e29s01: stable tag, clickable placeholder
        themed_text("no thumb", slot="text_dim", tag=no_tag)
        dpg.bind_item_handler_registry(no_tag, click_reg_tag)


def on_mapper_row_thumb_click(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Row-thumb click: apply the selected media to the whole row (e29s01).

    Mirrors the sequencer clip-slot click: the media selected in the Vimix
    sources grid (state.viseq_selected_source) becomes the row's source —
    every mapping of the row re-targets onto it and the Mapper body rebuilds
    (rows regroup by first appearance; an existing row of the new source
    merges). A click with NO grid selection, or on a row already targeting
    the selection, is a no-op: deliberately NOT the sequencer's
    get_current_target_id() vimix-current fallback (user-confirmed) — the
    source of a mapper row is a deliberate choice.
    """
    target_id = user_data
    selected = state.viseq_selected_source
    if selected is None or selected == target_id:
        return
    if mapper.retarget_source(target_id, selected):
        refresh_mapper_ui()


def _mapper_row_add(target_id: str, parent: Any, height: int) -> None:
    """The per-row '+' add button at the end of a mapper row (e34s01).

    A NARROW borderless slot (MAPPER_ADD_SLOT_W, not the card pitch — the 2026-09-05
    rework: the '+' no longer wraps like a 150 px card on resize) as tall as the
    row, holding a small centered '+' button; a click opens the New-Mapping
    dialog with the row's source preselected, so the created mapping lands on
    this row.
    """
    with dpg.child_window(
        parent=parent,
        width=MAPPER_ADD_SLOT_W,
        height=height,
        border=False,
        no_scrollbar=True,
        tag=f"mapper_add_{target_id}",
    ):
        dpg.add_spacer(height=max(0, (height - MAPPER_ADD_H) // 2))
        dpg.add_button(
            label="+",
            width=MAPPER_ADD_W,
            height=MAPPER_ADD_H,
            callback=open_new_mapping_dialog,
            user_data=target_id,
            tag=f"mapper_add_btn_{target_id}",
        )
    with dpg.tooltip(parent=f"mapper_add_btn_{target_id}"):
        dpg.add_text("Add a mapper to this line")


def _mapper_marker_slot(mapping: dict[str, Any], parent: Any) -> None:
    """One card's markers in the under-card strip line (e34s03, answer D).

    The enable/reset/value M markers of one mapper card, centred on the card
    slot (MAPPER_MINI_W wide) so the strip rows stay aligned under the cards;
    the markers render OUTSIDE the bordered card (the card never hosts them).
    e34s05: a cue-list card also gets its dedicated window-action marker
    (mapper_mk_cue_open_<id> — the e33 rule). Captures and tags are unchanged
    so the learn capture path and existing bindings are untouched.
    """
    mid = mapping["id"]
    has_cue_open = mapping["control"] == "cue list"
    marker_count = 4 if has_cue_open else 3
    cluster_w = marker_count * MAPPER_MARKER_W  # the M buttons + item gaps
    indent = max(0, (MAPPER_MINI_W - cluster_w) // 2)
    slot_tag = f"mapper_mk_slot_{mid}"
    dpg.add_group(horizontal=True, parent=parent, tag=slot_tag)
    dpg.add_spacer(width=indent, parent=slot_tag)
    learn_marker(
        MIDI_ACTION_MAPPER_ENABLE,
        {"mapping_id": mid},
        parent=slot_tag,
        tag=f"mapper_mk_enable_{mid}",
    )
    learn_marker(
        MIDI_ACTION_MAPPER_RESET,
        {"mapping_id": mid},
        parent=slot_tag,
        tag=f"mapper_mk_reset_{mid}",
    )
    learn_marker(
        MIDI_ACTION_MAPPER_MAPPING,  # e33s03: the control value (e18 semantics)
        {"mapping_id": mid},
        parent=slot_tag,
        tag=f"mapper_mk_value_{mid}",
    )
    if has_cue_open:  # e34s05: open this mapping's cue-list window
        learn_marker(
            MIDI_ACTION_MAPPER_CUE_OPEN,
            {"mapping_id": mid},
            parent=slot_tag,
            tag=f"mapper_mk_cue_open_{mid}",
        )
    dpg.add_spacer(width=max(0, MAPPER_MINI_W - indent - cluster_w), parent=slot_tag)


def _mapper_card_has_source(mapping: dict[str, Any]) -> bool:
    """True when a mapping is bound to a band/MIDI/leap source (its Inp row shows).

    The one source-of-truth the readout/row-height decisions share (e34s02).
    """
    return (
        mapping.get("band") is not None
        or mapping.get("midi") is not None
        or mapping.get("leap") is not None
    )


def _mapper_readout_line(
    label: str,
    mid: int,
    kind: str,
    from_value: float,
    to_value: float,
    width: int,
) -> None:
    """One from/to readout row: a short label + two drag boxes (e34s02).

    Shared by the legacy slider lines (label 'output:'/'input:', wide boxes)
    and the compact Out/Inp column beside the band control (label 'Out'/'Inp',
    MAPPER_BAND_DRAG_W boxes). Tags and handler wire-up stay the same for both
    so on_mapper_output/input and the card right-click registry keep working.
    """
    callback = on_mapper_output if kind == "out" else on_mapper_input
    with dpg.group(horizontal=True):
        themed_text(label, slot="text_dim", tag=f"mapper_{kind}_lbl_{mid}")
        dpg.add_drag_float(
            default_value=from_value,
            width=width,
            format="%.2f",
            speed=0.01,
            callback=callback,
            user_data=(mid, "from"),
            tag=f"mapper_{kind}_from_{mid}",
        )
        dpg.add_drag_float(
            default_value=to_value,
            width=width,
            format="%.2f",
            speed=0.01,
            callback=callback,
            user_data=(mid, "to"),
            tag=f"mapper_{kind}_to_{mid}",
        )
    _bind_mapper_font(f"mapper_{kind}_lbl_{mid}")
    _bind_mapper_font(f"mapper_{kind}_from_{mid}")
    _bind_mapper_font(f"mapper_{kind}_to_{mid}")


def _mapper_band_control(mapping: dict[str, Any], mid: int) -> None:
    """Compact control band of a knob/button/cue-list card (e34s02, e34s04).

    ONE row as tall as the 44 px control: the control on the LEFT, and on the
    right the readout — knob/button: the 'Out' line (always) with the 'Inp'
    line under it when a band/MIDI/leap source is bound; cue list: a
    'Cue list...' button that opens the mapping's cue-list window instead of
    the Out/Inp fields (e34s04, answer E). The readout no longer stacks BELOW
    the control, so the rows shrink; the trigger becomes a 44 px square
    showing the value; the caption row above keeps the property label.
    """
    control = mapping["control"]
    kind = mapper.control_tag_kind(control)
    out_from = mapping["output_from"]
    out_to = mapping["output_to"]
    with dpg.group(horizontal=True):
        if control == "knob":
            dpg.add_knob_float(
                min_value=out_from,
                max_value=out_to,
                default_value=mapping["value"],
                width=MAPPER_KNOB_H,
                callback=on_mapper_control,
                user_data=mid,
                tag=f"mapper_{kind}_{mid}",
            )
        else:  # button / cue list: a 44 px square trigger labelled with the value
            dpg.add_button(
                label=_trigger_label(mapping),
                width=MAPPER_KNOB_H,
                height=MAPPER_KNOB_H,
                callback=on_mapper_button,
                user_data=mid,
                tag=f"mapper_{kind}_{mid}",
            )
        _bind_mapper_font(f"mapper_{kind}_{mid}")
        if control in ("button", "cue list"):
            # UAT e35: the trigger shows its state — accent fill when ON/RUN
            _style_trigger_theme(f"mapper_{kind}_{mid}", _trigger_is_on(mapping))
        if control == "cue list":
            # e34s04/e35 UAT: the right readout column hosts the window opener
            # and, UNDER it, the 'N of Total' progress readout of the cue.
            with dpg.group():
                dpg.add_button(
                    label="Cue list...",
                    width=MAPPER_MINI_W - 8 - MAPPER_KNOB_H - 2,
                    callback=open_cue_list_window,
                    user_data=mid,
                    tag=f"mapper_cue_open_{mid}",
                )
                themed_text(
                    _cue_progress_label(mapping),
                    slot="text_dim",
                    tag=f"mapper_cue_prog_{mid}",
                )
            _bind_mapper_font(f"mapper_cue_open_{mid}")
            _bind_mapper_font(f"mapper_cue_prog_{mid}")
        else:
            with dpg.group():
                _mapper_readout_line("Out", mid, "out", out_from, out_to, MAPPER_BAND_DRAG_W)
                if _mapper_card_has_source(mapping):
                    in_from = mapping.get("input_from")
                    in_to = mapping.get("input_to")
                    _mapper_readout_line(
                        "Inp",
                        mid,
                        "in",
                        in_from if in_from is not None else 0.0,
                        in_to if in_to is not None else 1.0,
                        MAPPER_BAND_DRAG_W,
                    )


def _render_mapper_card(mapping: dict[str, Any], parent: Any, height: int) -> None:
    """One bordered mapping mini-card inside a source row (e22s01, e23s01).

    Anatomy (e34s02): the caption row — dim property label left, X delete
    button right (NO value text: the control shows the value) — then the
    control. e34s02 (answers A/B): slider cards keep the vertical anatomy —
    slider spanning the content width, then the 'output:' from/to line and
    (when a band/MIDI/leap source is bound) the 'input:' line BELOW. Knob and
    button cards collapse into ONE band as tall as the 44 px control: the
    control LEFT, the 'Out' (always) and 'Inp' (bound source only) readouts
    RIGHT (see _mapper_band_control). The card height is the row height
    (per-content, see _mapper_row_height). Tags are unchanged so the band/MIDI
    drive and delete keep working; the right-click source menu lives on the
    CARD (buttons cannot host DPG handler registries).
    """
    mid = mapping["id"]
    caption = _mapper_card_caption(mapping)
    out_from = mapping["output_from"]
    out_to = mapping["output_to"]
    content_w = MAPPER_MINI_W - 8  # 4 px card padding each side
    with dpg.child_window(
        parent=parent,
        width=MAPPER_MINI_W,
        height=height,
        border=True,
        no_scrollbar=True,
        tag=f"mapper_card_{mid}",
    ):
        with dpg.group(horizontal=True):
            themed_text(caption, slot="text_dim", tag=f"mapper_prop_{mid}")
            dpg.add_spacer(
                width=_mapper_caption_spacer(
                    caption, {}, has_reset=mapping["control"] != "cue list"
                )
            )
            # e27s01: the reset button sits LEFT of the enable checkbox — it
            # returns the control to its neutral default (mapper.reset_mapping_value).
            # UAT e35: a cue-list trigger has no value to reset (it RUNS the cue,
            # stopped by the window 'Stop') — no R on cue-list cards.
            if mapping["control"] != "cue list":
                dpg.add_button(
                    label="R",
                    width=MAPPER_RESET_W,
                    height=MAPPER_RESET_H,
                    callback=reset_mapping,
                    user_data=mid,
                    tag=f"mapper_reset_{mid}",
                )
            dpg.add_checkbox(
                default_value=mapping.get("enabled", False),
                callback=on_mapper_enable,
                user_data=mid,
                tag=f"mapper_enable_{mid}",
            )
            dpg.add_button(
                label="X",
                width=MAPPER_X_W,
                height=MAPPER_X_H,
                callback=delete_mapping,
                user_data=mid,
                tag=f"mapper_del_{mid}",
            )
        # e34s03: the card markers moved OUT of the card — while MIDI Learn is
        # on they render in a dedicated strip line UNDER each card line (see
        # refresh_mapper_ui + _mapper_marker_slot); the card itself stays clean.
        _bind_mapper_font(f"mapper_prop_{mid}")
        if mapping["control"] == "slider":
            # e34s02 (answer A): the slider keeps the vertical anatomy — control
            # spanning the content width, then the legacy readout lines BELOW.
            dpg.add_slider_float(
                min_value=out_from,
                max_value=out_to,
                default_value=mapping["value"],
                width=content_w,
                callback=on_mapper_control,
                user_data=mid,
                tag=f"mapper_slider_{mid}",
            )
            _bind_mapper_font(f"mapper_slider_{mid}")
            _mapper_readout_line("output:", mid, "out", out_from, out_to, MAPPER_DRAG_W)
            if _mapper_card_has_source(mapping):
                in_from = mapping.get("input_from")
                in_to = mapping.get("input_to")
                _mapper_readout_line(
                    "input:",
                    mid,
                    "in",
                    in_from if in_from is not None else 0.0,
                    in_to if in_to is not None else 1.0,
                    MAPPER_DRAG_W,
                )
        else:
            # knob / button: the compact band (e34s02)
            _mapper_band_control(mapping, mid)
        _render_mapper_source_menu(mapping)


def _mapper_card_caption(mapping: dict[str, Any]) -> str:
    """Caption of one mapping card (e36s03).

    A multi-value property card shows the property + its component (Position X,
    Color R, Corner B.y…); a trigger-family card shows the action name (its
    value is meaningless); a cue-list card is titled by its TYPE — its stored
    property is inert (UAT e35). Everything else shows the catalog label
    (unchanged for the legacy scalar properties).
    """
    control = str(mapping.get("control") or "")
    if control == "cue list":
        return "cue list"
    entry = catalog.PROPERTY_CATALOG[str(mapping["property"])]
    if entry["family"] == catalog.FAMILY_TRIGGER:
        return str(mapping["property"]).upper()
    label = str(entry["label"])
    component = mapping.get("component")
    if component is not None:
        for comp in entry["components"]:
            if comp["key"] == component:
                return f"{label} {comp['label']}"
    return label


def _render_mapper_source_menu(mapping: dict[str, Any]) -> None:
    """Right-click source menu on a mini-card (e18, e23 bugfix).

    DearPyGui's dpg.popup() cannot attach to a BUTTON control (buttons cannot
    host the handler registry popup builds -> 1005 at refresh, which aborts the
    whole body and hides every card after it) and child windows reject its
    clicked handler (1000). So, like the Mediagrid tiles, the menu is a popup
    WINDOW shown by a per-card item-handler registry bound to every card
    child (all item types host an item-clicked registry on DPG 2.3.1):
    right-clicking anywhere on the bordered card opens the source menu. The
    ACTIVE source is marked with a checkmark; band and MIDI sources are
    mutually exclusive (see mapper.set_mapping_band). e33s03: while MIDI
    Learn is on the menu rows become uniform label + learn-marker rows (the
    marker captures mapper_band); the Leap picker row and Clear source are
    never marker targets (picker/destructive exclusion). The e18 'MIDI
    Learn...' modal item is gone — the card control marker captures the
    value binding instead; 2026-09-06 the slot returns as a MODE-arm item
    (_add_context_learn_item) so learning starts without opening the MIDI
    window.
    """
    mid = mapping["id"]
    # the control tag comes from the shared control->kind map (e34s04):
    # mapper_slider_N / mapper_knob_N / mapper_btn_N / mapper_cue_N — the map
    # keeps the e23 'btn' quirk in ONE place (building it from the control
    # name would look for the nonexistent mapper_button_N and abort).
    control_kind = mapper.control_tag_kind(mapping["control"])
    menu_tag = f"mapper_menu_{mid}"
    reg_tag = f"mapper_menu_reg_{mid}"
    for stale in (menu_tag, reg_tag):
        if dpg.does_item_exist(stale):
            dpg.delete_item(stale)
    with dpg.window(popup=True, show=False, no_title_bar=True, autosize=True, tag=menu_tag):
        if state.midi_learn_mode:
            # 2026-09-06 (user): the menu that armed the mode can cancel it
            with dpg.group(horizontal=True):
                dpg.add_button(label="Cancel MIDI Learn", callback=toggle_midi_learn)
            dpg.add_separator()
            # e33s03: uniform label + learn-marker rows while learning — the
            # marker captures the band action, the label keeps its click behavior
            for band_id in (2, 3):
                with dpg.group(horizontal=True) as band_row:
                    dpg.add_button(
                        label=f"Map Band {band_id}",
                        callback=set_mapping_band,
                        user_data=(mid, band_id),
                    )
                    learn_marker(
                        MIDI_ACTION_MAPPER_BAND,
                        {"mapping_id": mid, "band": band_id},
                        parent=band_row,
                        tag=f"mapper_mk_band{band_id}_{mid}",
                    )
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Leap Motion...",
                    callback=open_mapper_leap_picker,
                    user_data=mid,
                )
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Clear source",
                    callback=clear_mapping_source,
                    user_data=mid,
                )
        else:
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
            # 2026-09-06 (user): the e18 slot returns as a MODE arm — capture
            # still happens on the uniform markers that appear once armed.
            _add_context_learn_item()
            dpg.add_menu_item(
                label="Leap Motion...",
                check=True,
                default_value=(mapping.get("leap") is not None),
                callback=open_mapper_leap_picker,
                user_data=mid,
            )
            dpg.add_separator()
            dpg.add_menu_item(label="Clear source", callback=clear_mapping_source, user_data=mid)
    with dpg.item_handler_registry(tag=reg_tag):
        dpg.add_item_clicked_handler(1, callback=lambda *_, m=mid: _show_mapper_menu(m))
    for tag in (
        f"mapper_prop_{mid}",
        f"mapper_reset_{mid}",
        f"mapper_enable_{mid}",
        f"mapper_del_{mid}",
        f"mapper_{control_kind}_{mid}",
        f"mapper_out_lbl_{mid}",
        f"mapper_out_from_{mid}",
        f"mapper_out_to_{mid}",
        f"mapper_in_lbl_{mid}",
        f"mapper_in_from_{mid}",
        f"mapper_in_to_{mid}",
    ):
        if dpg.does_item_exist(tag):
            dpg.bind_item_handler_registry(tag, reg_tag)


def _show_mapper_menu(mid: int) -> None:
    """Open the right-click source menu of a mini-card at the cursor (e18).

    e27s02 (BUG-2026-09-03T175000): the popup must be placed at
    get_mouse_pos(local=False) — DPG's stable per-frame ImGui CLIENT mouse
    position, the same coordinate space window positions (set_item_pos) live
    in. The default get_mouse_pos() is not usable here: DPG overwrites it
    while drawing with coordinates LOCAL TO the focused window/child under the
    cursor (over a card that is card-local, e.g. (37, 14)), so subtracting the
    viewport position from it opened the menu at the top of the viewport
    instead of at the cursor.
    """
    menu_tag = f"mapper_menu_{mid}"
    if not dpg.does_item_exist(menu_tag):
        return
    dpg.set_item_pos(menu_tag, dpg.get_mouse_pos(local=False))
    dpg.show_item(menu_tag)


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
    """Move a mapping's control widget from an external source (band/MIDI, e18).

    e23s01: the value caption is gone — the control's own readout shows the
    output value, so only the widget values are set here.
    """
    for kind in ("slider", "knob"):
        tag = f"mapper_{kind}_{mapping_id}"
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, value)


def drive_mapper_band(band_id: int, level: float) -> None:
    """Push an audio-band level into every control mapped to that band (e18).

    Called by refresh_band_value (main thread, ~30 fps while the band is
    enabled). e23s02: the raw level (0..1) is remapped through each mapping's
    input range (default 0..1), then through its output range.
    """
    for m in state.mapper_mappings:
        if m.get("band") == band_id:
            value = mapper.apply_input_value(m["id"], level)
            _set_mapper_control_value(m["id"], value)


def drive_leap_mappings(snapshot: dict[str, float], now: float | None = None) -> None:
    """Push a fresh leap snapshot into every leap-bound mapping (e26s03).

    Called by the leap worker listener on tracking frames. Per-mapping rate
    cap (~30 Hz) and the change epsilon (input span / 1000) come from the
    pure drive_ready gate; an absent hand/key HOLDS the mapping's last value.
    OSC sends happen inside mapper.apply_input_value (worker-safe, HIGH-1) and
    the card widget moves on the main thread via ui_task.
    """
    if now is None:
        now = time.monotonic()
    snapshot = snapshot or {}
    for m in list(state.mapper_mappings):
        key = m.get("leap")
        if not key:
            continue
        raw = snapshot.get(key)
        if raw is None:
            continue  # absent hand: hold the mapping's last value
        in_from, in_to = m.get("input_from"), m.get("input_to")
        if in_from is None or in_to is None:
            parts = leap.binding_parts(key)
            if parts is not None:
                in_from, in_to = leap.signal_default_range(parts[1]) or (0.0, 1.0)
            else:
                in_from, in_to = 0.0, 1.0
        book = state.leap_drive_state.setdefault(m["id"], {"last_raw": None, "last_push": 0.0})
        if not leap.drive_ready(
            now,
            float(book["last_push"]),
            leap.LEAP_DRIVE_INTERVAL,
            book["last_raw"],
            float(raw),
            float(in_from),
            float(in_to),
        ):
            continue
        book["last_raw"] = float(raw)
        book["last_push"] = now
        value = mapper.apply_input_value(m["id"], float(raw))

        def _move_widget(mid: int = m["id"], v: float = value) -> None:
            _set_mapper_control_value(mid, v)

        ui_task(_move_widget)


def open_mapper_leap_picker(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Mapper card menu > Leap Motion...: hand/signal picker modal (e26s03).

    Unlike MIDI there is nothing to LEARN — the signal IS the address, so the
    user picks a hand (Left/Right) and a curated signal from the catalog. The
    picker is a small modal (same shape the e18 MIDI learn modal had before
    e33s03 replaced it with the uniform control marker).
    """
    mapping_id = int(user_data)
    if dpg.does_item_exist("mapper_leap_window"):
        dpg.delete_item("mapper_leap_window")
    mapping = mapper.find_mapping(mapping_id)
    signal_items = [leap.leap_field(f)["label"] for f in leap.bindable_signals()]
    hand_default = "Left"
    signal_default = signal_items[0]
    if mapping is not None:
        parts = leap.binding_parts(mapping.get("leap") or "")
        if parts is not None:
            hand_default = "Left" if parts[0] == "left" else "Right"
            label = leap.leap_field(parts[1])["label"]
            if label in signal_items:
                signal_default = label
    with dpg.window(
        label="Leap Motion",
        tag="mapper_leap_window",
        modal=True,
        width=340,
        height=200,
        no_resize=True,
    ):
        themed_text("Bind this control to a hand signal:", slot="text")
        dpg.add_spacer(height=6)
        with dpg.group(horizontal=True):
            themed_text("Hand", slot="text_dim")
            dpg.add_combo(
                items=["Left", "Right"],
                default_value=hand_default,
                width=110,
                tag="mapper_leap_hand_cb",
            )
        dpg.add_spacer(height=4)
        with dpg.group(horizontal=True):
            themed_text("Signal", slot="text_dim")
            dpg.add_combo(
                items=signal_items,
                default_value=signal_default,
                width=210,
                tag="mapper_leap_signal_cb",
            )
        dpg.add_spacer(height=10)
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="Bind", callback=mapper_leap_confirm, user_data=mapping_id, width=90
            )
            dpg.add_button(label="Cancel", callback=mapper_leap_cancel, width=90)
    dpg.show_item("mapper_leap_window")


def mapper_leap_confirm(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Bind the picked hand/signal to the mapping, close the modal, refresh (e26s03)."""
    mapping_id = int(user_data)
    hand = str(dpg.get_value("mapper_leap_hand_cb")).lower()
    label = str(dpg.get_value("mapper_leap_signal_cb"))
    field = next((f for f in leap.bindable_signals() if leap.leap_field(f)["label"] == label), None)
    if field is None or hand not in leap.LEAP_HANDS:
        return
    mapper.set_mapping_leap(mapping_id, leap.binding_key(hand, field))
    if dpg.does_item_exist("mapper_leap_window"):
        dpg.delete_item("mapper_leap_window")
    refresh_mapper_ui()


def mapper_leap_cancel(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Close the leap picker modal without binding (e26s03)."""
    if dpg.does_item_exist("mapper_leap_window"):
        dpg.delete_item("mapper_leap_window")


def midi_mapping_value(mapping_id: int, midi_value: int) -> None:
    """Drive a mapper control from a learned MIDI value (e23s02).

    The raw 0..127 value is remapped through the mapping's input range
    (default 0..127), then through its output range, and the control widget
    follows. Cue list (e35s03): the value marker IS the trigger — a value >= 64
    runs/restarts the cue, anything below is ignored.
    """
    mapping = mapper.find_mapping(mapping_id)
    if mapping is not None and mapping.get("control") == "cue list":
        if midi_value >= 64:
            cue.cue_start(mapping_id, allow_restart=True)
            tick_cue_triggers()
        return
    value = mapper.apply_input_value(mapping_id, midi_value)
    _set_mapper_control_value(mapping_id, value)
    if mapping is not None and mapping.get("control") == "button":
        # BUG-2026-09-07: the square trigger must follow the driven value too
        _sync_mapper_button_state(mapping)


def tick_midi_learn_timeout() -> None:
    """Expire a stale MIDI Learn session (marker captures included) past the timeout."""
    if state.midi_learn_mode and (
        time.time() - state.midi_learn_started_at > MIDI_LEARN_TIMEOUT_SECONDS
    ):
        _exit_midi_learn()


def _mapper_row_lead_px() -> int:
    """Width of the row lead consumed before the first card of a mapper line.

    The lead is the line-number column + its spacing + the thumbnail slot; while
    MIDI Learn is on it also includes the e33s03 row line-marker column. The
    trailing group spacing is excluded (the horizontal line adds it), matching
    the e32s02 continuation spacer that aligns cards across wrapped lines.
    """
    lead = MAPPER_LINE_NO_W + 4 + MAPPER_ROW_THUMB_W
    if state.midi_learn_mode:  # e33s03: the row line-marker column
        lead += 4 + MAPPER_MARKER_W
    return lead


def _mapper_cards_per_line() -> int:
    """How many mapping mini-cards fit one source line at the live window width.

    e24s02: the Mapper wraps instead of overflowing — the capacity derives from
    the current mapper window width minus the row lead (the e32s02 line-number
    column + the 110 px thumbnail block + their item spacings, and the e33s03
    line-marker column while learn mode is on), over the card pitch
    (MAPPER_MINI_W + the 4 px horizontal item spacing).
    """
    width = dpg.get_item_width("mapper_window")
    if not width:
        width = MAPPER_WINDOW_WIDTH
    available = width - 8  # window content padding
    pitch = MAPPER_MINI_W + 4
    lead = _mapper_row_lead_px() + 4  # row lead + trailing group spacing
    return max(1, int((available - lead) // pitch))


# --- ROUTES IN THE MAPPER (e40s01) -------------------------------------------
# Each source is one line with a Control band (the input-mapping cards) and a Get
# band (the State Routes reading that source); source-less Routes (Clock/
# Constant) live in a Global line on top. Three chips hide a band/line.

_mapper_show_control = True
_mapper_show_get = True
_mapper_show_global = True


def on_mapper_filter(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Section chips: apply the visibility flags and rebuild the body (e40s01)."""
    global _mapper_show_control, _mapper_show_get, _mapper_show_global
    _mapper_show_control = bool(dpg.get_value("mapper_filter_control"))
    _mapper_show_get = bool(dpg.get_value("mapper_filter_get"))
    _mapper_show_global = bool(dpg.get_value("mapper_filter_global"))
    refresh_mapper_ui()


def _route_destination_label(route: dict[str, Any]) -> str:
    """Short Destination summary of a Route row readout (e40s01)."""
    destination = mapper.destination_of(route)
    spec = route.get("destination_spec") or {}
    if destination == DEST_MIDI:
        kind = "note" if str(spec.get("type")) == MIDI_KIND_NOTE else "cc"
        return f"MIDI {spec.get('controller_port', '?')} {kind} {spec.get('number', 0)}"
    if destination == DEST_OSC:
        return f"OSC {spec.get('host', '?')}:{spec.get('port', '?')} {spec.get('address', '?')}"
    return "Vimix"


def _route_origin_label(route: dict[str, Any]) -> str:
    """Short Origin summary of a Route row readout (e40s01)."""
    origin = mapper.origin_of(route)
    if origin == ORIGIN_STATE:
        return f"{route.get('target_id') or '?'}.{route['property']}"
    if origin == ORIGIN_CLOCK:
        return f"clock {route.get('origin_spec', {}).get('clock', 'beat')}"
    if origin == ORIGIN_CONST:
        return f"const {route.get('origin_spec', {}).get('value', 0.0)}"
    return str(route["property"])


def _render_route_row(route: dict[str, Any], parent: Any) -> None:
    """One Get/Global row: Origin -> Destination + arm/edit/delete + learn marker."""
    rid = int(route["id"])
    tag = f"route_row_{rid}"
    in_from = float(route.get("input_from") or 0.0)
    in_to = float(route.get("input_to") or 0.0)
    out_from = float(route["output_from"])
    out_to = float(route["output_to"])
    cadence = f" {int(route['cadence'])}ms" if route.get("cadence") else ""
    with dpg.group(horizontal=True, parent=parent, tag=tag):
        themed_text(
            f"{_route_origin_label(route)} -> {_route_destination_label(route)}",
            slot="text_dim",
        )
        themed_text(
            f"In {in_from:.2f}..{in_to:.2f} Out {out_from:.0f}..{out_to:.0f}{cadence}",
            slot="text_dim",
        )
        dpg.add_checkbox(
            tag=f"route_enable_{rid}",
            default_value=bool(route.get("enabled", False)),
            callback=on_route_enable,
            user_data=rid,
        )
        dpg.add_button(label="Edit", callback=open_route_editor, user_data=rid)
        dpg.add_button(label="X", callback=delete_route, user_data=rid)
        if state.midi_learn_mode:  # e33 rule: the arm toggle is MIDI-mappable
            learn_marker(
                MIDI_ACTION_ROUTE_TOGGLE,
                {"route_id": rid},
                parent=tag,
                tag=f"route_mk_{rid}",
            )


def on_route_enable(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Route arm checkbox: flip the Enabled gate (no body refresh, e40s01)."""
    mapper.set_mapping_enabled(int(user_data), bool(app_data))


def delete_route(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Delete a Route, drop its runtime memory and rebuild the body (e40s01)."""
    rid = int(user_data)
    state.mapper_mappings[:] = [m for m in state.mapper_mappings if int(m["id"]) != rid]
    live_ids = {int(m["id"]) for m in state.mapper_mappings}
    routeengine.prune_book(state.route_book, state.route_values, live_ids)
    refresh_mapper_ui()


def new_route_dialog(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Create a State -> MIDI Route on the first source and open its editor (e40s01)."""
    targets = list(mapper.row_targets())
    if not targets:
        log_error("Mapper", "no source to route from - right-click one in Vimix sources first")
        return
    ports = [str(c.get("port", "")) for c in midi_controllers if c.get("port")]
    route = mapper.add_route(
        ORIGIN_STATE,
        DEST_MIDI,
        target_id=targets[0],
        prop="seek",
        destination_spec={
            "controller_port": ports[0] if ports else "",
            "channel": 0,
            "type": MIDI_KIND_CC,
            "number": 0,
        },
    )
    _open_route_editor(int(route["id"]))


def open_route_editor(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """'Edit' on a Route row: open the editor modal (e40s01)."""
    _open_route_editor(int(user_data))


def _open_route_editor(route_id: int) -> None:
    """Edit one Route: Origin (source/property/cadence) + Destination + steps."""
    route = mapper.find_mapping(route_id)
    if route is None:
        return
    if dpg.does_item_exist("mapper_route_window"):
        dpg.delete_item("mapper_route_window")
    spec = route.get("destination_spec") or {}
    ports = [str(c.get("port", "")) for c in midi_controllers if c.get("port")]
    with dpg.window(
        label="Edit Route",
        tag="mapper_route_window",
        modal=True,
        width=430,
        height=380,
        no_resize=True,
    ):
        themed_text("Source", slot="text_dim")
        dpg.add_combo(
            items=list(mapper.row_targets()),
            default_value=str(route.get("target_id") or ""),
            width=280,
            tag="route_source_combo",
        )
        themed_text("Property", slot="text_dim")
        dpg.add_combo(
            items=mapper.mappable_properties(),
            default_value=str(route["property"]),
            width=280,
            tag="route_property_combo",
        )
        themed_text("Cadence (ms)", slot="text_dim")
        dpg.add_drag_int(
            default_value=int(route.get("cadence") or ROUTE_DEFAULT_CADENCE_MS),
            min_value=ROUTE_MIN_CADENCE_MS,
            max_value=60_000,
            width=160,
            tag="route_cadence",
        )
        themed_text("Controller", slot="text_dim")
        dpg.add_combo(
            items=ports,
            default_value=str(spec.get("controller_port") or (ports[0] if ports else "")),
            width=280,
            tag="route_controller_combo",
        )
        with dpg.group(horizontal=True):
            themed_text("Channel", slot="text_dim")
            dpg.add_drag_int(
                default_value=int(spec.get("channel", 0)),
                min_value=0,
                max_value=15,
                width=70,
                tag="route_channel",
            )
            themed_text("Type", slot="text_dim")
            dpg.add_combo(
                items=[MIDI_KIND_CC, MIDI_KIND_NOTE],
                default_value=str(spec.get("type") or MIDI_KIND_CC),
                width=90,
                tag="route_type_combo",
            )
            themed_text("Number", slot="text_dim")
            dpg.add_drag_int(
                default_value=int(spec.get("number", 0)),
                min_value=0,
                max_value=127,
                width=70,
                tag="route_number",
            )
        themed_text("Steps (1 = continuous)", slot="text_dim")
        dpg.add_drag_int(
            default_value=int(route.get("steps", ROUTE_STEPS_CONTINUOUS)),
            min_value=1,
            max_value=64,
            width=160,
            tag="route_steps",
        )
        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_button(label="OK", width=120, callback=route_editor_confirm, user_data=route_id)
            dpg.add_button(
                label="Cancel",
                width=120,
                callback=lambda s, a: dpg.delete_item("mapper_route_window"),
            )
    dpg.show_item("mapper_route_window")


def route_editor_confirm(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Store the edited Route fields, reseeding the Origin window, and rebuild (e40s01)."""
    route = mapper.find_mapping(int(user_data))
    if route is None:
        return
    prop = str(dpg.get_value("route_property_combo"))
    entry = catalog.PROPERTY_CATALOG.get(prop)
    if entry is not None:
        route["property"] = prop
        if len(entry["components"]) > 1:
            route["component"] = entry["components"][0]["key"]
        else:
            route["component"] = None
        if mapper.origin_of(route) == ORIGIN_STATE:
            comp = entry["components"][0]
            mapper.set_mapping_input(route["id"], float(comp["min"]), float(comp["max"]))
    route["target_id"] = str(dpg.get_value("route_source_combo")) or None
    route["cadence"] = max(ROUTE_MIN_CADENCE_MS, int(dpg.get_value("route_cadence")))
    route["steps"] = max(ROUTE_STEPS_CONTINUOUS, int(dpg.get_value("route_steps")))
    route["destination_spec"] = {
        "controller_port": str(dpg.get_value("route_controller_combo")),
        "channel": int(dpg.get_value("route_channel")),
        "type": str(dpg.get_value("route_type_combo")),
        "number": int(dpg.get_value("route_number")),
    }
    if dpg.does_item_exist("mapper_route_window"):
        dpg.delete_item("mapper_route_window")
    refresh_mapper_ui()


def refresh_mapper_ui() -> None:
    """Rebuild the Mapper window body from state.mapper_mappings (main thread).

    e22s01/e24s02: the body is a vertical stack of SOURCE BLOCKS. Rows derive
    from the flat mapping list at every rebuild — one block per distinct
    target_id in first-appearance order, mappings inside in list (creation)
    order. Each block wraps its mappings onto as many aligned LINES as the
    live window width fits: the source thumbnail slot leads the FIRST line
    only, later lines start with an empty spacer the width of the thumbnail
    slot so the cards align under the first line's cards. A source whose last
    mapping was deleted produces no block, so it disappears on the redraw;
    a window resize re-runs this rebuild (see the mapper_resize registry).
    """
    if not dpg.does_item_exist("mapper_mappings_group"):
        return
    dpg.delete_item("mapper_mappings_group", children_only=True)
    if not state.mapper_mappings:
        # explicit parent: at runtime (menu callback) DPG cannot deduce the
        # implicit container, so a parentless add_text would raise 1011
        themed_text(
            "No mappings yet — right-click a source in Vimix sources.",
            slot="text_dim",
            wrap=MAPPER_WINDOW_WIDTH - 40,
            parent="mapper_mappings_group",
        )
        return
    # e40s01: split the flat list into Control Origins (the card bands, grouped
    # by source) and the other Routes (the Get bands + the source-less Global
    # line). Row order stays mapper.row_targets() (e31s01).
    targets = list(mapper.row_targets())
    rows: dict[str, list[dict[str, Any]]] = {target: [] for target in targets}
    route_rows: dict[str, list[dict[str, Any]]] = {target: [] for target in targets}
    global_routes: list[dict[str, Any]] = []
    for mapping in state.mapper_mappings:
        if mapper.origin_of(mapping) == ORIGIN_CONTROL:
            target = mapping["target_id"]
            if target in rows:
                rows[target].append(mapping)
        elif str(mapping.get("target_id") or "") in route_rows:
            route_rows[str(mapping["target_id"])].append(mapping)
        else:
            global_routes.append(mapping)
    if _mapper_show_global and global_routes:
        global_block = dpg.add_group(parent="mapper_mappings_group", tag="mapper_global_block")
        themed_text("Global", slot="text_dim", parent=global_block)
        for route in global_routes:
            _render_route_row(route, parent=global_block)
    per_line = _mapper_cards_per_line()
    width = dpg.get_item_width("mapper_window") or MAPPER_WINDOW_WIDTH
    card_area = width - 8 - (_mapper_row_lead_px() + 4)
    pitch = MAPPER_MINI_W + 4
    for row_no, (target_id, mappings) in enumerate(rows.items(), start=1):
        routes = route_rows.get(target_id, [])
        if not (_mapper_show_control and mappings) and not (_mapper_show_get and routes):
            continue
        block = dpg.add_group(parent="mapper_mappings_group")
        if mappings and _mapper_show_control:
            row_height = _mapper_row_height(mappings)
            # e34s01 (2026-09-05 rework): the per-row '+' is a SMALL trailing
            # button, not a card slot — it rides the row's last card line when the
            # pixel room fits it (mapper.add_fits_last_line) and only moves to its
            # own narrow line when a very narrow window leaves no room.
            add_inline = mapper.add_fits_last_line(
                len(mappings), per_line, card_area, pitch, MAPPER_ADD_SLOT_W
            )
            lines = mapper.row_slots(len(mappings), per_line)
            for line_no, slot_line in enumerate(lines):
                line = dpg.add_group(horizontal=True, parent=block)
                if line_no == 0:
                    # e32s02: the row number leads the line (1-based window order,
                    # matches the tile menu "Add to Mapper > line N")
                    _mapper_line_number(row_no, target_id, parent=line, height=row_height)
                    _mapper_row_thumb(target_id, parent=line, height=row_height)
                    if state.midi_learn_mode:  # e33s03: map this row's line-assign action
                        learn_marker(
                            MIDI_ACTION_MAPPER_LINE,
                            {"line": row_no - 1},
                            parent=line,
                            tag=f"mapper_mk_line_{target_id}",
                        )
                else:
                    # alignment slot: continuation lines start where the cards of
                    # the first line start (row lead, e32s02/e33s03)
                    dpg.add_spacer(
                        width=_mapper_row_lead_px(),
                        parent=line,
                    )
                for slot in slot_line:
                    if slot < len(mappings):
                        _render_mapper_card(mappings[slot], parent=line, height=row_height)
                # e34s01: the small '+' rides the last card line when it fits
                if add_inline and line_no == len(lines) - 1:
                    _mapper_row_add(target_id, parent=line, height=row_height)
                # e34s03 (answer D): while learn mode is on every card LINE gets a
                # marker strip line directly UNDER it — the cards' markers live
                # outside the bordered cards, aligned under each card slot. The
                # add-only slot lines carry no markers.
                if state.midi_learn_mode and any(slot < len(mappings) for slot in slot_line):
                    strip = dpg.add_group(
                        horizontal=True,
                        parent=block,
                        tag=f"mapper_mk_strip_{row_no}_{line_no}",
                    )
                    dpg.add_spacer(width=_mapper_row_lead_px(), parent=strip)
                    for slot in slot_line:
                        if slot < len(mappings):
                            _mapper_marker_slot(mappings[slot], parent=strip)
            if not add_inline:
                # a very narrow window: the '+' gets its own narrow line under the
                # cards (aligned with them) instead of overflowing the edge
                line = dpg.add_group(horizontal=True, parent=block)
                dpg.add_spacer(width=_mapper_row_lead_px(), parent=line)
                _mapper_row_add(target_id, parent=line, height=row_height)
        else:
            # a Get-only source keeps its line lead (number + thumbnail) without cards
            lead_height = MAPPER_CTRL_H
            line = dpg.add_group(horizontal=True, parent=block)
            _mapper_line_number(row_no, target_id, parent=line, height=lead_height)
            _mapper_row_thumb(target_id, parent=line, height=lead_height)
            if state.midi_learn_mode:
                learn_marker(
                    MIDI_ACTION_MAPPER_LINE,
                    {"line": row_no - 1},
                    parent=line,
                    tag=f"mapper_mk_line_{target_id}",
                )
        if _mapper_show_get:
            for route in routes:
                _render_route_row(route, parent=block)
    # e35s03/UAT: rebuilt cards relabel their running triggers and progress
    # readouts (the caches are stale after the body rebuild — re-seed in one pass)
    state.cue_trigger_label_cache.clear()
    state.cue_progress_cache.clear()
    tick_cue_triggers()


def show_mapper_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Windows menu: open the Mapper window with a fresh mappings body (e16)."""
    refresh_mapper_ui()
    dpg.show_item("mapper_window")
    dpg.focus_item("mapper_window")  # e17: a shown window must come to the front


def on_mapper_enable(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Tick the enable checkbox: arm/mute the mapping (no body refresh, e24)."""
    mapper.set_mapping_enabled(int(user_data), bool(app_data))


def on_mapper_control(sender: Any, app_data: Any, user_data: Any) -> None:
    """Slider/knob change: send the clamped OUTPUT value (e23s01)."""
    mid = int(user_data)
    mapper.send_mapping_value(mid, float(app_data))


def _sync_mapper_button_state(mapping: dict[str, Any]) -> None:
    """Restyle a square trigger (button) from the mapping's current value.

    BUG-2026-09-07T154112: the mouse click AND the MIDI value drive are two
    drivers of the same control — both must refresh label + theme, or a
    MIDI-mapped button stays visually OFF while state and OSC change.
    """
    kind = mapper.control_tag_kind(mapping["control"])
    tag = f"mapper_{kind}_{mapping['id']}"
    if not dpg.does_item_exist(tag):
        return
    dpg.configure_item(tag, label=_trigger_label(mapping))
    _style_trigger_theme(tag, _trigger_is_on(mapping))


def on_mapper_button(sender: Any, app_data: Any, user_data: Any) -> None:
    """Button/cue-list press.

    Button: toggle output_from (OFF) / output_to (ON), send OSC, refresh the
    label. Cue list (e35s03/UAT): the trigger RUNS the mapping's cue — a press
    while the cue runs RESTARTS it, an empty/disabled cue is an engine no-op;
    the trigger reads ON while the cue runs and OFF when it finishes.
    """
    mid = int(user_data)
    mapping = mapper.find_mapping(mid)
    if mapping is None:
        return
    if mapping["control"] == "cue list":
        cue.cue_start(mid, allow_restart=True)
        tick_cue_triggers()
        return
    mapper.send_button_mapping(mid)
    mapping = mapper.find_mapping(mid)
    if mapping is None:
        return
    _sync_mapper_button_state(mapping)


def _trigger_is_on(mapping: dict[str, Any]) -> bool:
    """A button-like trigger is visually ON when its value sits at the output_to end."""
    return float(mapping["value"]) == float(mapping["output_to"])


def _trigger_label(mapping: dict[str, Any]) -> str:
    """Square trigger text: a mapper button and a cue-list trigger read ON/OFF
    (UAT e35): the cue square is a momentary RUN trigger — ON while the cue is
    executing, OFF again when it finishes. No raw value ever shows inside.
    e36s03: a TRIGGER-family button (replay/reset/reload/flag) shows its action
    name instead — its value has no ON/OFF meaning and every press fires."""
    if mapping["control"] == "button":
        if catalog.family_of(str(mapping["property"])) == catalog.FAMILY_TRIGGER:
            return str(mapping["property"]).upper()
        return "ON" if _trigger_is_on(mapping) else "OFF"
    return "ON" if cue.cue_is_running(int(mapping["id"])) else "OFF"


def _trigger_theme_signature() -> tuple[tuple[int, ...], ...]:
    """The palette colors the trigger themes depend on (rebuild trigger)."""
    pal = state.active_palette
    return tuple(tuple(pal[slot]) for slot in ("play_on_bg", "badge_bg", "text_bright", "text_dim"))


def _ensure_trigger_themes() -> None:
    """Create (or rebuild on palette change) the two SHARED trigger themes.

    e35 UAT (errors 1000/1005): per-trigger themes recreated on every refresh
    collided — DPG aliases must be unique. Two root themes (OFF index 0, ON
    index 1) are bound by many triggers; they are deleted and rebuilt only when
    the palette colors change.
    """
    signature = _trigger_theme_signature()
    tags = state.trigger_theme_tags
    if signature == state.trigger_theme_signature and all(
        t is not None and dpg.does_item_exist(t) for t in tags
    ):
        return
    for old in tags:
        if old is not None and dpg.does_item_exist(old):
            dpg.delete_item(old)
    rebuilt: list[str | None] = []
    for on in (False, True):
        if on:
            bg = state.active_palette["play_on_bg"]
            label_color = state.active_palette["text_bright"]
        else:
            bg = state.active_palette["badge_bg"]
            label_color = state.active_palette["text_dim"]
        theme_tag = f"th_trig_{'on' if on else 'off'}"
        with dpg.theme(tag=theme_tag), dpg.theme_component(dpg.mvThemeCat_Core):
            dpg.add_theme_color(dpg.mvThemeCol_Button, palette_rgba(bg))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, palette_rgba(bg))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, palette_rgba(bg))
            dpg.add_theme_color(dpg.mvThemeCol_Text, palette_rgba(label_color))
        rebuilt.append(theme_tag)
    state.trigger_theme_tags = rebuilt
    state.trigger_theme_signature = signature


def _style_trigger_theme(tag: str, on: bool) -> None:
    """Point a square trigger at the shared ON/OFF theme (UAT e35)."""
    if not dpg.does_item_exist(tag):
        return
    _ensure_trigger_themes()
    theme_tag = state.trigger_theme_tags[1 if on else 0]
    if theme_tag is not None:
        dpg.bind_item_theme(tag, theme_tag)


def _cue_progress_label(mapping: dict[str, Any]) -> str:
    """The card readout under 'Cue list...': '3 of 10' — executed actions over
    the cue total (waits are pacing, not actions). Empty cue -> blank (UAT e35)."""
    executed, total = cue.cue_progress(int(mapping["id"]))
    if total <= 0:
        return ""
    return f"{executed} of {total}"


def tick_cue_triggers() -> None:
    """Refresh every cue-list card trigger + progress readout (main thread).

    e35s03/UAT: cheap per-frame call — it only relabels triggers whose label
    left the cache (run started/finished), updates the 'N of Total' progress
    text while actions dispatch, or after a Mapper rebuild cleared the caches.
    Idle frames cost one dict loop over the cue-list mappings.
    """
    changed_labels: dict[int, str] = {}
    changed_progress: dict[int, str] = {}
    for mapping in state.mapper_mappings:
        if mapping.get("control") != "cue list":
            continue
        mid = int(mapping["id"])
        label = _trigger_label(mapping)
        if state.cue_trigger_label_cache.get(mid) != label:
            changed_labels[mid] = label
        progress = _cue_progress_label(mapping)
        if state.cue_progress_cache.get(mid) != progress:
            changed_progress[mid] = progress
    for mid, label in changed_labels.items():
        tag = f"mapper_cue_{mid}"
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, label=label)
            _style_trigger_theme(tag, label == "ON")
    for mid, progress in changed_progress.items():
        tag = f"mapper_cue_prog_{mid}"
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, progress)
    state.cue_trigger_label_cache.update(changed_labels)
    state.cue_progress_cache.update(changed_progress)


def cue_tick_loop() -> None:
    """e35s03: drive the cue engine on the monotonic clock (fade_tick_loop pattern).

    The thread only works while a cue is running; UI reflection happens on the
    main thread (tick_cue_triggers), never here (HIGH-1).
    """
    while True:
        if state.cue_runs:
            cue.tick(time.monotonic() * 1000.0)
        time.sleep(0.01)  # 100 FPS cap, same cadence as the fade loop


def _sync_mapper_control(mid: int) -> None:
    """Reconfigure a mapping's control widget to its current output range (e23s01).

    Called after an output-box edit so the control min/max follow the new
    (possibly reversed) output range and the stored value stays inside it.
    """
    mapping = mapper.find_mapping(mid)
    if mapping is None:
        return
    kind = mapper.control_tag_kind(mapping["control"])  # e34s04: slider/knob/btn/cue
    tag = f"mapper_{kind}_{mid}"
    if not dpg.does_item_exist(tag):
        return
    out_from = mapping["output_from"]
    out_to = mapping["output_to"]
    if kind in ("slider", "knob"):
        dpg.configure_item(tag, min_value=out_from, max_value=out_to)
        dpg.set_value(tag, mapping["value"])
    else:  # button-like trigger: ON/OFF (button) or the value label (cue list)
        dpg.configure_item(tag, label=_trigger_label(mapping))
        _style_trigger_theme(tag, _trigger_is_on(mapping))


def on_mapper_output(sender: Any, app_data: Any, user_data: Any) -> None:
    """Drag an output from/to box: store the new output range and re-fit the control.

    Reads BOTH boxes (the edited one via the sender, the other via get_value)
    so the pair is always consistent; the control is reconfigured in place —
    no full body rebuild (a refresh mid-drag would kill the drag).
    """
    mid, _edge = user_data
    from_tag = f"mapper_out_from_{mid}"
    to_tag = f"mapper_out_to_{mid}"
    out_from = float(dpg.get_value(from_tag))
    out_to = float(dpg.get_value(to_tag))
    mapper.set_mapping_output(mid, out_from, out_to)
    _sync_mapper_control(mid)


def on_mapper_input(sender: Any, app_data: Any, user_data: Any) -> None:
    """Drag an input from/to box: store the new input range of the bound source.

    Reads BOTH boxes (edited via the sender, the other via get_value) so the
    pair is consistent; no body rebuild (a refresh mid-drag would kill it).
    """
    mid, _edge = user_data
    from_tag = f"mapper_in_from_{mid}"
    to_tag = f"mapper_in_to_{mid}"
    mapper.set_mapping_input(mid, float(dpg.get_value(from_tag)), float(dpg.get_value(to_tag)))


def reset_mapping(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """R on a mapping card: reset the control to its neutral default (e27s01).

    The core stores the neutral (catalog midpoint for slider/knob, the OFF
    end for buttons), clamps it into the mapping's output interval and sends
    it through the e24 gate; the widget moves in place via _sync_mapper_control
    — no body rebuild, so a live band/MIDI drive is never interrupted.
    """
    mid = int(user_data)
    mapping = mapper.find_mapping(mid)
    if mapping is not None and mapping.get("control") == "cue list":
        # e35s03: reset STOPS a running cue — no OSC, no value toggle (the cue
        # owns the trigger now, not the single property value)
        cue.cue_stop(mid)
        tick_cue_triggers()
        return
    mapper.reset_mapping_value(mid)
    _sync_mapper_control(mid)


def delete_mapping(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """X on a mapping card: remove the mapping, stop its cue, close its editor."""
    mid = int(user_data)
    mapper.remove_mapping(mid)
    _drop_mapping_editor_and_runs(mid)
    refresh_mapper_ui()


def open_new_mapping_dialog(
    sender: Any = None, app_data: Any = None, user_data: Any = None
) -> None:
    """Mediagrid tile right-click > Add to Mapper: modal asking TYPE first (e35s04).

    The type is the mapper control; the property only matters for the value
    controls (slider/knob/button), so it is hidden for 'cue list' — its
    trigger runs an editable OSC macro and no single property applies.
    """
    state.mapper_pending_target = str(user_data)
    if dpg.does_item_exist("mapper_new_dialog"):
        dpg.delete_item("mapper_new_dialog")
    with dpg.window(
        label="New Mapping",
        tag="mapper_new_dialog",
        modal=True,
        width=340,
        height=260,
        no_resize=True,
    ):
        themed_text(f"Source: {state.mapper_pending_target}", slot="text")
        dpg.add_separator()
        themed_text("Type", slot="text_dim")
        dpg.add_combo(
            items=list(mapper.MAPPER_CONTROLS),
            default_value="slider",
            width=260,
            tag="mapper_control_combo",
            callback=on_mapper_control_type_change,
        )
        with dpg.group(tag="mapper_prop_group", show=True):
            themed_text("Property", slot="text_dim")
            dpg.add_combo(
                items=mapper.mappable_properties(),  # e36s03: catalog minus the Cue-only rates
                default_value="brightness",
                width=260,
                tag="mapper_prop_combo",
                callback=on_mapper_prop_change,
            )
        with dpg.group(tag="mapper_comp_group", show=False):
            # e36s03: multi-value properties pick the controlled component/axis
            themed_text("Component", slot="text_dim")
            dpg.add_combo(
                items=[],
                default_value="",
                width=260,
                tag="mapper_component_combo",
            )
        themed_text(
            "The trigger runs the mapping's OSC macro (cue list) — no property needed.",
            slot="text_dim",
            tag="mapper_cue_hint",
            wrap=300,
        )
        dpg.configure_item("mapper_cue_hint", show=False)
        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_button(label="Create", callback=mapper_dialog_confirm, width=120)
            dpg.add_button(label="Cancel", callback=mapper_dialog_cancel, width=120)
    dpg.show_item("mapper_new_dialog")


def _sync_mapper_component_picker(prop: str) -> None:
    """Populate + reveal the dialog's component picker for a vector property
    (e36s03); hide it for scalar/toggle/trigger/enum properties.
    """
    entry = catalog.PROPERTY_CATALOG.get(prop)
    keys = (
        [c["key"] for c in entry["components"]]
        if entry is not None and len(entry["components"]) > 1
        else []
    )
    if keys:
        dpg.configure_item("mapper_component_combo", items=keys)
        dpg.set_value("mapper_component_combo", keys[0])
    dpg.configure_item("mapper_comp_group", show=bool(keys))


def on_mapper_prop_change(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """New-Mapping property combo: reveal the component picker for a vector
    property and suggest the right control type for toggle/trigger families
    (e36s03)."""
    prop = str(app_data)
    if prop not in catalog.PROPERTY_CATALOG:
        return
    family = catalog.family_of(prop)
    control = str(dpg.get_value("mapper_control_combo"))
    if family in (catalog.FAMILY_TOGGLE, catalog.FAMILY_TRIGGER) and control != "cue list":
        dpg.set_value("mapper_control_combo", "button")
    _sync_mapper_component_picker(prop)


def on_mapper_control_type_change(sender: Any, app_data: Any, user_data: Any = None) -> None:
    """New-Mapping type combo: a 'cue list' needs no property — hide the
    property row (and the component picker) and pin it to 'play' (inert; the
    card caption only); the value controls show the property picker (e35s04,
    e36s03)."""
    control = str(app_data)
    is_cue = control == "cue list"
    if is_cue:
        dpg.set_value("mapper_prop_combo", "play")
    dpg.configure_item("mapper_prop_group", show=not is_cue)
    dpg.configure_item("mapper_comp_group", show=False)
    dpg.configure_item("mapper_cue_hint", show=is_cue)
    if not is_cue:
        _sync_mapper_component_picker(str(dpg.get_value("mapper_prop_combo")))


def mapper_dialog_confirm(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Dialog Create: add the mapping chosen in the combos and open the Mapper window.

    e36s03: a multi-value property reads the component picker (defaulting to its
    first component when the combo was never populated); cue-list controls skip
    it (their property is inert). BUG-2026-09-08T120000: the component picker
    must be read BEFORE the dialog is deleted — real dpg get_value() on a
    deleted widget returns None, and the fallback would silently bind every
    vector mapping to its FIRST component (color -> always R).
    """
    if not dpg.does_item_exist("mapper_new_dialog"):
        return
    prop = str(dpg.get_value("mapper_prop_combo"))
    control = str(dpg.get_value("mapper_control_combo"))
    target = state.mapper_pending_target
    component: str | None = None
    if target is not None and control != "cue list":
        entry = catalog.PROPERTY_CATALOG.get(prop)
        if entry is not None and len(entry["components"]) > 1:
            keys = [c["key"] for c in entry["components"]]
            candidate = str(dpg.get_value("mapper_component_combo"))
            component = candidate if candidate in keys else keys[0]
    dpg.delete_item("mapper_new_dialog")
    if target is None:
        return
    mapper.add_mapping(target, prop, control, component=component)
    refresh_mapper_ui()
    dpg.show_item("mapper_window")


def mapper_dialog_cancel(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Dialog Cancel: close without creating a mapping."""
    if dpg.does_item_exist("mapper_new_dialog"):
        dpg.delete_item("mapper_new_dialog")


def _cue_row_label(mapping: dict[str, Any], row: dict[str, Any]) -> str:
    """Human row text: 'alpha = 0.80', 'color (0.20, 0.90, 0.70)',
    'burst turn 0.50 for 500 ms', 'wait 500 ms', 'mapper #7: clipA lock'
    (e35s04 + e36s05 typed payloads)."""
    kind = row.get("kind")
    payload = row.get("payload", {})
    if kind in ("property", "burst"):
        prop = str(payload.get("property") or "")
        if isinstance(payload.get("values"), list):
            text = f"{prop} ({', '.join(f'{float(v):.2f}' for v in payload['values'])})"
        else:
            text = f"{prop} = {float(payload.get('value', 0.0)):.2f}"
        ms = payload.get("ms")
        if kind == "burst":
            text = "burst " + text + f" for {float(ms or 0.0):.0f} ms"
        elif ms:
            text += f" over {float(ms):.0f} ms"
        return text
    if kind == "wait":
        return f"wait {float(payload.get('ms', 0.0)):.0f} ms"
    ref = int(payload.get("mapping_id", 0))
    target = mapper.find_mapping(ref)
    if target is None:
        return f"mapper #{ref} (missing)"
    return f"mapper #{ref}: {target['target_id']} {target['property']}"


def _cue_mapper_option(mapping_id: int) -> str:
    """One mapper-picker item: '<id>: <target> <property>' (id parsed back in the dialog)."""
    target = mapper.find_mapping(mapping_id)
    if target is None:
        return f"{mapping_id}: missing"
    return f"{mapping_id}: {target['target_id']} {target['property']}"


# e35 UAT: horizontal pixels per indent level in the cue editor row list — a
# level-1 row starts visibly to the right of a level-0 row (like code indent).
CUE_ROW_INDENT_PX = 22


def _render_cue_row(mid: int, index: int, row: dict[str, Any]) -> None:
    """One row band: wave level, action label and the +/←/→/X commands (e35s04).

    UAT e35: the whole band is indented by ``level * CUE_ROW_INDENT_PX`` so the
    wave structure reads visually, not just through the level number.
    """
    mapping = mapper.find_mapping(mid)
    if mapping is None:
        return
    level = int(row["level"])
    with dpg.group(horizontal=True, tag=f"cue_row_{mid}_{index}"):
        if level:
            dpg.add_spacer(width=level * CUE_ROW_INDENT_PX)
        themed_text(str(level), slot="text_dim", tag=f"cue_lvl_{mid}_{index}")
        text_tag = f"cue_txt_{mid}_{index}"
        themed_text(_cue_row_label(mapping, row), slot="text", tag=text_tag)
        dpg.add_button(
            label="+",
            width=22,
            callback=cue_row_add_dialog,
            user_data=(mid, index),
            tag=f"cue_add_{mid}_{index}",
        )
        dpg.add_button(
            label="<-",
            width=26,
            callback=cue_row_shift,
            user_data=(mid, index, -1),
            tag=f"cue_left_{mid}_{index}",
        )
        dpg.add_button(
            label="->",
            width=26,
            callback=cue_row_shift,
            user_data=(mid, index, +1),
            tag=f"cue_right_{mid}_{index}",
        )
        dpg.add_button(
            label="X",
            width=22,
            callback=cue_row_delete,
            user_data=(mid, index),
            tag=f"cue_del_{mid}_{index}",
        )
    # double-click the action label to edit the row (e35s04)
    reg_tag = f"cue_dbl_reg_{mid}_{index}"
    if dpg.does_item_exist(reg_tag):
        dpg.delete_item(reg_tag)  # a rebuild must not leak registries
    with dpg.item_handler_registry(tag=reg_tag):
        dpg.add_item_double_clicked_handler(
            0,
            callback=lambda *_, m=mid, i=index: cue_row_edit_dialog(None, None, (m, i)),
        )
    if dpg.does_item_exist(text_tag):
        dpg.bind_item_handler_registry(text_tag, reg_tag)


def _render_cue_rows(mid: int) -> None:
    """Render the row bands (or the empty hint) into the cue_rows_group child."""
    mapping = mapper.find_mapping(mid)
    if mapping is None:
        return
    rows = (mapping.get("cue") or mapper.fresh_cue()).get("rows", [])
    if not rows:
        themed_text("Add rows with +", slot="text_dim")
        return
    for index, row in enumerate(rows):
        _render_cue_row(mid, index, row)


def _refresh_cue_rows(mid: int) -> None:
    """Rebuild the row list of the open cue window after a model change (e35s04).

    The rows live inside the ``cue_rows_group`` child, so the child must be
    PUSHED onto the container stack while rendering (a callback runs with an
    empty stack — parentless items would raise 1011/1009, the UAT bug).
    """
    if not dpg.does_item_exist("cue_rows_group"):
        return
    dpg.delete_item("cue_rows_group", children_only=True)
    dpg.push_container_stack("cue_rows_group")
    try:
        _render_cue_rows(mid)
    finally:
        dpg.pop_container_stack()


def _safe_refresh_cue_rows(mid: int) -> None:
    """Rebuild with errors surfaced in the Logs window (never a silent no-op)."""
    try:
        _refresh_cue_rows(mid)
    except Exception as e:  # worker/UI defensive posture: log, keep the app alive
        log_error("cue window", f"row refresh failed: {e}")


def on_cue_gap_change(sender: Any, app_data: Any, user_data: Any) -> None:
    """Level-gap drag box: store the cue's ms between levels (clamped >= 0, e35s04)."""
    mapping = mapper.find_mapping(int(user_data))
    if mapping is not None:
        cue = mapping.get("cue")
        if isinstance(cue, dict):
            cue["gap_ms"] = max(0.0, float(app_data))


def cue_window_run(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """'Run' in the cue window: start the mapping's cue (same path as the card trigger)."""
    cue.cue_start(int(user_data), allow_restart=True)
    tick_cue_triggers()


def cue_window_stop(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """'Stop' in the cue window: stop the run without OSC (e35s03/e35s04)."""
    cue.cue_stop(int(user_data))
    tick_cue_triggers()


def cue_row_add_dialog(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """'+' on a row: open the add dialog; the new row lands BELOW this one."""
    mid, index = user_data
    _open_cue_row_dialog(mid, insert_after=index)


def cue_window_add_dialog(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """'Add row' on an empty cue: append at the end."""
    _open_cue_row_dialog(int(user_data), insert_after=-1)


def cue_row_edit_dialog(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Double-click on a row: open the edit dialog prefilled with the row (e35s04)."""
    mid, index = user_data
    _open_cue_row_dialog(mid, edit_index=index)


def cue_row_shift(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """'<-' / '->': move the row one wave level (model clamps at 0, e35s04)."""
    mid, index, delta = user_data
    mapping = mapper.find_mapping(mid)
    if mapping is None:
        return
    cue = mapping.get("cue") or mapper.fresh_cue()
    if mapper.shift_cue_row_level(cue, index, delta) is not None:
        _safe_refresh_cue_rows(mid)


def cue_row_delete(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """'X': delete the row and rebuild the list (e35s04)."""
    mid, index = user_data
    mapping = mapper.find_mapping(mid)
    if mapping is None:
        return
    cue = mapping.get("cue") or mapper.fresh_cue()
    mapper.remove_cue_row(cue, index)
    _safe_refresh_cue_rows(mid)


# Cue row dialog geometry (BUG-2026-09-12T191000): the window must FIT its
# editors — corner renders 8 components + ms (9 rows) and overflowed the old
# fixed 470 px height, hiding the OK/Cancel row.
CUE_DLG_DEFAULT_HEIGHT = 470
CUE_DLG_BASE_HEIGHT = 150  # kind + property pickers, separator, OK/Cancel
CUE_DLG_EDITOR_ROW_HEIGHT = 45  # one labelled drag row
CUE_DLG_MAX_HEIGHT = 640  # cap; taller content scrolls
# A no-argument trigger fires without a value, so the row dialog shows a hint
# instead of an editor whose content compose_send_args would discard.
CUE_DLG_FIRE_HINT = "Fire (no value: this message takes no argument)"
# The property and burst groups SHARE these fixed editor tags, so only the
# active kind may hold them (BUG-2026-09-12T191000: a cleanup loop that reused
# the renderer's ``container`` parameter sent every editor to the hidden burst
# group, so the property kind never showed its value editors).
CUE_DLG_VALUE_CONTAINERS = ("cue_prop_values", "cue_burst_values")


def on_cue_kind_change(sender: Any, app_data: Any, user_data: Any = None) -> None:
    """Row-dialog kind combo: reveal ONLY the fields of the chosen kind
    (e35s04 + e36s05: a 'burst' kind runs one bounded rate message).

    e36s06 real-DPG fix: the property/burst containers share the fixed editor
    tags (cue_dlg_value / cue_dlg_v0.. / cue_dlg_ms), so switching kind must
    re-render the ACTIVE value kind (or clear the shared containers when
    leaving them) — otherwise the burst area stays empty (no row can be added)
    and the previous kind's tags linger and duplicate (duplicate tags raise in
    real DPG 2.3.1).
    """
    kind = str(app_data)
    dpg.configure_item("cue_prop_fields", show=kind == "property")
    dpg.configure_item("cue_wait_fields", show=kind == "wait")
    dpg.configure_item("cue_mapper_fields", show=kind == "mapper")
    dpg.configure_item("cue_burst_fields", show=kind == "burst")
    _cue_dlg_render_kind_editors(kind)


def _cue_dlg_render_kind_editors(kind: str) -> None:
    """(Re)render the value editors of the row dialog's ACTIVE kind (e36s06).

    The property and burst containers share the fixed editor tags, so only one
    kind may hold them: entering a value kind renders one fresh set, leaving
    one clears both. Burst normalizes its property combo onto a legal rate
    when it still holds the previous kind's property.
    """
    if kind == "property":
        prop = str(dpg.get_value("cue_dlg_prop_combo"))
        if prop not in catalog.PROPERTY_CATALOG:
            prop = "alpha"
            dpg.set_value("cue_dlg_prop_combo", prop)
        _cue_dlg_render_values("cue_prop_values", prop)
    elif kind == "burst":
        prop = str(dpg.get_value("cue_dlg_burst_combo"))
        rates = _cue_row_rate_candidates()
        if prop not in rates:
            prop = rates[0] if rates else "turn"
            dpg.set_value("cue_dlg_burst_combo", prop)
        _cue_dlg_render_values("cue_burst_values", prop)
    else:
        _cue_dlg_clear_editors()
        _cue_dlg_fit_height(None)


def _cue_dlg_clear_editors() -> None:
    """Drop the shared editor widgets so the next kind renders clean tags."""
    for shared_tag in CUE_DLG_VALUE_CONTAINERS:
        if dpg.does_item_exist(shared_tag):
            dpg.delete_item(shared_tag, children_only=True)


def _cue_dlg_fit_height(prop: str | None) -> None:
    """Fit the row dialog to the active property's editors (BUG-2026-09-12T191000).

    corner renders 8 components + ms = 9 editor rows, which the fixed 470 px
    window pushed below the visible area; a small property keeps the default
    height and a named cap keeps the modal sane (taller content scrolls).
    """
    rows = 0
    if prop is not None:
        entry = catalog.PROPERTY_CATALOG[prop]
        rows = len(entry["components"]) + (1 if entry["ms"] else 0)
    height = CUE_DLG_BASE_HEIGHT + rows * CUE_DLG_EDITOR_ROW_HEIGHT
    height = max(CUE_DLG_DEFAULT_HEIGHT, min(height, CUE_DLG_MAX_HEIGHT))
    if dpg.does_item_exist("cue_dialog"):
        dpg.configure_item("cue_dialog", height=height)


def _dlg_float(tag: str, default: float) -> float:
    """Robust float read from a dialog widget (falls back on garbage, e35s04)."""
    try:
        return float(dpg.get_value(tag))
    except (TypeError, ValueError):
        return default


def _cue_row_prop_candidates() -> list[str]:
    """Property rows target every catalog property except the rate family."""
    return list(mapper.mappable_properties())


def _cue_row_rate_candidates() -> list[str]:
    """Burst rows target the rate family only (loom/turn/grab/resize/ffwd)."""
    return [p for p, e in catalog.PROPERTY_CATALOG.items() if e["family"] == catalog.FAMILY_RATE]


def _cue_dlg_render_values(
    container: str, prop: str, prefilled: list[float] | None = None, ms_value: float = 0.0
) -> None:
    """Render the value/component + optional ms editors of a row property into
    the given container (e36s05): one editor for a single-value property (tag
    cue_dlg_value), one labelled drag per component for multi-value properties
    (tags cue_dlg_v0..), plus an 'Animate (ms)' drag on ms-capable properties.

    The shared editor tags mean both containers are cleared first and the
    labels use themed_text (raw dpg.add_text has no ``slot`` keyword, e36s06).
    BUG-2026-09-12T191000: the cleanup loop must NOT reuse this function's
    ``container`` parameter — doing so parented every editor to the hidden
    burst group, so the property kind never showed its value. Family-aware
    editors: an enum offers its named options, a no-argument trigger shows a
    'fire' hint, everything else a drag.
    """
    _cue_dlg_clear_editors()
    entry = catalog.PROPERTY_CATALOG[prop]
    comps = entry["components"]
    pre = prefilled or [float(c["neutral"]) for c in comps]
    if entry["family"] == catalog.FAMILY_TRIGGER and not catalog.trigger_carries_value(prop):
        themed_text(CUE_DLG_FIRE_HINT, slot="text_dim", parent=container)
    elif entry["family"] == catalog.FAMILY_ENUM:
        _cue_dlg_add_enum_editor(container, entry, pre)
    else:
        _cue_dlg_add_component_editors(container, comps, pre)
    if entry["ms"]:
        _cue_dlg_add_ms_editor(container, ms_value)
    _cue_dlg_fit_height(prop)


def _cue_dlg_add_component_editors(
    container: str, comps: list[dict[str, Any]], pre: list[float]
) -> None:
    """One labelled drag per component under the fixed editor tags."""
    if len(comps) == 1:
        comp = comps[0]
        themed_text("Value", slot="text_dim", parent=container)
        dpg.add_drag_float(
            parent=container,
            default_value=pre[0],
            min_value=float(comp["min"]),
            max_value=float(comp["max"]),
            width=140,
            format="%.2f",
            speed=0.01,
            tag="cue_dlg_value",
        )
        return
    for i, comp in enumerate(comps):
        themed_text(str(comp["label"]), slot="text_dim", parent=container)
        dpg.add_drag_float(
            parent=container,
            default_value=pre[i] if i < len(pre) else float(comp["neutral"]),
            min_value=float(comp["min"]),
            max_value=float(comp["max"]),
            width=140,
            format="%.2f",
            speed=0.01,
            tag=f"cue_dlg_v{i}",
        )


def _cue_dlg_add_enum_editor(container: str, entry: dict[str, Any], pre: list[float]) -> None:
    """An enum row picks its named option; the payload stores the index."""
    options = [str(o) for o in entry["options"] or []]
    index = max(0, min(round(pre[0]), len(options) - 1)) if options else 0
    themed_text("Value", slot="text_dim", parent=container)
    dpg.add_combo(
        items=options,
        default_value=options[index] if options else "",
        width=140,
        tag="cue_dlg_value",
        parent=container,
    )


def _cue_dlg_add_ms_editor(container: str, ms_value: float) -> None:
    """The optional native-animation duration of an ms-capable property."""
    themed_text("Animate (ms)", slot="text_dim", parent=container)
    dpg.add_drag_float(
        parent=container,
        default_value=ms_value if ms_value > 0 else 0.0,
        width=140,
        format="%.0f",
        speed=10.0,
        min_value=0.0,
        max_value=60_000.0,
        tag="cue_dlg_ms",
    )


def _cue_dlg_read_payload(prop: str) -> dict[str, Any]:
    """Read the value/ms fields rendered by _cue_dlg_render_values into the
    model payload shape {property, value|values, ms?} (e36s05). A no-argument
    trigger carries no value: compose_send_args fires [] regardless."""
    entry = catalog.PROPERTY_CATALOG[prop]
    payload: dict[str, Any] = {"property": prop}
    if entry["family"] == catalog.FAMILY_TRIGGER and not catalog.trigger_carries_value(prop):
        return payload
    if len(entry["components"]) > 1:
        payload["values"] = [
            _dlg_float(f"cue_dlg_v{i}", float(c["neutral"]))
            for i, c in enumerate(entry["components"])
        ]
    else:
        payload["value"] = _cue_dlg_read_single_value(entry)
    ms = _dlg_float("cue_dlg_ms", 0.0)
    if ms > 0 and entry["ms"]:
        payload["ms"] = ms
    return payload


def _cue_dlg_read_single_value(entry: dict[str, Any]) -> float:
    """The single-value editor: an enum combo maps its option label to the index."""
    options = [str(o) for o in entry["options"] or []]
    if entry["family"] == catalog.FAMILY_ENUM and options:
        label = str(dpg.get_value("cue_dlg_value"))
        return float(options.index(label)) if label in options else 0.0
    return _dlg_float("cue_dlg_value", 0.0)


def _open_cue_row_dialog(mid: int, insert_after: int = -1, edit_index: int | None = None) -> None:
    """One add/edit dialog for a cue row (property / wait / mapper, e35s04).

    Add mode: ``insert_after`` is the row index the new row lands below
    (-1 appends at the end). Edit mode: ``edit_index`` prefills the fields and
    the confirm replaces that row in place, keeping its level. The dialog is
    kind-first: choosing a kind reveals only its fields.
    """
    mapping = mapper.find_mapping(mid)
    if mapping is None:
        return
    cue = mapping.get("cue") or mapper.fresh_cue()
    editing = edit_index is not None and 0 <= edit_index < len(cue["rows"])
    row = cue["rows"][edit_index] if editing else None
    kind = str((row or {}).get("kind", "property"))
    payload = (row or {}).get("payload") or {}
    prop = str(payload.get("property", "alpha"))
    value = float(payload.get("value", 0.0))
    values = payload.get("values")
    wait_ms = float(payload.get("ms", 500.0)) if kind == "wait" else 0.0
    prop_ms = float(payload.get("ms", 0.0)) if kind == "property" else 0.0
    ref = int(payload.get("mapping_id", 0))
    if dpg.does_item_exist("cue_dialog"):
        dpg.delete_item("cue_dialog")
    with dpg.window(
        label="Edit cue row" if editing else "Add cue row",
        tag="cue_dialog",
        modal=True,
        width=360,
        height=CUE_DLG_DEFAULT_HEIGHT,
        no_resize=True,
    ):
        themed_text("Kind", slot="text_dim")
        dpg.add_combo(
            items=list(mapper.CUE_ROW_KINDS),
            default_value=kind,
            width=220,
            tag="cue_dlg_kind_combo",
            callback=on_cue_kind_change,
        )
        with dpg.group(tag="cue_prop_fields", show=kind == "property"):
            themed_text("Property", slot="text_dim")
            dpg.add_combo(
                items=_cue_row_prop_candidates(),
                default_value=prop if prop in catalog.PROPERTY_CATALOG else "alpha",
                width=260,
                tag="cue_dlg_prop_combo",
                callback=lambda s2, a2: _cue_dlg_render_values("cue_prop_values", str(a2)),
            )
            dpg.add_group(tag="cue_prop_values")
        with dpg.group(tag="cue_burst_fields", show=kind == "burst"):
            themed_text("Rate burst", slot="text_dim")
            dpg.add_combo(
                items=_cue_row_rate_candidates(),
                default_value=prop if prop in catalog.PROPERTY_CATALOG else "turn",
                width=260,
                tag="cue_dlg_burst_combo",
                callback=lambda s2, a2: _cue_dlg_render_values(
                    "cue_burst_values", str(a2), ms_value=_dlg_float("cue_dlg_ms", 0.0)
                ),
            )
            dpg.add_group(tag="cue_burst_values")
        with dpg.group(tag="cue_wait_fields", show=kind == "wait"):
            themed_text("Wait ms", slot="text_dim")
            dpg.add_drag_float(
                default_value=wait_ms,
                width=140,
                format="%.0f",
                speed=10.0,
                min_value=0.0,
                max_value=60_000.0,
                tag="cue_dlg_wait",
            )
        with dpg.group(tag="cue_mapper_fields", show=kind == "mapper"):
            themed_text("Mapper (button / cue list)", slot="text_dim")
            options = [
                _cue_mapper_option(int(m["id"]))
                for m in state.mapper_mappings
                if m.get("control") in ("button", "cue list")
            ]
            default_option = ""
            if options:
                if ref:
                    wanted = _cue_mapper_option(ref)
                    default_option = wanted if wanted in options else options[0]
                else:
                    default_option = options[0]
            dpg.add_combo(
                items=options,
                default_value=default_option,
                width=280,
                tag="cue_dlg_mapper_combo",
            )
            if not options:
                themed_text(
                    "No button / cue list mapping exists yet — add one in the Mapper first.",
                    slot="text_dim",
                    wrap=280,
                )
        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="OK",
                width=120,
                callback=cue_dialog_confirm,
                user_data=(mid, insert_after, edit_index),
            )
            dpg.add_button(
                label="Cancel",
                width=120,
                callback=cue_dialog_cancel,
            )
    if kind == "property":
        prefill = values if isinstance(values, list) else [value]
        _cue_dlg_render_values("cue_prop_values", prop, prefill, ms_value=prop_ms)
    elif kind == "burst":
        prefill = values if isinstance(values, list) else [value]
        _cue_dlg_render_values("cue_burst_values", prop, prefill, ms_value=prop_ms)
    dpg.show_item("cue_dialog")


def cue_dialog_confirm(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Dialog OK: add a row below / replace the edited row through the model.

    Reads the widgets of the chosen kind (the dialog reveals only that kind's
    fields), keeps the level on edit, clamps through the model helpers and
    rebuilds the list. On invalid input the dialog STAYS open and the problem
    is logged to the Logs window — never a silent no-op.
    """
    mid, insert_after, edit_index = user_data
    mapping = mapper.find_mapping(mid)
    if mapping is None:
        return
    cue = mapping.get("cue") or mapper.fresh_cue()
    try:
        kind = str(dpg.get_value("cue_dlg_kind_combo"))
        if kind == "property":
            prop = str(dpg.get_value("cue_dlg_prop_combo"))
            if prop not in catalog.PROPERTY_CATALOG:
                raise ValueError(f"unknown property {prop!r}")
            payload = _cue_dlg_read_payload(prop)  # e36s05: scalar/vector + ms
        elif kind == "wait":
            payload = {"ms": max(0.0, _dlg_float("cue_dlg_wait", 0.0))}
        elif kind == "burst":
            prop = str(dpg.get_value("cue_dlg_burst_combo"))
            if prop not in catalog.PROPERTY_CATALOG:
                raise ValueError(f"unknown rate {prop!r}")
            payload = _cue_dlg_read_payload(prop)
            if float(payload.get("ms", 0.0)) <= 0.0:
                raise ValueError("burst rows need a duration (ms)")
        elif kind == "mapper":
            option = str(dpg.get_value("cue_dlg_mapper_combo"))
            mapping_id = int(option.split(":", 1)[0])
            if mapping_id < 1:
                raise ValueError(f"bad mapper reference {option!r}")
            payload = {"mapping_id": mapping_id}
        else:
            raise ValueError(f"unknown kind {kind!r}")
    except (TypeError, ValueError) as e:
        log_error("cue row dialog", f"nothing added: {e}")
        return  # keep the dialog open so the user can fix the input
    if edit_index is not None and 0 <= edit_index < len(cue["rows"]):
        level = int(cue["rows"][edit_index]["level"])
        row = mapper.add_cue_row(mapper.fresh_cue(), kind, payload, level=level)
        if row is None:
            log_error("cue row dialog", f"row rejected: {kind} {payload}")
            return
        cue["rows"][edit_index] = row
    else:
        position = insert_after if insert_after is not None else -1
        row = mapper.add_cue_row(mapper.fresh_cue(), kind, payload, level=0)
        if row is None:
            log_error("cue row dialog", f"row rejected: {kind} {payload}")
            return
        at = position + 1 if position >= 0 else len(cue["rows"])
        cue["rows"].insert(at, row)
    if dpg.does_item_exist("cue_dialog"):
        dpg.delete_item("cue_dialog")
    _safe_refresh_cue_rows(mid)


def cue_dialog_cancel(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Dialog Cancel: close without touching the cue."""
    if dpg.does_item_exist("cue_dialog"):
        dpg.delete_item("cue_dialog")


def cue_editor_close(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Close the cue-list editor (Close button / mapping removal, e35s05)."""
    mid = int(user_data or 0)
    if dpg.does_item_exist("cue_list_window"):
        dpg.delete_item("cue_list_window")
    if state.cue_editor_mapping_id == mid:
        state.cue_editor_mapping_id = None


def _drop_mapping_editor_and_runs(mapping_id: int) -> None:
    """e35s05: a removed mapping stops its cue run and closes its open editor."""
    cue.cue_stop(mapping_id)
    if state.cue_editor_mapping_id == mapping_id:
        cue_editor_close(None, None, mapping_id)


def _mapper_line_of(mapping: dict[str, Any]) -> int:
    """1-based Mapper line of a mapping's source (UAT e35): the window title
    names the line the cue belongs to, like the other mappings of that row."""
    targets = mapper.row_targets()
    try:
        return targets.index(str(mapping["target_id"])) + 1
    except ValueError:
        return 1


def open_cue_list_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """'Cue list...' on a cue-list card: open the mapping's cue-list editor (e35s04).

    The window edits the mapping's own cue (per-mapping, answer 2). UAT e35: it
    is titled 'Cue list mapper <N>' after the Mapper LINE the mapping belongs to
    — the cue always applies to that line's current source, so no Source/context
    header is shown: the level-gap box and the Run/Stop/Add-row buttons come
    first. Created on demand, non-modal. Unknown mapping ids are a no-op.
    """
    mid = int(user_data)
    mapping = mapper.find_mapping(mid)
    if mapping is None:
        return
    if dpg.does_item_exist("cue_list_window"):
        dpg.delete_item("cue_list_window")
    state.cue_editor_mapping_id = mid  # e35s05: which mapping this editor edits
    line_no = _mapper_line_of(mapping)
    cue = mapping.get("cue") or mapper.fresh_cue()
    with dpg.window(
        label=f"Cue list mapper {line_no}",
        tag="cue_list_window",
        width=520,
        height=420,
    ):
        with dpg.group(horizontal=True):
            themed_text("Level gap (ms)", slot="text_dim")
            dpg.add_drag_float(
                default_value=float(cue.get("gap_ms") or 0.0),
                width=90,
                format="%.0f",
                speed=1.0,
                min_value=0.0,
                callback=on_cue_gap_change,
                user_data=mid,
                tag=f"cue_gap_{mid}",
            )
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="Run", callback=cue_window_run, user_data=mid, tag=f"cue_run_{mid}"
            )
            dpg.add_button(
                label="Stop", callback=cue_window_stop, user_data=mid, tag=f"cue_stop_{mid}"
            )
            dpg.add_button(
                label="Add row",
                callback=cue_window_add_dialog,
                user_data=mid,
                tag=f"cue_add_row_{mid}",
            )
            dpg.add_button(
                label="Close",
                width=90,
                callback=cue_editor_close,
                user_data=mid,
            )
        dpg.add_separator()
        with dpg.child_window(tag="cue_rows_group", width=0, height=180, border=True):
            _render_cue_rows(mid)
    dpg.show_item("cue_list_window")


def midi_control_loop() -> None:
    """MIDI control worker (e14s02): poll every controller input port, route messages,
    push executions to the main thread via ui_task_queue (HIGH-1 — no direct dpg calls).
    """
    try:
        import mido
    except ImportError:
        return
    open_ports: dict[str, Any] = {}
    # BUG-2026-09-07: last-failure timestamps per port gate the open retry — a
    # failing open can leak an ALSA sequencer client, so a dead ALSA is never
    # hammered every tick (that saturation silently killed controller detection).
    open_fail_at: dict[str, float] = {}
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
                if not midi_open_retry_due(open_fail_at.get(port_name), time.monotonic()):
                    continue  # cooldown: stay quiet on a failing port
                try:
                    open_ports[port_name] = mido.open_input(port_name)
                    open_fail_at.pop(port_name, None)
                    controller_connect(controller, mido)  # e14s02: LED output + grid bindings
                    append_log("MIDI", f"Control listening on {port_name}")
                except Exception as e:
                    open_fail_at[port_name] = time.monotonic()
                    log_error("MIDI", str(e))
                    continue
            try:
                for msg in open_ports[port_name].iter_pending():
                    handle_midi_message(msg, port_name)
            except Exception as e:
                open_fail_at[port_name] = time.monotonic()
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
            # BUG-2026-09-07: a no-input idle loop must not re-enumerate every few
            # seconds — each scan on a failing ALSA can leak a sequencer client.
            time.sleep(MIDI_OPEN_RETRY_COOLDOWN_SECONDS)
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
            time.sleep(MIDI_OPEN_RETRY_COOLDOWN_SECONDS)


# ---------- e26: Leap Motion worker ----------
# The library's OWN auto-poll thread (auto_poll=True + a Listener) owns the LeapC
# handshake and event loop — the official example pattern. This daemon thread
# only manages the lifecycle: lazy-import the external 'leap' package (absolute
# import — the suite/CI may not have it), open/keep/reconnect the connection
# while state.leap_enabled, close when disabled. Missing library or service
# degrades to status 'missing'/'disconnected' with slow retries and never
# crashes the app (CONVENTIONS § Defensive Code). Tracking frames normalize
# into state.leap_values under state.leap_lock; UI/status updates go through
# the queues (HIGH-1: no dpg from any thread here).


def _leap_lib() -> Any:
    """The external leap package, imported lazily; None when unavailable (e26s01)."""
    try:
        import leap  # type: ignore[import-not-found]

        return leap
    except Exception:
        return None


def _leap_viz_capture_geometry(event: Any) -> list[dict[str, Any]]:
    """Hand-geometry dicts for one tracking event (e26s04, poll thread).

    Called synchronously while the hand data is valid; an empty list means no
    hand is in view (the skeleton panel shows its waiting label).
    """
    return [leap.viz_hand_geometry(h) for h in event.hands or []]


def _leap_viz_copy_ir(img: Any) -> Any:
    """Synchronous grayscale copy of one IR image (e26s04, poll thread).

    The C buffer behind the Image wrapper is only valid until the next poll, so
    the copy happens inside the event callback (reference example's UAF note).
    leapc_cffi is imported lazily here: rig-only, the suite never runs this.
    """
    props = img.c_data.properties
    w, h, bpp = int(props.width), int(props.height), int(props.bpp)
    if w == 0 or h == 0 or bpp != 1:
        raise ValueError(f"unsupported IR image {w}x{h} bpp={bpp}")
    import leapc_cffi  # type: ignore[import-not-found]

    ptr = leapc_cffi.ffi.cast("uint8_t*", img.c_data.data) + img.c_data.offset
    buf = leapc_cffi.ffi.buffer(ptr, w * h * bpp)
    return np.frombuffer(buf, dtype=np.uint8).reshape((h, w)).copy()


def _leap_viz_publish_if_due(now: float) -> None:
    """Compose + publish one visualizer frame at the rate cap (e26s04, poll thread).

    Reads the latest IR copy + hand geometry by reference swap (both are only
    ever replaced, never mutated), composes OUTSIDE the lock, then publishes
    under state.leap_lock (frame swap + seq bump + render timestamp). A compose
    failure degrades to a log line and a cleared frame — never a crash.
    """
    if not (state.leap_enabled and state.leap_visualizer):
        return
    with state.leap_lock:
        last = state.leap_viz_last_render
    if not leap.viz_render_due(now, last):
        return
    with state.leap_lock:
        ir = state.leap_viz_ir
        hands = state.leap_viz_hands
    try:
        frame = leap.compose_viz_frame(ir, hands)
    except Exception as e:
        log_error("Leap", f"viz compose: {e}")
        with state.leap_lock:
            state.leap_viz_frame = None
            state.leap_viz_ir = None
            state.leap_viz_hands = None
        return
    with state.leap_lock:
        state.leap_viz_frame = frame
        state.leap_viz_seq += 1
        state.leap_viz_last_render = now


def _leap_set_images_policy(connection: Any, lib: Any, on: bool) -> None:
    """Set/clear the LeapC Images policy on the LIVE connection (e26s04).

    Off = the device does not stream IR, so the disabled visualizer costs
    nothing. set_policy_flags is a blocking call-and-wait whose Policy event is
    delivered by the library's auto-poll thread; a failure degrades to a log
    line, never a crash (BLE001 posture).
    """
    try:
        # The binding exposes PolicyFlag under leap.enums (not the package root
        # — live-verified 2026-09-03: lib.PolicyFlag raises AttributeError and
        # the IR stream silently never starts).
        images = lib.enums.PolicyFlag.Images
        if on:
            connection.set_policy_flags(flags_to_set=[images])
            append_log("Leap", "visualizer on (IR stream requested)")
        else:
            connection.set_policy_flags(flags_to_clear=[images])
            append_log("Leap", "visualizer off (IR stream stopped)")
    except Exception as e:
        log_error("Leap", f"images policy: {e}")


def _leap_listener(lib: Any) -> Any:
    """A Listener whose callbacks run on the library's auto-poll thread.

    The callbacks only touch state (under leap_lock) and the log queues — no
    dpg (HIGH-1). Poll errors arrive via on_error (the library reports timeouts
    and handshake noise the same way); only real failures flip the status.
    """

    class _LeapTrackingListener(lib.Listener):
        def on_tracking_event(self, event):  # type: ignore[no-untyped-def]
            snapshot = normalize_tracking_event(event)
            with state.leap_lock:
                state.leap_values.clear()
                state.leap_values.update(snapshot)
            state.leap_status = "tracking"
            state.leap_last_frame = time.time()  # e26s05: watchdog heartbeat
            if state.leap_stall_count:
                # e26s05: a frame after an escalation = the stream is back.
                state.leap_stall_count = 0
            drive_leap_mappings(snapshot)  # e26s03: push leap-bound mappings
            # e26s04: while the visualizer is on, capture the hand geometry and
            # publish one rate-capped composite (the poll thread is the only
            # writer of the visualizer snapshot; the main tick only uploads the
            # finished texture).
            if state.leap_visualizer:
                hands = _leap_viz_capture_geometry(event)
                with state.leap_lock:
                    state.leap_viz_hands = hands
                _leap_viz_publish_if_due(time.time())

        def on_image_event(self, event):  # type: ignore[no-untyped-def]
            # IR frames arrive ONLY while the Images policy is set, i.e. while
            # the visualizer is on — the copy must happen synchronously (the C
            # buffer is valid only until the next poll, same UAF as tracking).
            if not state.leap_visualizer:
                return
            try:
                ir = _leap_viz_copy_ir(event.image[0])
            except Exception as e:
                log_error("Leap", f"viz image: {e}")
                return
            with state.leap_lock:
                state.leap_viz_ir = ir
            _leap_viz_publish_if_due(time.time())

        def on_connection_lost_event(self, event):  # type: ignore[no-untyped-def]
            state.leap_status = "disconnected"

        def on_device_lost_event(self, event):  # type: ignore[no-untyped-def]
            state.leap_status = "disconnected"

        def on_error(self, error):  # type: ignore[no-untyped-def]
            if type(error).__name__ in ("LeapTimeoutError", "LeapNotConnectedError"):
                return  # idle poll / handshake in progress: not failures
            state.leap_status = "disconnected"
            log_error("Leap", f"poll: {error}")

    return _LeapTrackingListener()


def _leap_backoff_sleep(seconds: float) -> None:
    """Interruptible backoff after a stall-forced reconnect (e26s05).

    Sleeps in short steps so an engine-off during the backoff reacts within a
    second instead of after the full escalated wait.
    """
    deadline = time.time() + seconds
    while state.leap_enabled and time.time() < deadline:
        time.sleep(min(1.0, deadline - time.time()))


def leap_control_loop() -> None:
    """Leap Motion worker (e26s01): keep one auto-polling connection while enabled.

    The library's own poll thread (auto_poll=True) owns the LeapC handshake and
    event loop — the official example pattern; our listener normalizes tracking
    frames into state.leap_values. The worker thread only manages the lifecycle:
    open when enabled (retry/backoff on failure), watch the status, reconnect
    after a loss, close when disabled.
    """
    missing_logged = False
    while True:
        if not state.leap_enabled:
            time.sleep(0.2)
            continue
        lib = _leap_lib()
        if lib is None:
            state.leap_status = "missing"
            if not missing_logged:
                append_log("Leap", "library/service not available (leapc-python-api + Gemini)")
                missing_logged = True
            time.sleep(5.0)
            continue
        missing_logged = False
        connection: Any = None
        try:
            connection = lib.Connection()
            connection.add_listener(_leap_listener(lib))
            # NOTE: connection.open() is a @contextmanager — calling it without
            # `with` is a silent no-op. connect() is the plain-method equivalent
            # that keeps the connection open for this worker's keep-alive loop.
            connection.connect(auto_poll=True, timeout=3)
            connection.set_tracking_mode(lib.TrackingMode.Desktop)
            state.leap_status = "connected"
            state.leap_last_frame = time.time()  # e26s05: silence window starts fresh
            append_log("Leap", "connected")
            # e26s04: a persisted visualizer toggle asks for IR as soon as the
            # (re)connection is up; afterwards the keep-alive below follows it.
            if state.leap_visualizer:
                _leap_set_images_policy(connection, lib, True)
        except Exception as e:
            log_error("Leap", f"connect: {e}")
            with contextlib.suppress(Exception):
                connection.disconnect()
            state.leap_status = "disconnected"
            time.sleep(2.0)
            continue
        # Keep the connection open while enabled; a lost service/device flips the
        # status to disconnected (listener) so this loop exits and reconnects.
        # The keep-alive also watches the visualizer toggle and sets/clears the
        # LeapC Images policy on the live connection (no reconnect needed) and
        # the e26s05 watchdog: the service can wedge silently (evaluator frozen
        # while the process + USB stay alive) with NO events arriving, so after
        # LEAP_STALL_TIMEOUT of tracking silence this loop forces a reconnect.
        viz_policy = bool(state.leap_visualizer)
        stall_exit = False
        while state.leap_enabled and state.leap_status != "disconnected":
            if leap.stall_detected(time.time(), state.leap_last_frame):
                stall_exit = True
                state.leap_stall_count += 1
                state.leap_status = "disconnected"
                if state.leap_stall_count == leap.LEAP_STALL_ESCALATION_COUNT:
                    append_log(
                        "Leap",
                        "tracking keeps stalling - restart the hand-tracking "
                        "service or replug the device",
                    )
                elif state.leap_stall_count < leap.LEAP_STALL_ESCALATION_COUNT:
                    append_log(
                        "Leap",
                        "tracking stalled (no frames for "
                        f"{leap.LEAP_STALL_TIMEOUT:.0f}s) - reconnecting "
                        f"(attempt {state.leap_stall_count})",
                    )
                break
            viz_now = bool(state.leap_visualizer)
            if viz_now != viz_policy:
                viz_policy = viz_now
                _leap_set_images_policy(connection, lib, viz_now)
            time.sleep(0.5)
        with contextlib.suppress(Exception):
            connection.disconnect()
        if stall_exit:
            # a wedged service is not hot-looped: back off (fast, then slow),
            # interruptibly so an engine-off during the backoff reacts quickly.
            _leap_backoff_sleep(leap.stall_retry_wait(state.leap_stall_count))


def show_settings_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the general settings window from the top menubar."""
    dpg.show_item("settings_window")
    dpg.focus_item("settings_window")  # e17: a shown window must come to the front


def show_logs_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Open the OSC logs window from the top menubar (Show > Logs).

    The multiline log follows the window size when shown (e38 UAT request:
    selectable/copyable log).
    """
    dpg.show_item("logs_window")
    dpg.focus_item("logs_window")  # e17: a shown window must come to the front
    if dpg.does_item_exist("osc_log_text"):
        w = max(300, dpg.get_item_width("logs_window") - 20)
        h = max(100, dpg.get_item_height("logs_window") - 42)
        dpg.configure_item("osc_log_text", width=w, height=h)


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
    """Windows in switching order: (tag, real window-title label).

    The main window (Step Sequencer) is always on screen and is not a switching
    target; Monitor Players are appended live so the list (and Ctrl+Tab) always
    match the windows that exist.
    """
    entries = [
        ("sequencer_window", "Step Sequencer"),
        ("audio_window", "Audio analyzer"),
        ("vimix_media_window", "Vimix sources"),
        ("logs_window", "Logs"),
        ("mapper_window", "Mapper"),
    ]
    entries += [(p["tag"], f"Monitor Player {p['id']}") for p in monitor_players]
    if state.preview_active is not None:  # the preview window exists while a preview is active
        entries.append((PREVIEW_WINDOW_TAG, "Preview"))
    return entries


_window_menu_dynamic_tags: list[str] = []  # live list items, deleted on refresh


# The app's windows (BUG-2026-09-01T194500). Opening a menu makes DPG report the
# menu itself as the active window (mvContainers.cpp: menu draw sets
# GContext->activeWindow), so the focus track must accept ONLY real windows.
_FOCUS_TRACKED_WINDOWS: tuple[str, ...] = (
    "sequencer_window",
    "audio_window",
    "vimix_media_window",
    "logs_window",
    "mapper_window",
    "settings_window",
    "midi_window",
    "leap_window",
    "help_window",
)


def _is_tracked_window(tag: Any) -> bool:
    """True when the active item is one of the app's windows, not a menu/popup."""
    s = str(tag)
    return s in _FOCUS_TRACKED_WINDOWS or s.startswith("monitor_player_")


def _active_window_tag(tag: Any) -> str | None:
    """Resolve the active item to its app window (BUG-2026-09-01T194500).

    DPG reports arbitrary widgets as the active window (verified live: clicking
    a step pad makes get_active_window() return the pad tag, e.g. 'seq_cell_2_4')
    and the open menu itself while a menu is shown. Walk up the item tree to the
    first tracked window; return None for menus/popups/unknowns.
    """
    item = tag
    for _ in range(64):  # bounded parent walk
        if item is None:
            return None
        s = str(item)
        if _is_tracked_window(s):
            return s
        # Real DPG raises get_item_info on stale/unknown ids (boot returns 0)
        # — bail out before the walk instead of crashing.
        if not dpg.does_item_exist(item):
            return None
        parent = dpg.get_item_parent(item)
        if parent is None or str(parent) == s:
            return None
        item = parent
    return None


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
    active = state.current_window
    for tag, label in _window_menu_entries():
        if not (dpg.does_item_exist(tag) and dpg.is_item_shown(tag)):
            continue  # only list windows that are actually open
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

    The signature is (tracked current window, shown window tags); anything else
    the list shows is static. We remember the last focused window: get_active_window()
    reports arbitrary widgets (step pads, combos) and the open menu itself, so
    the track resolves the active item up to its app window and never clears on
    a menu/popup/None result (BUG-2026-09-01T194500).
    """
    global _window_menu_sig
    active = dpg.get_active_window()
    window = _active_window_tag(active)
    if window is not None:
        state.current_window = window
    sig = (
        state.current_window,
        tuple(
            tag
            for tag, _ in _window_menu_entries()
            if dpg.does_item_exist(tag) and dpg.is_item_shown(tag)
        ),
    )
    if sig != _window_menu_sig:
        _window_menu_sig = sig
        refresh_window_menu()


def switch_to_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Show + focus a window (Windows-menu list click, Ctrl+Tab target)."""
    tag = user_data
    if dpg.does_item_exist(tag):
        dpg.show_item(tag)
        dpg.focus_item(tag)
        state.current_window = tag


def on_cycle_window(sender: Any = None, app_data: Any = None, user_data: Any = None) -> None:
    """Ctrl+Tab / Ctrl+Shift+Tab: cycle through the SHOWN workspace windows.

    DPG 2.3.1 key handlers have no modifier support, so the wrapper checks the
    modifier keys itself; Tab keeps its normal role while an input is focused.
    The anchor is the tracked current window (not DPG get_active_window, which
    is None while the menu bar has focus — BUG-2026-09-01T194500).
    """
    if not dpg.is_key_down(dpg.mvKey_ModCtrl) or _any_input_focused():
        return
    forward = not dpg.is_key_down(dpg.mvKey_ModShift)
    entries = _window_menu_entries()
    if not entries:
        return
    active = state.current_window
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
                beat_seconds: float | None = None  # e36s04: fades fall back to 60/BPM
            else:
                if not _timed_bpm_live():
                    time.sleep(0.05)
                    continue  # no live tempo: never advance on a stale BPM (e10s08)
                base_sleep = 60.0 / state.current_bpm if state.current_bpm > 0 else 0.5
                beat_seconds = base_sleep
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
                    try:
                        # e36s04: the property+mode engine replaces the hard-coded
                        # AlphaV/AlphaR/AlphaF/ColorV/ColorR/SeekR if/elif chain
                        execute_step(track, r, state.current_step, beat_seconds)
                    except Exception as e:
                        print(f"[viseq OSC Error] {e}")

            led_tag = BEAT_LED_TAGS.get(state.beat_source)
            if led_tag:
                flash_led(led_tag)
        else:
            time.sleep(0.1)


def on_lowpass_toggle(sender: Any, app_data: Any, user_data: Any) -> None:
    state.lowpass_enabled = bool(app_data)


def _sync_analysis_widgets() -> None:
    """Mirror the analysis flags onto their checkboxes (e28s03)."""
    for tag, flag in (
        ("cb_spectrum_analysis", state.is_audio_analyzing),
        ("cb_bpm_analysis", state.is_beat_tracking),
    ):
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, bool(flag))


def _sync_audio_stream() -> bool:
    """Open/close the shared input stream to match the analysis flags (e28s03).

    Returns False when the requested stream could not be opened (no input
    device or a device failure); the callers flip the flags back off and sync
    the widgets — the same bounce a failed manual click produces.
    """
    global audio_stream
    needs_stream = state.is_audio_analyzing or state.is_beat_tracking
    if needs_stream and audio_stream is None:
        device_string = str(dpg.get_value("combo_devices"))
        if "No input device" in device_string:
            return False
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
            audio_stream = None
            return False
    elif not needs_stream and audio_stream is not None:
        audio_stream.stop()
        audio_stream.close()
        audio_stream = None
        dpg.set_value("testo_bpm", "BPM: ---")
    return True


def set_audio_analysis_flags(spectrum: bool, bpm: bool) -> None:
    """Restore the analysis checkboxes from a project (e28s03, main thread).

    Sets both flags + widgets, then opens the input stream when any flag is on
    (the device combo is restored before this call). On a failed open the flags
    bounce back off with a log, exactly like a failed manual click — a rig
    without capture never hangs with a phantom analysis flag.
    """
    state.is_audio_analyzing = bool(spectrum)
    state.is_beat_tracking = bool(bpm)
    _sync_analysis_widgets()
    if not _sync_audio_stream():
        state.is_audio_analyzing = False
        state.is_beat_tracking = False
        _sync_analysis_widgets()
        log_error("Audio", "analysis disabled: no usable input device")


def toggle_audio_stream(sender: Any, app_data: Any, user_data: Any) -> None:
    """Checkbox clicks: flip one analysis flag, sync the widgets and the stream.

    e28s03: the failure bounce (set_value(sender, False)) is now the flag +
    widget sync — a failed open flips the clicked flag back off.
    """
    if user_data == "vu_meter":
        state.is_audio_analyzing = bool(app_data)
    elif user_data == "beat_tracking":
        state.is_beat_tracking = bool(app_data)
    else:
        return
    _sync_analysis_widgets()
    if not _sync_audio_stream():
        if user_data == "vu_meter":
            state.is_audio_analyzing = False
        else:
            state.is_beat_tracking = False
        _sync_analysis_widgets()


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

# Compact ProggyTiny font for the Mediagrid tile titles (same guarded pattern; a missing
# asset leaves _tile_title_font None and the titles use the default 13 px font).
for _tile_title_font_path in _TILE_TITLE_FONT_PATHS:
    if os.path.exists(_tile_title_font_path):
        with dpg.font_registry():
            _tile_title_font = dpg.add_font(_tile_title_font_path, size=MEDIA_TITLE_FONT_SIZE)
        break

# e32s02: larger ProggyTiny for the Mapper row line numbers (one step above the
# 10 px mapper font, so the numbers stay compact but readable).
for _mapper_line_no_font_path in _TILE_TITLE_FONT_PATHS:
    if os.path.exists(_mapper_line_no_font_path):
        with dpg.font_registry():
            _mapper_line_no_font = dpg.add_font(
                _mapper_line_no_font_path, size=MAPPER_LINE_NO_FONT_SIZE
            )
        break


def _project_flow_ui_blocked() -> bool:
    """True while a project dialog or a confirmation modal is shown (e37s02).

    Save shortcuts must stay inert while the user types a filename in a file
    dialog or answers the Exit / New-project prompt.
    """
    tags = ("open_project_dialog", "save_project_dialog", EXIT_CONFIRM_TAG, NEW_PROJECT_CONFIRM_TAG)
    return any(dpg.does_item_exist(t) and dpg.is_item_shown(t) for t in tags)


def _on_save_key(sender: Any, app_data: Any, user_data: Any) -> None:
    """Key wrapper: DPG 2.3.1 handlers ignore modifiers — gate on Ctrl/Shift (e37s02)."""
    if not dpg.is_key_down(dpg.mvKey_ModCtrl) or _project_flow_ui_blocked():
        return
    if dpg.is_key_down(dpg.mvKey_ModShift):
        show_save_project_dialog()  # Ctrl+Shift+S = Save as...
    else:
        save_current_project()  # Ctrl+S = Save


with dpg.handler_registry():
    # DPG 2.3.1 key handlers have no modifier support: the wrapper checks Ctrl itself
    dpg.add_key_press_handler(dpg.mvKey_C, callback=_on_copy_key)
    dpg.add_key_press_handler(dpg.mvKey_V, callback=_on_paste_key)
    # e17: Ctrl+Tab / Ctrl+Shift+Tab cycle the workspace windows (the callback
    # checks the modifiers and the input-focus guard itself).
    dpg.add_key_press_handler(dpg.mvKey_Tab, callback=on_cycle_window)
    # e37s02: Ctrl+S / Ctrl+Shift+S save / save-as (the wrapper checks modifiers).
    dpg.add_key_press_handler(dpg.mvKey_S, callback=_on_save_key)


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
    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, MEDIA_TILE_PAD, MEDIA_TILE_PAD)
    dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 2, 2)

with dpg.theme() as theme_normal_clip, dpg.theme_component(dpg.mvChildWindow):
    theme_color(dpg.mvThemeCol_Border, "border")
    theme_color(dpg.mvThemeCol_ChildBg, "panel_bg")
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)
    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, MEDIA_TILE_PAD, MEDIA_TILE_PAD)
    dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 2, 2)

with dpg.theme() as theme_vimix_current_clip, dpg.theme_component(dpg.mvChildWindow):
    # Vimix's current source in a lighter, non-green border (e10s06): clearly
    # distinct from the green viseq primary selection and the plain tile.
    theme_color(dpg.mvThemeCol_Border, "text_dim")
    theme_color(dpg.mvThemeCol_ChildBg, "panel_bg")
    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)
    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, MEDIA_TILE_PAD, MEDIA_TILE_PAD)
    dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 2, 2)

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

with dpg.theme() as theme_alpha_slider, dpg.theme_component(dpg.mvSliderFloat):
    # Thin vertical alpha slider on a Mediagrid tile: subtle rail + accent grabber.
    # The rail uses the border slot (palette-driven); the grabber matches the app
    # accent so the value reads at a glance on the dark tile.
    theme_color(dpg.mvThemeCol_FrameBg, "border")
    theme_color(dpg.mvThemeCol_FrameBgHovered, "border")
    theme_color(dpg.mvThemeCol_FrameBgActive, "border")
    theme_color(dpg.mvThemeCol_SliderGrab, "accent")
    theme_color(dpg.mvThemeCol_SliderGrabActive, "accent")
    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 2)
    dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 2)

with dpg.theme() as theme_mapper_compact, dpg.theme_component(dpg.mvAll):
    # e23: compact Mapper — tight paddings/frames so the source rows and the
    # mini-cards shrink (the texts/controls also use the 10 px ProggyTiny
    # font). Measured against DPG 2.3.1: rows become ~60 px and the mini-cards
    # 150 px wide.
    theme_color(dpg.mvThemeCol_FrameBg, "border")
    theme_color(dpg.mvThemeCol_FrameBgHovered, "border")
    theme_color(dpg.mvThemeCol_FrameBgActive, "border")
    theme_color(dpg.mvThemeCol_SliderGrab, "accent")
    theme_color(dpg.mvThemeCol_SliderGrabActive, "accent")
    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 4, 4)
    dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 6, 2)
    dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 4, 2)
    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 3)
    dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 2)

with dpg.theme() as theme_learn_marker, dpg.theme_component(dpg.mvButton):
    # e33: the red 'M' learn markers — the red text signals "bind this to MIDI"
    # and stands out from the dim card captions (fixed accent red on every
    # palette; the markers only appear during a MIDI Learn session).
    dpg.add_theme_color(dpg.mvThemeCol_Text, (225, 60, 60, 255))
    dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 0, 0)

with dpg.theme() as theme_learn_marker_armed, dpg.theme_component(dpg.mvButton):
    # ARMED 'M' (user 2026-09-07): the marker clicked last turns amber while it
    # waits for its MIDI message, so the user sees exactly which control the
    # next binding lands on. Palette-driven via theme_color (warning slot), so
    # it re-themes with the active palette like the other semantic colors.
    theme_color(dpg.mvThemeCol_Text, "warning")
    theme_color(dpg.mvThemeCol_ButtonHovered, "accent_bg")
    dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 0, 0)

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
            callback=toggle_play,  # e33s04: marker captures (uniform), the button executes
            width=60,
            height=26,
        )
        dpg.add_button(
            label="<",
            callback=callback_nudge_backward,  # e33s04: plain callback (marker era)
            width=28,
            height=26,
        )
        dpg.add_button(
            label="RESYNC",
            callback=callback_resync,  # e33s04: plain callback (marker era)
            width=50,
            height=26,
        )
        dpg.add_button(
            label=">",
            callback=callback_nudge_forward,  # e33s04: plain callback (marker era)
            width=28,
            height=26,
        )
        dpg.add_spacer(width=8)
        dpg.add_checkbox(
            label=BEAT_SOURCE_LABELS[BEAT_SOURCE_ANALYSIS],
            tag="cb_beat_bpm_analysis",
            default_value=True,
            callback=on_beat_source,  # e33s04: plain callback (marker era)
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
            callback=on_beat_source,  # e33s04: plain callback (marker era)
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
            callback=on_beat_source,  # e33s04: plain callback (marker era)
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
            callback=on_beat_source,  # e33s04: plain callback (marker era)
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
            callback=tap_bpm,  # e33s04: plain callback (marker era)
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
        tag="cb_spectrum_analysis",  # e28s03: stable tag for project restore
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
            tag="cb_bpm_analysis",  # e28s03: stable tag for project restore
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
        label="Vimix sources",
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
    # e38 UAT (user, 2026-09-07): the log must be selectable/copyable — a
    # readonly multiline input (ImGui readonly still allows mouse selection
    # and Ctrl+C); a plain text item cannot be copied.
    dpg.add_input_text(
        default_value="Waiting for OSC traffic...",
        multiline=True,
        readonly=True,
        width=930,
        height=112,
        tag="osc_log_text",
    )

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
    # BUG-2026-09-07: input-scan outcome line — shows the ALSA failure instead of
    # presenting a silent empty device list when the sequencer is exhausted.
    themed_text("", slot="text_dim", tag="midi_ports_status")
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

# WINDOW 8: Leap Motion (hidden; opened from Settings > "Leap Motion"). One
# window hosts EVERYTHING (user request, e26s02): the Enable switch, the
# device/service status line and the LIVE two-hand value monitor — static rows
# built ONCE at construction (one row per snapshot field, both hands), refreshed
# by tick_leap_monitor on the main thread. e26s04 adds the embedded visualizer
# toggle + hidden panel between the status line and the monitor; the window
# stays COMPACT (560x680, never widens — user decision), the monitor child
# shrinks when the panel is shown. Never touches the external leap package at
# import time.

# e26s04: compact visualizer layout. The always-present toggle row costs the
# monitor ~25 px; showing the 150-tall panel costs a further ~165 px and the
# monitor scrolls (it is already a child_window).
LEAP_MONITOR_H: int = 535
LEAP_MONITOR_VIZ_H: int = 370
LEAP_VIZ_PANEL_H: int = 158
with dpg.window(
    label="Leap Motion", width=560, height=680, pos=(560, 300), tag="leap_window", show=False
):
    dpg.add_checkbox(
        label="Enable Leap Motion",
        tag="leap_enable_cb",
        default_value=state.leap_enabled,
        callback=on_leap_enable,
    )
    dpg.add_separator()
    dpg.add_spacer(height=4)
    dpg.add_text("", tag="leap_status_text")
    dpg.add_spacer(height=4)
    dpg.add_separator()
    dpg.add_checkbox(
        label="Show device view + hands",
        tag="leap_viz_cb",
        default_value=state.leap_visualizer,
        callback=on_leap_visualizer,
    )
    with dpg.child_window(height=LEAP_VIZ_PANEL_H, show=False, tag="leap_viz_panel"):
        dpg.add_text("Waiting for the Leap device...", tag="leap_viz_wait_text")
    with (
        dpg.child_window(height=LEAP_MONITOR_H, tag="leap_monitor_scroll"),
        dpg.group(horizontal=True),
    ):
        for hand in leap.LEAP_HANDS:
            with dpg.group(tag=f"leap_mon_{hand}_col"):
                themed_text(f"{hand.capitalize()} hand", slot="text_bright")
                for field in leap.LEAP_FIELDS:
                    meta = leap.leap_field(field)
                    with dpg.group(horizontal=True):
                        themed_text(meta["label"], slot="text_dim")
                        dpg.add_text(
                            leap.LEAP_MONITOR_PLACEHOLDER,
                            tag=f"leap_mon_{hand}_{field}",
                        )

# e16/e22/e23/e24: Mapper window — the body is rebuilt by refresh_mapper_ui()
# (menu open, create, delete, prune, resize) as a stack of wrapping source
# blocks. Hidden at boot and never part of the saved layout (transient
# workspace, like Logs).
# e20s02: only the mapping blocks — no header line, and the scroll container is
# borderless so no outer frame wraps the content.
# e23: compact theme (theme_mapper_compact).
# e24s02: sources WRAP onto multiple lines (no overflow), so the scroll child
# keeps NO horizontal_scrollbar (DPG would force both scrollbar tracks) and its
# vertical scrollbar appears only when the wrapped content is taller than the
# window. A resize item handler reflows the body live.
with (
    dpg.window(
        label="Mapper",
        width=MAPPER_WINDOW_WIDTH,
        height=MAPPER_WINDOW_HEIGHT,
        pos=(60, 60),
        tag="mapper_window",
        show=False,
    ),
    dpg.child_window(
        height=MAPPER_WINDOW_HEIGHT - 8,
        border=False,
        tag="mapper_scroll",
    ),
):
    # e40s01: the section chips + New Route sit above the body inside the scroll
    with dpg.group(horizontal=True, tag="mapper_filter_group"):
        themed_text("Show", slot="text_dim")
        dpg.add_checkbox(
            label="Control",
            tag="mapper_filter_control",
            default_value=True,
            callback=on_mapper_filter,
        )
        dpg.add_checkbox(
            label="Get", tag="mapper_filter_get", default_value=True, callback=on_mapper_filter
        )
        dpg.add_checkbox(
            label="Global",
            tag="mapper_filter_global",
            default_value=True,
            callback=on_mapper_filter,
        )
        dpg.add_button(label="New Route", callback=new_route_dialog)
    # NOTE: a bare dpg.group(...) call does NOT create the item — the context
    # manager must be entered (dearpygui 2.x), same as the original tuple-with.
    with dpg.group(tag="mapper_mappings_group"):
        pass
dpg.bind_item_theme("mapper_window", theme_mapper_compact)
with dpg.item_handler_registry(tag="mapper_resize_reg"):
    # e24s02: reflow the wrapping body whenever the user resizes the window
    dpg.add_item_resize_handler(callback=lambda s, a: refresh_mapper_ui())
dpg.bind_item_handler_registry("mapper_window", "mapper_resize_reg")

# NEW THREAD FOR HIGH-FREQUENCY FADES
threading.Thread(target=fade_tick_loop, daemon=True).start()
threading.Thread(target=cue_tick_loop, daemon=True).start()  # e35s03: cue engine clock
threading.Thread(target=spectrum_analyzer_loop, daemon=True).start()
threading.Thread(target=midi_clock_loop, daemon=True).start()
threading.Thread(target=midi_control_loop, daemon=True).start()  # e09: control worker
threading.Thread(target=leap_control_loop, daemon=True).start()  # e26: leap worker

threading.Thread(target=sequencer_tick, daemon=True).start()
threading.Thread(target=visual_metronome_loop, daemon=True).start()
threading.Thread(target=essentia_analyzer_loop, daemon=True).start()
threading.Thread(target=thumbnail_decoder_worker, daemon=True).start()

dpg.create_viewport(title="viSeq - Audio-Reactive VJ Controller", width=1700, height=1080)
# e19/e37s04: closing the main window goes through the dirty-gated request — a
# clean session quits immediately, a dirty one opens the exit modal
# (disable_close keeps rendering on; the modal's Exit button stops the app).
dpg.set_exit_callback(request_exit)
dpg.configure_viewport("__viewport", disable_close=True)
apply_boot_config()  # e06: apply the saved theme + (optionally) the saved window layout
ensure_user_dirs()  # e21s01: eager XDG user dirs (config + projects) + legacy .viseq migration
with dpg.viewport_menu_bar():
    with dpg.menu(label="viSeq"):  # e11s03: first menubar menu — project file flows
        dpg.add_menu_item(label="New project", callback=request_new_project)  # e15s01/e37s04
        dpg.add_menu_item(label="Open project", callback=show_open_project_dialog)
        with dpg.menu(label="Last project", tag="menu_last_project"):
            pass  # children rebuilt by rebuild_last_project_menu() (boot + after every save/open)
        dpg.add_menu_item(label="Save", callback=save_current_project)  # e37s02: silent save
        dpg.add_menu_item(label="Save as...", callback=show_save_project_dialog)  # e37s02
        dpg.add_separator()
        dpg.add_menu_item(label="Exit", callback=exit_app)
    with dpg.menu(label="Windows", tag="menu_windows"):  # e12s01 + e17 (window list)
        dpg.add_menu_item(label="New Monitor Player", callback=new_monitor_player)
        dpg.add_menu_item(label="Show Mapper", callback=show_mapper_window)  # e16
        dpg.add_menu_item(label="Show Logs", callback=show_logs_window)
        dpg.add_menu_item(label="Show MIDI Monitor", callback=show_midi_monitor)  # e39s01
        dpg.add_menu_item(label="Show Info", callback=show_help_window)
        dpg.add_separator(parent="menu_windows")  # e17: open windows below the actions
        # the live window list is rebuilt by refresh_window_menu() (e17)
    with dpg.menu(label="Settings"):  # e12s01: config panels under one menu
        dpg.add_menu_item(label="General", callback=show_settings_window)
        dpg.add_menu_item(label="MIDI", callback=show_midi_window)
        dpg.add_menu_item(label="Leap Motion", callback=show_leap_window)  # e26

# e11s03/e13s02: project file dialogs are created ON DEMAND by
# show_open_project_dialog / show_save_project_dialog (_recreate_project_dialog)
# with .viseq/.* filters — DPG shows only directories without extension filters,
# and a fresh dialog guarantees the default path exists.
rebuild_last_project_menu()  # e11s03: populate the Last-project submenu for boot
dpg.setup_dearpygui()
dpg.show_viewport()
refresh_window_title()  # e37s03: the title announces the boot project identity
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

        tick_project_dirty(time.time())  # e37s03: unsaved-changes marker cadence

        tick_cue_triggers()  # e35s03: cue-list card running labels (idle-cheap)

        tick_midi_monitor()  # e39s01: MIDI Monitor panes (idle-cheap, revision-gated)

        tick_routes()  # e40s01: state Origin -> MIDI/OSC Destination emissions

        tick_midi_learn_timeout()  # e18: expire stale MIDI Learn sessions (incl. mapper)

        tick_leap_monitor()  # e26s02: live two-hand values in the Leap Motion window

        tick_leap_visualizer()  # e26s04: embed the IR + skeleton frame (when shown)

        tick_source_preview(time.time())  # e38s03: preview frames + transport sync

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
