"""Named constants for viseq (REFACTOR_LATEST.md commit 2/13).

Pure data — no mutable app state, no dpg. Moved verbatim from viseq.py;
viseq.py imports these names so every usage site and the test facade
keep working unchanged.
"""

import copy
from typing import Any

MAX_THUMBNAIL_PIXELS = 3_000_000  # explicit cap; real thumbs are ~58k px


MAX_THUMBNAIL_BLOB_BYTES = 8 * 1024 * 1024  # per-blob cap


MAX_STATE_JSON_BYTES = 1 * 1024 * 1024  # per-replydata cap


THUMB_REQUEST_INTERVAL = 3.0  # min seconds between thumbnail requests per source


LOG_HISTORY_LIMIT = 25  # max entries kept in the OSC log window


MONITOR_OFFSET = (280, 260)  # grid spacing between monitor player windows


DPG_COLOR_SCALE = 255.0  # DPG ToColor divides color inputs by 255 -> its color API is 0..255


# --- BEHAVIORAL CONSTANTS (audit L-6) ---
# Main-loop render cadence (perf e07 P1): full rate while something animates, throttled
# while idle (input stays responsive at ~30 fps; SPIKE-perf measured the idle render as
# the biggest fixed cost).
FRAME_SLEEP_ANIMATED = 0.016  # ~60 fps while sequencer/spectrum/monitor video animate


FRAME_SLEEP_IDLE = 0.033  # ~30 fps at rest


# Step-cell layout: a centered square leaves the checkbox/type row on top (audit L-6)
# ImGui WindowPadding.x inside child windows (default style; app themes don't override)
# Measured on DPG 2.3.1: this indent centers the swatch in the 90px cell (padding included)
STEP_CELL_SIZE = 90  # px side of each sequencer step cell


STEP_COLOR_SQUARE_SIZE = 40  # px side of the centered color square inside a step cell


STEP_CELL_CONTENT_PADDING = 8


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


# Mediagrid tile: index badge overlay + compact layout (audit L-6)
# e10s06: the tile title fits at most two wrapped lines, truncated with an ellipsis.
# The title font is ProggyTiny (monospace, pixel family of the default ProggyClean):
# at 9 px its line height equals the font size and the advance is ~5.4 px.
MEDIA_TITLE_FONT_SIZE = 10  # px font size of the media tile title


MEDIA_TITLE_CHARS_PER_LINE = 20  # ProggyTiny-10 wrap capacity at MEDIA_TITLE_WRAP (measured)


MEDIA_TITLE_RESERVE_PX = 7  # net layout step per wrapped title line (DPG 2.3.1 measured)


MEDIA_TILE_PAD = 5  # px uniform WindowPadding of a media tile (equal gaps all around)


MEDIA_TITLE_GAP = 2  # px base gap between the tile title and the thumbnail row


MEDIA_BADGE_W = 28  # px width of the media-index badge overlay on the thumbnail


MEDIA_BADGE_H = 20  # px height of the media-index badge overlay on the thumbnail


MEDIA_TILE_H = 104  # px height of a media tile (two-line title + thumbnail + alpha slider)


MEDIA_ALPHA_SLIDER_W = 10  # px width of the thin vertical alpha slider on a tile


MEDIA_TITLE_WRAP = 125  # px wrap width of the media tile title


MEDIA_TITLE_MAX_LINES = 2


MEDIA_TITLE_ELLIPSIS = "..."  # ASCII dots: ProggyClean (default font) has no U+2026 glyph


MEDIA_TITLE_CHAR_PX = 7  # default-font estimate (ProggyClean 13 px) used before the atlas is built


MONITOR_THUMB_W = 115  # thumbnail width, same as the Mediagrid/sequencer


MONITOR_THUMB_H = 65  # thumbnail height, same as the Mediagrid/sequencer


MONITOR_DISC_SIZE = 64  # px side of the turntable disc


MONITOR_ALPHA_W = 10  # px width of the vertical alpha bar


MONITOR_SEEK_W = 250  # px width of the horizontal seek bar


MONITOR_DISC_R = 26.0  # radius of the rotating turntable arm


MONITOR_DISC_RPM = 33.0  # disc rotations per minute at speed 1.0 (vinyl standard)


MONITOR_SPEED_TEXT_SIZE = 12  # px font size of the speed label inside the disc


# e16/e20/e22/e23: Mapper window geometry — the body is a vertical stack of
# source rows (e22s01): one horizontal line per vimix source = the source
# thumbnail at the sequencer slot size (110x70, bare) + one bordered mini-card
# per mapping. e23: each mini-card holds — caption (label + X) above a control
# that spans the content width, then the 'output:' from/to line and the
# 'input:' from/to line (visible when a source is bound) under it. e23 iteration:
# everything renders COMPACT — the 10 px ProggyTiny font (MEDIA_TITLE_FONT_SIZE)
# for texts/controls, tight frame/item paddings, small X (16 px) and 40 px
# drag boxes — so rows are short and the mini-cards narrow. Rows are sized to
# their OWN content (see _mapper_row_height): slider rows ~60 px, knob rows
# taller, +16 px when a source is bound. Measured on DearPyGui 2.3.1 with the
# compact theme (WindowPadding 4/FramePadding y 2/ItemSpacing 2): content inset
# ~4 px top + ~4 px bottom air (MAPPER_ROW_PAD_V = 8 — trimmed 2026-09-05 so the
# cards no longer show a dead band at the bottom), a small-font text/drag row is
# 14 px, a slider/button box 14 px, the knob a fixed 44 px, gaps 2 px; the
# ProggyTiny-10 advance is 6 px/char (widest label 'Transparency' = 72 px).
MAPPER_WINDOW_WIDTH = 660


MAPPER_WINDOW_HEIGHT = 460


MAPPER_ROW_THUMB_W = SLOT_BUTTON_WIDTH  # px width of the row thumbnail (= the sequencer slot)


MAPPER_ROW_THUMB_H = SLOT_BUTTON_HEIGHT  # px height of the row thumbnail


MAPPER_LINE_NO_W = 16  # px width of the row line-number column (e32s02; fits 2 digits flush)


MAPPER_LINE_NO_DIGIT_PX = 8  # px advance of a ProggyTiny-13 digit (5.4 px @9 px -> ~7.8 px)


MAPPER_LINE_NO_FONT_SIZE = 13  # px size of the row line numbers (ProggyTiny, > the 10 px font)


MAPPER_LINE_NO_TEXT_H = 13  # px text height used to center the line number in the row


MAPPER_TEXT_H = 14  # px height of one compact text/drag row (measured 2026-09-06 on the
# 10 px ProggyTiny theme: a drag box is ~14 px, not 17 — the old value left a
# visible dead band under the last readout row of a card)


MAPPER_CTRL_H = 14  # px height of a compact slider/button box (same 10 px-font measure)


MAPPER_KNOB_H = 44  # px height of the fixed knob widget


MAPPER_ROW_GAP = 2  # px item spacing between the card rows


MAPPER_ROW_PAD_V = 8  # card inset: 4 top + 4 bottom air (2026-09-05: was 6+12 —
# rows reclaimed ~8-10 px and the cards no longer show a dead band at the bottom)


MAPPER_SMALL_CHAR_PX = 6  # px advance of the 10 px ProggyTiny mapper font (measured)


MAPPER_MINI_W = 150  # px width of one mapping mini-card (caption + output line fit)


MAPPER_X_W = 16  # px width of the X delete button on a mini-card caption row


MAPPER_X_H = 16  # px height of the X delete button


MAPPER_RESET_W = 16  # px width of the reset button on a mini-card caption row (e27s01)


MAPPER_RESET_H = 16  # px height of the reset button


MAPPER_CB_W = 16  # px width of the enable checkbox on a caption row (measured, compact theme)


# e34s01: per-row '+' add button — a NARROW trailing slot (2026-09-05 rework:
# it used to span the full card pitch and wrapped like a 150 px card on resize).
MAPPER_ADD_W = 24  # the '+' button width
MAPPER_ADD_H = 22  # the '+' button height
MAPPER_ADD_SLOT_W = 36  # the slot child + its spacing (small, NOT the card pitch)


MAPPER_DRAG_W = 40  # px width of the from/to drag boxes on the slider 'output:'/'input:' lines


# e34s02: px width of the from/to drag boxes in the compact knob/button band —
# smaller than MAPPER_DRAG_W so 'Out'/'Inp' + two boxes fit beside the 44 px control.
MAPPER_BAND_DRAG_W = 34

# e33s04: fixed slot count of the Mapper in the learn bar (user rule: 4 lines,
# pre-bindable even when fewer rows exist — a missing line is a logged no-op).
MAPPER_LEARN_SLOTS = 4

# e33s02: learn-marker button on a card caption / row lead (compact, tooltip-only)
MAPPER_MARKER_W = 15
MAPPER_MARKER_H = 15

# e33s04: wider spacing BETWEEN the learn-bar groups (replaces the almost
# invisible DPG vertical separators — the user asked for plain space).
MARKER_GROUP_GAP = 18


VIOSC_IP = "127.0.0.1"


VIOSC_PORT = 6666


VIOSC_LISTEN_PORT = 6667  # the port viOSC sends replies to; viseq's own server listens here


# e38: source video preview (SOURCE_PREVIEW_LATEST.md) — the HTTP file
# transport on machine A (viOSC, VIOSC_PREVIEW_PORT default) and the local
# decode/display caps on machine B. Frames are decoded at <= the texture cap
# (DPG stretches the small texture to the panel) and pushed at most
# PREVIEW_MAX_FPS — one raw-texture set_value per frame on the main thread.
PREVIEW_PORT = 8686  # matches the viOSC HTTP preview server default
PREVIEW_PATH_PREFIX = "/preview/"  # served as /preview/<source-name>/file|meta
PREVIEW_CAP_WIDTH = 640  # px texture-cap width (measured budget, SPIKE-source-preview)
PREVIEW_CAP_HEIGHT = 360  # px texture-cap height
PREVIEW_MAX_FPS = 30.0  # decoded/pushed frame ceiling
PREVIEW_HTTP_TIMEOUT = 10.0  # s: av.open + meta request timeout
# e38 BUG-2026-09-12: playback rate. The transport is paced against the wall
# clock from the frame PTS; 1.0 is real time (the default). The control lets the
# user speed up/slow down and reset to the default.
PREVIEW_SPEED_DEFAULT = 1.0  # real-time playback (the reset target)
PREVIEW_SPEED_MIN = 0.25  # slowest selectable rate (~4x longer)
PREVIEW_SPEED_MAX = 4.0  # fastest selectable rate
PREVIEW_SPEED_STEP = 0.05  # drag speed of the Speed field


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
    "mapper_window",  # e28s02: the Mapper is a workspace window — pos/size/open persist
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


MIDI_ACTION_SEQ_TOGGLE = "seq_toggle"


MIDI_ACTION_TRANSPORT_PLAY = "transport_play"


MIDI_ACTION_TRANSPORT_RESYNC = "transport_resync"


MIDI_ACTION_TRANSPORT_TAP = "transport_tap"


MIDI_ACTION_NUDGE_BACK = "nudge_back"


MIDI_ACTION_NUDGE_FORWARD = "nudge_forward"


MIDI_ACTION_BEAT_SOURCE = "beat_source"


MIDI_ACTION_TRACK_ASSIGN = "track_assign"


MIDI_ACTION_MAPPER_MAPPING = "mapper_mapping"  # e18: a learned MIDI control drives a Mapper mapping


MIDI_ACTION_MAPPER_CUE_OPEN = "mapper_cue_open"  # e34s05: open a cue-list mapping's window

# e33s02: marker-based learn targets on a Mapper card caption (enable + reset)
MIDI_ACTION_MAPPER_ENABLE = "mapper_enable"  # toggle the e24 armed flag (momentary)
MIDI_ACTION_MAPPER_RESET = "mapper_reset"  # return the control to its neutral default (e27)

# e33s03: marker-based learn targets on the Mapper rows and the card source menu
MIDI_ACTION_MAPPER_LINE = "mapper_line"  # variant B: selected media -> the mapper row (line)
MIDI_ACTION_MAPPER_BAND = "mapper_band"  # bind the mapping to an audio band (2/3)

# e33s04: source browsing + selection-relative actions (Mediagrid learn bar).
# Rule (user, 2026-09-05): bindings never anchor to a volatile source — the
# action applies to the SELECTED source at trigger time.
MIDI_ACTION_SOURCE_NEXT = "source_next"  # select the next source in the grid order (wrap)
MIDI_ACTION_SOURCE_PREV = "source_prev"  # select the previous source in the grid order (wrap)
MIDI_ACTION_REGEN_SELECTED = "regen_selected_thumb"  # regen the selected source's thumbs
MIDI_ACTION_SEQ_ROW_ASSIGN = "seq_row_assign"  # selected source -> a sequencer row (slot)
MIDI_ACTION_SEQ_ROW_ENABLE = "seq_row_enable"  # activate every step of a sequencer row
MIDI_ACTION_SEQ_ROW_DISABLE = "seq_row_disable"  # deactivate every step of a sequencer row
MIDI_ACTION_ENABLE_CORRECTION = "enable_correction"  # arm the SELECTED source's CC block
MIDI_ACTION_MONITOR_TOGGLE = "monitor_toggle"  # e39s01: show/hide the MIDI Monitor window


# e39s01: MIDI Monitor (diagnostic window). The capture is bounded so a spinning
# wheel can never grow memory; coalescing is for display only (one row per
# control with last/min/max/count). The neutral centre is the app-wide CC/pitch
# midpoint used by _parse_midi_msg; the outcome tags are what the stream and
# controls panes render.
MIDI_MONITOR_STREAM_LIMIT = 400  # newest stream entries kept
MIDI_MONITOR_CONTROL_LIMIT = 256  # distinct controls tracked (oldest activity evicted)
MIDI_MONITOR_NEUTRAL_CENTRE = 64
MIDI_MONITOR_VALUE_MAX = 127.0
MIDI_MONITOR_REFRESH_INTERVAL = 0.1  # s; UI text blocks refresh at most this often
MIDI_MONITOR_OUTCOME_LEARN = "LEARN"
MIDI_MONITOR_OUTCOME_MATCH = "MATCH"
MIDI_MONITOR_OUTCOME_SENT = "SENT"
MIDI_MONITOR_OUTCOME_HOLD = "HOLD"
MIDI_MONITOR_OUTCOME_MUTED = "MUTED"
MIDI_MONITOR_OUTCOME_NOMATCH = "NOMATCH"
MIDI_MONITOR_OUTCOME_NOBIND = "NOBIND"


# e33s02: momentary learn actions trigger at CC value >= this threshold (MIDI's
# conventional CC-switch split); lower CC values are a deliberate no-op.
MIDI_CC_TRIGGER_THRESHOLD = 64


# BUG-2026-09-06T124150: pitch-bend levers (DJ pitch faders) send mido
# `pitchwheel`, a 14-bit signed message (-8192..8191, centre 0), not CC. viseq
# normalizes it onto the app-wide 0..127 value scale so every consumer (Mapper
# input-range seed, trigger thresholds) is unchanged. A pitch wheel has no
# number — the channel is its only discriminator.
MIDI_PITCH_MIN = -8192
MIDI_PITCH_MAX = 8191
MIDI_PITCH_NUMBER = 0
MIDI_PITCH_VALUE_STEPS = 127  # target scale, matching CC/note velocities


DEFAULT_CONFIG: dict[str, Any] = {
    # e11s02: the window layout moved into project files; the config keeps the
    # fallback theme, MIDI and the recent-projects list + restore flag.
    "theme": {"preset": "scuro", "colors": copy.deepcopy(DEFAULT_PALETTE)},
    # e14s02: multi-controller schema — controllers[] replaces the single input_port.
    "midi": {"enabled": False, "controllers": [], "clock_source": None},
    # e26: Leap Motion engine — persisted settings: enabled flag + the e26s04
    # embedded visualizer toggle (both off by default).
    "leap": {"enabled": False, "visualizer": False},
    "projects": {"recent": [], "restore_last_on_boot": True},
    # e28s04: OSC endpoints are rig settings (not project content) — the viOSC
    # client + listening-server IP:port persist app-level like theme/MIDI/Leap.
    "osc": {
        "client_ip": VIOSC_IP,
        "client_port": VIOSC_PORT,
        "listen_ip": VIOSC_IP,
        "listen_port": VIOSC_LISTEN_PORT,
    },
    # e38: source video preview — the viOSC HTTP file-server port (same host as
    # the OSC client, machine A). Only the port is tunable; the host always
    # follows the configured osc client_ip.
    "preview": {"port": PREVIEW_PORT},
}


# e14: a Learn session can never hijack the sequencer controls indefinitely — it
# auto-exits after this many seconds if no capture completes.
MIDI_LEARN_TIMEOUT_SECONDS = 30.0


# BUG-2026-09-07: after a failed input open (or clock reconnect) the worker waits
# this long before trying again. Every failing mido/rtmidi open can leak an ALSA
# sequencer client (mido #256, fixed only in 1.3.4.dev+), so a dead ALSA must
# never be hammered — the 2 s retry loop saturated the 192-client kernel table
# on the live rig and silently killed controller detection.
MIDI_OPEN_RETRY_COOLDOWN_SECONDS = 15.0


PROJECT_FORMAT = "viseq-project"


PROJECT_VERSION = 1


PROJECT_FILE_EXTENSION = ".viseq"


RECENT_PROJECTS_MAX = 5  # cap for the Last-project submenu / config list (e11s02)


STEP_PERSISTED_KEYS: tuple[str, ...] = ("active", "type", "v1", "v2", "frames", "msgs", "color")


# e28s01: the mapping-model keys that survive in a project file (capture whitelist /
# sanitize schema). The runtime model never gains a key that is not persisted here.
MAPPER_PERSISTED_KEYS: tuple[str, ...] = (
    "id",
    "target_id",
    "property",
    "component",  # e36s02: controlled axis/channel of a multi-value property (None for scalar)
    "control",
    "value",
    "band",
    "midi",
    "leap",
    "output_from",
    "output_to",
    "input_from",
    "input_to",
    "enabled",
    "cue",  # e35s01: the per-mapping cue macro (rows + gap_ms), inert for non-cue-list controls
)


# e28s01: restore cap for the mapper section of a project file — a corrupted or
# hand-edited document cannot balloon the live mapper beyond this many mappings.
MAPPER_MAX_MAPPINGS = 256


NUM_STEPS = 8


NUM_TRACKS = 8


BEAT_SOURCE_ANALYSIS = "bpm_analysis"


BEAT_SOURCE_BAND1 = "band1_beat"


BEAT_SOURCE_MIDI = "midi_sync"


BEAT_SOURCE_MANUAL = "manual_bpm"


BEAT_SOURCE_LABELS = {
    BEAT_SOURCE_ANALYSIS: "BPM Det",
    BEAT_SOURCE_BAND1: "Band 1",
    BEAT_SOURCE_MIDI: "MIDI",
    BEAT_SOURCE_MANUAL: "Manual",
}


DEFAULT_MANUAL_BPM = 120.0  # manual-mode BPM default (New project + sanitize fallback)


MIDI_CLOCK_PULSES_PER_BEAT = 24  # MIDI standard: 24 clock pulses (0xF8) per quarter note


BPM_DETECTION_STALE_SECONDS = 2.0  # 2x the 1 s analysis cadence


SPECTRUM_FFT_SIZE = 2048  # samples per FFT frame


SPECTRUM_BARS = 16


NUM_BANDS = 3  # independent selectable bands (band1/band2/band3)


SPECTRUM_FPS = 30.0  # spectrum redraw rate while analyzing


SPECTRUM_DB_FLOOR = 60.0  # dB below full scale mapped to bar level 0


SPECTRUM_F_MIN = 40.0  # Hz: bottom of the perceptual bar range (sub-bass edge)


SPECTRUM_F_MAX = 20000.0  # Hz: top of the perceptual bar range (below Nyquist)


SPECTRUM_PEAK_TARGET = 0.9  # AGC: the recent spectral peak maps to this level


SPECTRUM_PEAK_FLOOR = 0.06  # AGC: below this peak the gain stays flat (silence guard)


SPECTRUM_PEAK_DECAY = 0.995  # AGC release per frame (~-0.04 dB/frame, ≈ -1.3 dB/s):


BAND_AGG_WEIGHT = 0.6  # band value = weight*peak + (1-weight)*mean of the band bars


BAND_BEAT_THRESHOLD = 0.6


SPEC_DRAWLIST_W = 330  # spectrum drawlist width (px)


SPEC_DRAWLIST_H = 66  # spectrum drawlist height (px) — tall enough to read the bars


THUMB_CYCLE_INTERVAL = 0.75  # seconds per frame in the Mediagrid thumb cycle


THUMB_FAIL_THRESHOLD = 5  # request cycles (~15 s) before the tile flips to failed


THUMB_FAIL_LABEL = " [ Thumb failed — right-click to retry ] "


GRID_LED_OFF = "off"


GRID_LED_WHITE = "white"


GRID_LED_RED = "red"


GRID_LED_AMBER = "amber"


GRID_LED_GREEN = "green"


GRID_FLASH_SECONDS = 0.12  # beat flash pulse duration (timer restores the head color)
