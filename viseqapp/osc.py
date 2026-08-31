"""OSC communication for viseq (REFACTOR_LATEST.md commit 7/13).

The conversation with viOSC: the UDP receiver (server class + default
handler routing replies into the queues), the thumbnail decode worker and
the pure vimix-state queries. WORKER-SAFE: this module never imports dpg
(HIGH-1) — the server thread and the decode thread only touch queues.
UI status wiring (server/client buttons) and reply rendering stay in the
composition root until the ui commit.
"""

import io
from typing import Any

import numpy as np
from PIL import Image
from pythonosc import osc_server, udp_client

from viseqapp import state
from viseqapp.constants import (
    MAX_STATE_JSON_BYTES,
    MAX_THUMBNAIL_BLOB_BYTES,
    MAX_THUMBNAIL_PIXELS,
    VIOSC_IP,
    VIOSC_PORT,
)
from viseqapp.queues import append_log, log_error

osc_client = udp_client.SimpleUDPClient(VIOSC_IP, VIOSC_PORT)


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


def thumbnail_decoder_worker() -> None:
    while True:
        name, idx, blob_bytes = state.blob_queue.get()
        try:
            image = Image.open(io.BytesIO(blob_bytes))
            width, height = image.size
            if width * height > MAX_THUMBNAIL_PIXELS:
                raise ValueError(f"thumbnail too large: {width}x{height} px")
            rgba = image.convert("RGBA")
            img_data = np.array(rgba, dtype=np.float32) / 255.0
            state.texture_queue.put((name, idx, img_data.flatten(), width, height))
        except Exception as e:
            print(f"[viseq Decoder Error] Unable to decode '{name}': {e}")
        state.blob_queue.task_done()


def get_current_target_id() -> str | None:
    """Return the target id of the currently selected media (e10s06).

    The viseq Mediagrid selection is primary; before the first click (or after
    the selected source is removed) it falls back to the vimix current source.
    """
    if state.viseq_selected_source is not None:
        return state.viseq_selected_source
    current_source = state.global_vimix_state.get("current_source")
    if current_source is None:
        return None
    for k, props in state.global_vimix_state.get("sources", {}).items():
        if str(k) == str(current_source):
            name = props.get("name")
            return str(name) if name else str(k)
    return None


def find_source_by_name(name: str) -> Any:
    for idx, props in state.global_vimix_state.get("sources", {}).items():
        if str(props.get("name")) == str(name):
            return idx, props
    return None, None


def find_player_index(player_id: int) -> int | None:
    for i, p in enumerate(state.monitor_players):
        if p["id"] == player_id:
            return i
    return None


def send_monitor_command(player_id: int) -> None:
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = state.monitor_players[idx]
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


def incoming_osc_handler(address: str, *args: Any) -> None:
    append_log("IN ", address)
    try:
        if address == "/viosc/replydata" and args and len(args[0]) <= MAX_STATE_JSON_BYTES:
            state.ui_state_queue.put(args[0])
        elif (
            address.startswith("/viosc/replythumb/")
            and args
            and len(args[0]) <= MAX_THUMBNAIL_BLOB_BYTES
        ):
            parts = address.split("/")
            state.blob_queue.put((parts[-2], parts[-1], args[0]))
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
