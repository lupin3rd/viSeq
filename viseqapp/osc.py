"""OSC communication for viseq (REFACTOR_LATEST.md commit 7/13).

The conversation with viOSC: the UDP receiver (server class + default
handler routing replies into the queues), the thumbnail decode worker and
the pure vimix-state queries. WORKER-SAFE: this module never imports dpg
(HIGH-1) — the server thread and the decode thread only touch queues.
UI status wiring (server/client buttons) and reply rendering stay in the
composition root until the ui commit.
"""

import contextlib
import contextvars
import io
import socket
from collections.abc import Iterator
from typing import Any

import numpy as np
from PIL import Image
from pythonosc import osc_server, udp_client

from viseqapp import dataplane, iomonitor, state
from viseqapp.constants import (
    IO_MONITOR_DIRECTION_IN,
    IO_MONITOR_DIRECTION_OUT,
    IO_MONITOR_KIND_DESTINATION,
    IO_MONITOR_KIND_MONITOR,
    IO_MONITOR_KIND_OSC,
    IO_MONITOR_KIND_STATE,
    IO_MONITOR_KIND_SYNC,
    IO_MONITOR_KIND_THUMBNAIL,
    IO_MONITOR_KIND_VIMIX,
    IO_MONITOR_KIND_VIOSC,
    IO_MONITOR_KIND_WATCH,
    IO_MONITOR_KIND_WATCH_REPLY,
    MAPPING_OSC_MAX_PORT,
    MAX_STATE_JSON_BYTES,
    MAX_THUMBNAIL_BLOB_BYTES,
    MAX_THUMBNAIL_PIXELS,
    RECV_BUFFER_BYTES,
    THUMB_HTTP_MAX_FAILURES,
    THUMB_REQUESTS_PER_SOURCE,
    VIOSC_IP,
    VIOSC_PORT,
)
from viseqapp.queues import append_log, log_error

# NOTE: the client is created at the END of the module, once observe_client
# exists (the wrapper is defined further down with the I/O Monitor plumbing).


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


def request_thumbnail_over_osc(name: str, index: int) -> None:
    """Ask the daemon for one frame on the ORIGINAL OSC lane (e41s03 fallback)."""
    client = state.viosc_client or osc_client
    client.send_message(f"/viosc/thumb/{name}", [int(index)])
    append_log("OUT", f"/viosc/thumb/{name} {index}")


def fetch_one_thumbnail(name: str, index: int) -> bool:
    """Fetch one frame over the data plane, falling back to OSC (e41s03).

    Returns True when the data plane produced the blob. On ANY failure the
    request is re-issued on the OSC lane, so a mixed-version rig keeps working:
    an older daemon has no ``/thumb`` route at all, and a down server is
    indistinguishable from that.

    The lane is given up ONLY after THUMB_HTTP_MAX_FAILURES consecutive fetches
    with NO ANSWER. A 404 answers — the endpoint is alive and has no such frame
    (an out-of-range index on the e41s02 stall path) — and must not cost the
    fast lane for the rest of the session.
    """
    host = state.dataplane_host
    port = state.dataplane_port
    blob, answered = dataplane.fetch_thumbnail(host, port, name, index)
    if blob is not None:
        state.thumb_http_supported = True
        state.thumb_http_failures = 0
        state.blob_queue.put((name, str(index), blob))
        return True
    if answered:
        state.thumb_http_failures = 0
    else:
        state.thumb_http_failures += 1
        if state.thumb_http_failures >= THUMB_HTTP_MAX_FAILURES:
            state.thumb_http_supported = False
            log_error(
                "Thumbnail data plane",
                f"{host}:{port} not answering — falling back to the OSC thumbnail lane",
            )
    request_thumbnail_over_osc(name, index)
    return False


def thumbnail_fetch_worker() -> None:
    """Drain the data-plane fetch queue, one thumbnail at a time (e41s03).

    The OSC lane's replies arrive on the OSC server thread; this worker is the
    data plane's equivalent source, and it feeds the SAME blob queue — so the
    decode worker and every texture consumer are untouched by the new transport.
    """
    while True:
        name, index = state.thumb_fetch_queue.get()
        try:
            fetch_one_thumbnail(name, index)
        except Exception as e:  # a worker thread must never die (Defensive Code)
            log_error("Thumbnail data plane", str(e))
        state.thumb_fetch_queue.task_done()


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


def _reply_detail(address: str, args: Any) -> str:
    """The DETAIL of one incoming OSC entry (e39s05).

    The ADDRESS decides, not the payload shape: the state table and a thumbnail
    are reported by SIZE whatever their length (a short JSON is still a payload
    the Monitor must not retain), while the watch lane keeps its values because
    they are exactly what you debug when a LED stutters.
    """
    if address == "/viosc/replydata" and args:
        return f"state table {len(args[0])} B"
    if address.startswith("/viosc/replythumb/") and args:
        return f"thumb {len(args[0])} B"
    if isinstance(args, (list, tuple)) and len(args) >= 2:
        pairs = [
            f"{args[i]}={float(args[i + 1]):.2f}"
            for i in range(0, len(args) - 1, 2)
            if isinstance(args[i + 1], (int, float))
        ]
        if pairs:
            return " ".join(pairs[:6])
    return iomonitor.summarize_osc_args(args)


def _incoming_kind(address: str) -> str:
    """The monitor kind of an incoming OSC address (e39s05)."""
    if address == "/viosc/replydata":
        return IO_MONITOR_KIND_STATE
    if address.startswith("/viosc/replythumb/"):
        return IO_MONITOR_KIND_THUMBNAIL
    if address.startswith("/viosc/reply/"):
        return IO_MONITOR_KIND_WATCH_REPLY
    return IO_MONITOR_KIND_OSC


def listen_peer() -> str:
    """The 'host:port' the local OSC server listens on (the Monitor's Peer column)."""
    server = state.local_osc_server
    address = getattr(server, "server_address", None)
    if isinstance(address, tuple) and len(address) == 2:
        return f"{address[0]}:{address[1]}"
    return "osc-in"


def incoming_osc_handler(address: str, *args: Any) -> None:
    append_log("IN ", address)
    try:
        iomonitor.record_osc(
            IO_MONITOR_DIRECTION_IN,
            _incoming_kind(address),
            address,
            _reply_detail(address, args),
            listen_peer(),
        )
    except Exception as e:  # observation must never break the receive path
        log_error("OSC monitor", str(e))
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


def size_receive_buffer(sock: Any) -> None:
    """Give a UDP receive socket an explicit kernel buffer (e41s02).

    socketserver inherits the kernel default, which is small for a burst of
    datagrams — a state broadcast arriving together with a burst of monitor and
    watch replies. Best-effort on purpose: a kernel cap or a platform refusing
    the option must never stop the receiver (Defensive Code).
    """
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)


def next_thumb_frame_index(received: int, frames_at_last_request: int) -> int | None:
    """The next thumbnail frame index to ask for, or None to stop (e41s02).

    viOSC answers a digit index with ONE blob, so walking the indices keeps at
    most one thumbnail datagram in flight instead of the back-to-back burst an
    `all` request produces. The walk is bounded by the per-media frame budget and
    is progress-based:

    - nothing received yet -> keep asking frame 0, which keeps the e10s04
      retry/failure accounting exactly as it was;
    - the frame count grew since the last request -> ask for the next index;
    - it did NOT grow -> the daemon has no more frames for this source (an
      image), so stop instead of polling a non-existent index forever.
    """
    if received >= THUMB_REQUESTS_PER_SOURCE:
        return None
    if received == 0:
        return 0
    if received == frames_at_last_request:
        return None
    return received


class ViseqOSCUDPServer(osc_server.ThreadingOSCUDPServer):
    """OSC receiver with a recv buffer large enough for full thumbnail blobs.

    socketserver.UDPServer defaults max_packet_size to 8192, which truncates
    thumbnail datagrams larger than 8 KB before python-osc can parse them
    (BUG-2026-08-27T201742); the buffer must fit the largest accepted blob plus
    the OSC header (address + typetag + size + padding) margin.
    """

    max_packet_size = MAX_THUMBNAIL_BLOB_BYTES + 4096


# e40s03: OSC OUTPUT DESTINATIONS — an opt-in third-party egress.
# One cached SimpleUDPClient per (host, port) so a Mapping never recreates a socket
# per emission; a destination is validated before any send (address must be an
# OSC path, port in range, host non-empty). The viOSC client above is untouched:
# /vimix traffic never goes through here.

_mapping_clients: dict[tuple[str, int], Any] = {}


_OSC_KIND_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("/vimix/", IO_MONITOR_KIND_VIMIX),
    ("/viosc/watch/", IO_MONITOR_KIND_WATCH),
    ("/viosc/sync/", IO_MONITOR_KIND_SYNC),
    ("/viosc/monitor", IO_MONITOR_KIND_MONITOR),
    ("/viosc/", IO_MONITOR_KIND_VIOSC),
)


def kind_of(address: str) -> str:
    """The monitor kind of an outgoing OSC address (e39s05).

    The address says WHAT the message is; the kind says WHY it exists, so the
    chatty or instrumental senders (fade, sequencer, cue, a third-party
    Destination) pass their own kind explicitly instead of relying on this table.
    """
    for prefix, kind in _OSC_KIND_BY_PREFIX:
        if address.startswith(prefix):
            return kind
    return IO_MONITOR_KIND_OSC


def viosc_peer() -> str:
    """The 'host:port' of the viOSC client actually in use (the Monitor's Peer column)."""
    client = state.viosc_client or osc_client
    peer = getattr(client, "_peer", None)
    if peer:
        return str(peer)
    host = getattr(client, "_address", None) or VIOSC_IP
    port = getattr(client, "_port", None) or VIOSC_PORT
    return f"{host}:{port}"


# e39s05: the sender context — the chatty loops (fade, sequencer, cue) name
# themselves for the duration of a send, without changing any call signature:
# a fake client in the tests keeps working untouched.
_current_kind: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "osc_send_kind", default=None
)


@contextlib.contextmanager
def sent_by(kind: str) -> Iterator[None]:
    """Tag the OSC messages sent inside this block with a kind (e39s05).

    The address says WHAT a message is; the kind says WHY it exists. For the
    address-derived traffic the table in ``kind_of`` is enough, but a fade, a
    sequencer step and a cue row all write ``/vimix/...``: only the caller knows
    which one it is. Context-local, so the sequencer thread and the main-loop
    fade tick never leak into each other.
    """
    token = _current_kind.set(str(kind))
    try:
        yield
    finally:
        _current_kind.reset(token)


class ObservedClient:
    """A UDP client whose every send is reported to the I/O Monitor (e39s05).

    The wrapper is transparent: attribute access delegates to the wrapped client
    (so the tests' recording fakes keep exposing ``.messages``), and nothing on
    the wire changes — ``send_message`` forwards the arguments UNTOUCHED, so every
    message stays byte-identical. This is the one place an outgoing OSC message
    leaves viseq, which is what makes the Monitor complete without touching the
    26 call sites.
    """

    def __init__(self, client: Any, peer: str) -> None:
        self._client = client
        self._peer = str(peer)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def send_message(self, address: str, args: Any = None) -> None:
        """Send one OSC message and record it (the payload itself is not kept)."""
        self._client.send_message(address, args)
        iomonitor.record_osc(
            IO_MONITOR_DIRECTION_OUT,
            _current_kind.get() or kind_of(address),
            address,
            iomonitor.summarize_osc_args(args),
            self._peer,
        )


def observe_client(client: Any, peer: str) -> ObservedClient:
    """Wrap a UDP client so its sends reach the I/O Monitor (e39s05)."""
    if isinstance(client, ObservedClient):
        return client
    return ObservedClient(client, peer)


class VioscClientRouter:
    """The module-level client: routes each send to the client actually in use.

    BUG-2026-09-13T135000: every sender writes ``osc_client.send_message(...)``,
    while the endpoint the user configures lives in ``state.viosc_client`` (set by
    ``connect_osc_client`` from the persisted endpoints). The module client used
    to be bound to the VIOSC_IP/VIOSC_PORT constants forever, so on a rig with a
    non-default host/port the sequencer, the fades, the cue rows and the Mapper
    values went to the WRONG address while the e40 state lane (which reads
    ``state.viosc_client`` directly) worked — the most confusing symptom possible.

    The router resolves at CALL time, so no call site changes and the wired
    endpoint is a single source of truth. It is transparent: attribute access
    delegates to the active client (a test that reads ``osc_client.messages``
    follows whichever client is in use).
    """

    def __init__(self, default: Any) -> None:
        self._default = default

    def active(self) -> Any:
        """The client in use: the configured one when connected, else the default."""
        return state.viosc_client or self._default

    def __getattr__(self, name: str) -> Any:
        return getattr(self.active(), name)

    def send_message(self, address: str, args: Any = None) -> Any:
        return self.active().send_message(address, args)


# The module client: a constant-endpoint default for the case where the
# composition root has not connected a configured one yet.
osc_client = VioscClientRouter(
    observe_client(udp_client.SimpleUDPClient(VIOSC_IP, VIOSC_PORT), f"{VIOSC_IP}:{VIOSC_PORT}")
)


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
    if not 1 <= port <= MAPPING_OSC_MAX_PORT:
        return f"OSC port {port} out of range 1..{MAPPING_OSC_MAX_PORT}"
    address = str(spec.get("address") or "").strip()
    if not address.startswith("/") or len(address) < 2:
        return f"OSC address {address!r} must start with '/'"
    return None


def _client_for(host: str, port: int) -> Any:
    """The cached UDP client of one (host, port) destination (e40s03)."""
    key = (str(host), int(port))
    client = _mapping_clients.get(key)
    if client is None:
        client = udp_client.SimpleUDPClient(key[0], key[1])
        _mapping_clients[key] = client
    return client


def send_mapping_osc(spec: Any, value: float) -> bool:
    """Send one value to a Mapping's OSC destination; False on an invalid spec (e40s03).

    Best-effort and never raising into the main loop: a bad spec or a failing
    send is logged and returns False (the viOSC client and the /vimix path are
    untouched).
    """
    error = validate_destination(spec)
    if error is not None:
        log_error("OSC", f"mapping destination: {error}")
        return False
    address = str(spec["address"])
    try:
        client = _client_for(str(spec["host"]), int(spec["port"]))
        if not isinstance(client, ObservedClient):
            client = observe_client(client, f"{spec['host']}:{spec['port']}")
            _mapping_clients[(str(spec["host"]), int(spec["port"]))] = client
        with sent_by(IO_MONITOR_KIND_DESTINATION):
            client.send_message(address, [float(value)])
        append_log("OUT", f"{address} [{float(value):.2f}] (mapping)")
        return True
    except Exception as e:
        log_error("OSC", f"mapping destination {address}: {e}")
        return False
