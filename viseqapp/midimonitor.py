"""Live MIDI I/O monitor for viseq (e39s01, e39s05).

Diagnostic only: records every INCOMING MIDI message (as parsed by
``viseqapp.midi``) with the OUTCOME of its resolution, and every OUTGOING one
(the Mapping Destination, the grid LEDs, the controller setup) with the sender
that produced it, plus a per-control calibration envelope. The composition root
renders a live stream and a controls table. The monitor never sends, never
persists and never alters dispatch — it observes. Dpg-free (HIGH-1) and bounded
by the named caps in ``constants`` so a spinning wheel cannot grow memory;
coalescing is for display only: one row per
``(port, type, channel, number, direction)`` with last/min/max/count — the
DIRECTION is part of the key, so an outgoing LED note and an incoming note on
the same number never share an envelope.

Every entry carries a common shape (``ts``, ``transport``, ``direction``,
``kind``, ``port``, ``type``, ``channel``, ``number``, ``value``,
``normalized``, ``outcome``, ``detail``) so the OSC side can join in without a
second engine (e39s05 task 2).

A ``revision`` counter lets the UI update its text blocks only when something
actually changed (the ``osc_log_text`` refresh pattern).
"""

import time
from typing import Any

from viseqapp.constants import (
    MIDI_MONITOR_CONTROL_LIMIT,
    MIDI_MONITOR_DIRECTION_IN,
    MIDI_MONITOR_DIRECTION_OUT,
    MIDI_MONITOR_KIND_MIDI_IN,
    MIDI_MONITOR_KIND_MIDI_OUT,
    MIDI_MONITOR_NEUTRAL_CENTRE,
    MIDI_MONITOR_OUTCOME_RECV,
    MIDI_MONITOR_OUTCOME_SENT,
    MIDI_MONITOR_STREAM_LIMIT,
    MIDI_MONITOR_SUMMARY_TEXT_MAX,
    MIDI_MONITOR_TRANSPORT_MIDI,
    MIDI_MONITOR_TRANSPORT_OSC,
    MIDI_MONITOR_VALUE_MAX,
)

__all__ = [
    "centre_offset",
    "clear",
    "control_id",
    "control_key",
    "format_controls",
    "format_report",
    "format_stream",
    "normalize_value",
    "peers",
    "record_osc",
    "record_rx",
    "record_tx",
    "reset_controls",
    "revision",
    "snapshot_controls",
    "snapshot_stream",
    "stats",
    "summarize_osc_args",
]

# Oldest-first; newest entries are at the end (snapshots reverse for display).
_stream: list[dict[str, Any]] = []
_controls: dict[tuple[str, str, int, int, str], dict[str, Any]] = {}
_revision = 0


def control_key(
    port: str,
    msg_type: str,
    channel: int,
    number: int,
    direction: str = MIDI_MONITOR_DIRECTION_IN,
) -> tuple[str, str, int, int, str]:
    """The coalescing key of one control (one CC/note on one channel/port).

    e39s05: the DIRECTION belongs to the key — an outgoing LED note and an
    incoming note on the same number are different things, and sharing an
    envelope between them would make the calibration table meaningless.
    """
    return (str(port), str(msg_type), int(channel), int(number), str(direction))


def summarize_osc_args(args: Any) -> str:
    """A bounded, human summary of an OSC payload (e39s05).

    The payload itself is NEVER stored: a short scalar list is shown (that IS the
    interesting bit), a long text/blob collapses to its size, and a mixed list to
    its arity. This is what keeps a per-frame fade from ballooning memory.
    """
    if args is None:
        return "no args"
    if isinstance(args, (bool, int, float)):
        return f"{float(args):.3f}"
    if isinstance(args, (bytes, bytearray)):
        return f"blob {len(args)} B"
    if isinstance(args, str):
        return args if len(args) <= MIDI_MONITOR_SUMMARY_TEXT_MAX else f"text {len(args)} B"
    if isinstance(args, (list, tuple)):
        if not args:
            return "no args"
        if all(isinstance(a, (bool, int, float)) for a in args):
            if len(args) <= 4:
                return ", ".join(f"{float(a):.3f}" for a in args)
            return f"{len(args)} values"
        return f"{len(args)} args"
    return str(type(args).__name__)


def record_osc(
    direction: str,
    kind: str,
    address: str,
    detail: str = "",
    peer: str = "",
    now: float | None = None,
) -> None:
    """Record one OSC entry in the stream (e39s05) — payload never retained.

    OSC does NOT feed the coalesced control table: that is the MIDI learn and
    calibration instrument (e39s02/e39s03), and an OSC address is not a control.
    The chatty senders are handled by the stream's consecutive-message collapse
    (see ``_append_stream``): a fade at 60 Hz becomes one row with a count.
    """
    ts = time.time() if now is None else float(now)
    _append_stream(
        {
            "ts": ts,
            "transport": MIDI_MONITOR_TRANSPORT_OSC,
            "direction": str(direction),
            "kind": str(kind),
            "port": str(peer),
            "type": "osc",
            "channel": 0,
            "number": 0,
            "value": 0.0,
            "normalized": 0.0,
            "outcome": MIDI_MONITOR_OUTCOME_RECV
            if direction == MIDI_MONITOR_DIRECTION_IN
            else MIDI_MONITOR_OUTCOME_SENT,
            "detail": f"{address}{f' ({detail})' if detail else ''}",
            "subject": str(address),
        }
    )


def control_id(control: tuple[str, str, int, int, str]) -> str:
    """The copyable id of one control ('PORT type ch N') — a Binding is about input.

    e39s05: the key carries the direction, but the id does not: a physical control
    is an input by definition, so a trailing 'in' would just be noise in a ticket.
    """
    return " ".join(str(part) for part in control[:4])


def normalize_value(value: float) -> float:
    """The raw MIDI value on the app-wide 0..1 scale."""
    return float(value) / MIDI_MONITOR_VALUE_MAX


def centre_offset(value: float) -> int:
    """Signed distance of a raw value from the neutral centre (64).

    Positive above the centre, negative below: the readout that exposes a
    wheel's rest position and dead-zone.
    """
    return round(float(value)) - MIDI_MONITOR_NEUTRAL_CENTRE


def revision() -> int:
    """Monotonic counter bumped on every real change (stream, controls, clear)."""
    return _revision


def record_rx(
    port: str,
    msg_type: str,
    channel: int,
    number: int,
    value: float,
    outcome: str,
    detail: str = "",
    now: float | None = None,
) -> None:
    """Record one INCOMING message + its resolution outcome (main thread)."""
    _record(
        MIDI_MONITOR_DIRECTION_IN,
        (port, msg_type, channel, number),
        value,
        MIDI_MONITOR_KIND_MIDI_IN,
        outcome,
        detail,
        now,
    )


def record_tx(
    port: str,
    msg_type: str,
    channel: int,
    number: int,
    value: float,
    detail: str = "",
    now: float | None = None,
) -> None:
    """Record one OUTGOING message (e39s05) — a Mapping CC/note, a grid LED, a sysex.

    No resolution outcome exists on this side: the message left viseq, so the row
    reads SENT and ``detail`` names the sender ('mapping #7 alpha', 'grid led
    r2c3', 'controller setup'). Same bounded stream and coalescing as the RX side.
    """
    _record(
        MIDI_MONITOR_DIRECTION_OUT,
        (port, msg_type, channel, number),
        value,
        MIDI_MONITOR_KIND_MIDI_OUT,
        MIDI_MONITOR_OUTCOME_SENT,
        detail,
        now,
    )


def _record(
    direction: str,
    peer: tuple[str, str, int, int],
    value: float,
    kind: str,
    outcome: str,
    detail: str,
    now: float | None,
) -> None:
    """Append a bounded stream entry and coalesce it into its control row.

    ``now`` is injectable for deterministic tests. Shared by record_rx and
    record_tx so the two directions can never drift in shape or caps.
    """
    global _revision
    port, msg_type, channel, number = peer
    ts = time.time() if now is None else float(now)
    key = control_key(port, msg_type, channel, number, direction)
    raw = float(value)
    entry: dict[str, Any] = {
        "ts": ts,
        "transport": MIDI_MONITOR_TRANSPORT_MIDI,
        "direction": direction,
        "kind": str(kind),
        "port": key[0],
        "type": key[1],
        "channel": key[2],
        "number": key[3],
        "value": raw,
        "normalized": normalize_value(raw),
        "outcome": str(outcome),
        "detail": str(detail),
    }
    entry["subject"] = f"{key[0]} {key[1]} ch{key[2]} #{key[3]}"
    _append_stream(entry)
    row = _controls.get(key)
    if row is None:
        _controls[key] = {
            **entry,
            "min": raw,
            "max": raw,
            "count": 1,
            "first_ts": ts,
            "last_ts": ts,
        }
        _evict_controls()
    else:
        row.update(entry)
        row["min"] = min(float(row["min"]), raw)
        row["max"] = max(float(row["max"]), raw)
        row["count"] = int(row["count"]) + 1
        row["last_ts"] = ts
    _revision += 1


def _append_stream(entry: dict[str, Any]) -> None:
    """Append one entry, capped, collapsing a CONSECUTIVE identical OSC message.

    e39s05: a per-frame fade, a BPM sequencer row and the 2 s state mirror would
    otherwise own the whole stream. Two consecutive OSC entries with the same
    (direction, kind, peer, subject) become ONE row whose count grows, so the pane
    keeps saying "this is happening N times right now" instead of scrolling.
    MIDI entries never collapse (one physical event is one entry, and the
    calibration table already coalesces them per control).
    """
    global _revision
    last = _stream[-1] if _stream else None
    if (
        last is not None
        and entry["transport"] == MIDI_MONITOR_TRANSPORT_OSC
        and last.get("transport") == MIDI_MONITOR_TRANSPORT_OSC
        and (last.get("direction"), last.get("kind"), last.get("port"), last.get("subject"))
        == (entry["direction"], entry["kind"], entry["port"], entry["subject"])
    ):
        last["repeat"] = int(last.get("repeat", 1)) + 1
        last["ts"] = entry["ts"]
        last["value"] = entry["value"]
        last["outcome"] = entry["outcome"]
        _revision += 1
        return
    entry.setdefault("repeat", 1)
    _stream.append(entry)
    if len(_stream) > MIDI_MONITOR_STREAM_LIMIT:
        del _stream[: len(_stream) - MIDI_MONITOR_STREAM_LIMIT]
    _revision += 1


def _evict_controls() -> None:
    """Keep the control table bounded by dropping the least recently seen row."""
    while len(_controls) > MIDI_MONITOR_CONTROL_LIMIT:
        oldest = min(_controls, key=lambda k: float(_controls[k]["last_ts"]))
        del _controls[oldest]


def snapshot_stream(
    port: str | None = None,
    control: tuple[str, str, int, int, str] | None = None,
    direction: str | None = None,
    transport: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-first stream entries, filtered by peer, control, direction, transport."""
    return [
        dict(entry)
        for entry in reversed(_stream)
        if _entry_matches(entry, port, control, direction, transport)
    ]


def peers() -> list[str]:
    """The distinct peers seen so far (MIDI ports and OSC host:port), sorted.

    e39s05: the window's Peer filter lists these beside the configured MIDI ports,
    so an OSC destination you sent to once is selectable without retyping it.
    """
    seen = {str(entry["port"]) for entry in _stream if entry.get("port")}
    return sorted(seen)


def _entry_matches(
    entry: dict[str, Any],
    port: str | None,
    control: tuple[str, str, int, int, str] | None,
    direction: str | None = None,
    transport: str | None = None,
) -> bool:
    """True when a stream entry passes the active peer/control/direction/transport filters."""
    if port is not None and entry["port"] != port:
        return False
    if direction is not None and entry["direction"] != direction:
        return False
    if transport is not None and entry["transport"] != transport:
        return False
    if control is None:
        return True
    return (
        control_key(
            entry["port"], entry["type"], entry["channel"], entry["number"], entry["direction"]
        )
        == control
    )


def snapshot_controls(
    port: str | None = None, direction: str | None = MIDI_MONITOR_DIRECTION_IN
) -> list[dict[str, Any]]:
    """Control rows, most recently active first, with the centre offset derived.

    Defaults to the INCOMING direction: the table is the learn/calibration view
    (e39s02/e39s03), and an outgoing LED is not a control you can learn from.
    Pass ``direction=None`` for both directions.
    """
    rows = []
    for key, row in _controls.items():
        if port is not None and key[0] != port:
            continue
        if direction is not None and key[4] != direction:
            continue
        out = dict(row)
        out["key"] = key
        out["offset"] = centre_offset(float(row["value"]))
        out["age"] = max(0.0, time.time() - float(row["last_ts"]))
        rows.append(out)
    rows.sort(key=lambda r: float(r["last_ts"]), reverse=True)
    return rows


def clear() -> None:
    """Drop both the stream and the control table (Clear button)."""
    global _revision
    _stream.clear()
    _controls.clear()
    _revision += 1


def reset_controls() -> None:
    """Zero the envelope/counters but keep the rows (Reset stats button)."""
    global _revision
    now = time.time()
    for row in _controls.values():
        row["min"] = float(row["value"])
        row["max"] = float(row["value"])
        row["count"] = 1
        row["first_ts"] = now
        row["last_ts"] = now
    _revision += 1


def stats() -> dict[str, int]:
    """Sizes for diagnostics/tests (the caps are module-level constants)."""
    return {
        "stream": len(_stream),
        "controls": len(_controls),
        "revision": _revision,
        "stream_limit": MIDI_MONITOR_STREAM_LIMIT,
        "control_limit": MIDI_MONITOR_CONTROL_LIMIT,
    }


def format_stream(entries: list[dict[str, Any]]) -> str:
    """The stream pane text: one line per message, newest first (ASCII only)."""
    if not entries:
        return "No traffic yet."
    lines = ["TIME      DIR  KIND     PORT            TYPE  CH  NUM   RAW  NORM   OUTCOME  DETAIL"]
    for entry in entries:
        # an OSC entry has no channel/number/value: those columns stay BLANK, so an
        # OSC row does not read as a pile of zeros (e39s05)
        midi_columns = (
            f"{int(entry['channel']):>2}  "
            f"{int(entry['number']):>3}  "
            f"{float(entry['value']):>4.0f}  "
            f"{float(entry['normalized']):>5.2f}  "
            if entry["transport"] == MIDI_MONITOR_TRANSPORT_MIDI
            else f"{'':>2}  {'':>3}  {'':>4}  {'':>5}  "
        )
        lines.append(
            f"{time.strftime('%H:%M:%S', time.localtime(float(entry['ts'])))}  "
            f"{entry['direction']!s:<3}  "
            f"{str(entry['kind'])[:8]:<8} "
            f"{str(entry['port'])[:15]:<15} "
            f"{str(entry['type'])[:4]:<4}  "
            f"{midi_columns}"
            f"{entry['outcome']!s:<7}  "
            f"{entry['detail']}"
            f"{f'  x{entry["repeat"]}' if int(entry.get('repeat', 1)) > 1 else ''}"
        )
    return "\n".join(lines)


def format_controls(rows: list[dict[str, Any]]) -> str:
    """The controls pane text: one aligned row per control (ASCII only)."""
    if not rows:
        return "No control seen yet."
    lines = [
        "DIR  PORT            TYPE  CH  NUM   LAST   NORM   MIN..MAX      OFF   CNT   AGE  OUTCOME"
    ]
    for row in rows:
        lines.append(
            f"{row['direction']!s:<3}  "
            f"{str(row['port'])[:15]:<15} "
            f"{str(row['type'])[:4]:<4}  "
            f"{int(row['channel']):>2}  "
            f"{int(row['number']):>3}  "
            f"{float(row['value']):>4.0f}  "
            f"{float(row['normalized']):>5.2f}  "
            f"{float(row['min']):>4.0f}..{float(row['max']):<4.0f}  "
            f"{int(row['offset']):>+4}  "
            f"{int(row['count']):>4}  "
            f"{float(row['age']):>4.1f}  "
            f"{row['outcome']!s:<7}"
        )
    return "\n".join(lines)


def format_report(stream_text: str, controls_text: str) -> str:
    """The 'Copy report' payload: header + both panes, for pasting in a ticket."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    incoming = sum(1 for e in _stream if e["direction"] == MIDI_MONITOR_DIRECTION_IN)
    return (
        f"viSeq MIDI Monitor report - {stamp}\n"
        f"messages: {len(_stream)} (in {incoming} / out {len(_stream) - incoming})  "
        f"controls: {len(_controls)}\n\n"
        f"--- STREAM (newest first) ---\n{stream_text}\n\n"
        f"--- CONTROLS ---\n{controls_text}\n"
    )
