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
    ROUTE_OSC_MAX_PORT,
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


def incoming_osc_handler(address: str, *args: Any) -> None:
    append_log("IN ", address)
    try:
        if address == "/viosc/replydata" and args and len(args[0]) <= MAX_STATE_JSON_BYTES:
            state.ui_state_queue.put(args[0])
        elif address.startswith("/viosc/reply/") and args:
            # e40s06: a targeted watch delta (prop, value, ...) for one source
            state.watch_state_queue.put((address[len("/viosc/reply/") :], list(args)))
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


# e40s03: OSC OUTPUT DESTINATIONS — an opt-in third-party egress.
# One cached SimpleUDPClient per (host, port) so a Route never recreates a socket
# per emission; a destination is validated before any send (address must be an
# OSC path, port in range, host non-empty). The viOSC client above is untouched:
# /vimix traffic never goes through here.

_route_clients: dict[tuple[str, int], Any] = {}


def validate_destination(spec: Any) -> str | None:
    """The problem with an OSC destination spec, or None when it is usable (e40s03)."""
    if not isinstance(spec, dict):
        return "missing OSC destination"
    host = str(spec.get("host") or "").strip()
    if not host:
        return "OSC host is empty"
    raw_port = spec.get("port")
    try:
        port = int(raw_port) if raw_port is not None else 0
    except (TypeError, ValueError):
        return f"OSC port {raw_port!r} is not a number"
    if not 1 <= port <= ROUTE_OSC_MAX_PORT:
        return f"OSC port {port} out of range 1..{ROUTE_OSC_MAX_PORT}"
    address = str(spec.get("address") or "").strip()
    if not address.startswith("/") or len(address) < 2:
        return f"OSC address {address!r} must start with '/'"
    return None


def _client_for(host: str, port: int) -> Any:
    """The cached UDP client of one (host, port) destination (e40s03)."""
    key = (str(host), int(port))
    client = _route_clients.get(key)
    if client is None:
        client = udp_client.SimpleUDPClient(key[0], key[1])
        _route_clients[key] = client
    return client


def send_route_osc(spec: Any, value: float) -> bool:
    """Send one value to a Route's OSC destination; False on an invalid spec (e40s03).

    Best-effort and never raising into the main loop: a bad spec or a failing
    send is logged and returns False (the viOSC client and the /vimix path are
    untouched).
    """
    error = validate_destination(spec)
    if error is not None:
        log_error("OSC", f"route destination: {error}")
        return False
    address = str(spec["address"])
    try:
        _client_for(str(spec["host"]), int(spec["port"])).send_message(address, [float(value)])
        append_log("OUT", f"{address} [{float(value):.2f}] (route)")
        return True
    except Exception as e:
        log_error("OSC", f"route destination {address}: {e}")
        return False
