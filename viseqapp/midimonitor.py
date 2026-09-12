"""Live MIDI-input monitor for viseq (e39s01).

Diagnostic only: records every incoming MIDI message (as parsed by
``viseqapp.midi``) plus the OUTCOME of its resolution and a per-control
calibration envelope, so the composition root can render a live stream and a
controls table. The monitor never sends, never persists and never alters
dispatch — it observes. Dpg-free (HIGH-1) and bounded by the named caps in
``constants`` so a spinning wheel cannot grow memory; coalescing is for display
only: one row per ``(port, type, channel, number)`` with last/min/max/count.

A ``revision`` counter lets the UI update its text blocks only when something
actually changed (the ``osc_log_text`` refresh pattern).
"""

import time
from typing import Any

from viseqapp.constants import (
    MIDI_MONITOR_CONTROL_LIMIT,
    MIDI_MONITOR_NEUTRAL_CENTRE,
    MIDI_MONITOR_STREAM_LIMIT,
    MIDI_MONITOR_VALUE_MAX,
)

__all__ = [
    "centre_offset",
    "clear",
    "control_key",
    "format_controls",
    "format_report",
    "format_stream",
    "normalize_value",
    "record_rx",
    "reset_controls",
    "revision",
    "snapshot_controls",
    "snapshot_stream",
    "stats",
]

# Oldest-first; newest entries are at the end (snapshots reverse for display).
_stream: list[dict[str, Any]] = []
_controls: dict[tuple[str, str, int, int], dict[str, Any]] = {}
_revision = 0


def control_key(port: str, msg_type: str, channel: int, number: int) -> tuple[str, str, int, int]:
    """The coalescing key of one physical control (one CC on one channel/port)."""
    return (str(port), str(msg_type), int(channel), int(number))


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
    """Record one incoming message + its resolution outcome (main thread).

    Appends a bounded stream entry and coalesces the message into its control
    row (last value, min/max envelope, count, last outcome, age). ``now`` is
    injectable for deterministic tests.
    """
    global _revision
    ts = time.time() if now is None else float(now)
    key = control_key(port, msg_type, channel, number)
    raw = float(value)
    entry: dict[str, Any] = {
        "ts": ts,
        "port": key[0],
        "type": key[1],
        "channel": key[2],
        "number": key[3],
        "value": raw,
        "normalized": normalize_value(raw),
        "outcome": str(outcome),
        "detail": str(detail),
    }
    _stream.append(entry)
    if len(_stream) > MIDI_MONITOR_STREAM_LIMIT:
        del _stream[: len(_stream) - MIDI_MONITOR_STREAM_LIMIT]
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


def _evict_controls() -> None:
    """Keep the control table bounded by dropping the least recently seen row."""
    while len(_controls) > MIDI_MONITOR_CONTROL_LIMIT:
        oldest = min(_controls, key=lambda k: float(_controls[k]["last_ts"]))
        del _controls[oldest]


def snapshot_stream(
    port: str | None = None, control: tuple[str, str, int, int] | None = None
) -> list[dict[str, Any]]:
    """Newest-first stream entries, optionally filtered by port and/or control."""
    return [dict(entry) for entry in reversed(_stream) if _entry_matches(entry, port, control)]


def _entry_matches(
    entry: dict[str, Any], port: str | None, control: tuple[str, str, int, int] | None
) -> bool:
    """True when a stream entry passes the active port/control filters."""
    if port is not None and entry["port"] != port:
        return False
    if control is None:
        return True
    return control_key(entry["port"], entry["type"], entry["channel"], entry["number"]) == control


def snapshot_controls(port: str | None = None) -> list[dict[str, Any]]:
    """Control rows, most recently active first, with the centre offset derived."""
    rows = []
    for key, row in _controls.items():
        if port is not None and key[0] != port:
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
        return "No MIDI input yet."
    lines = ["TIME      PORT            TYPE  CH  NUM   RAW  NORM   OUTCOME  DETAIL"]
    for entry in entries:
        lines.append(
            f"{time.strftime('%H:%M:%S', time.localtime(float(entry['ts'])))}  "
            f"{str(entry['port'])[:15]:<15} "
            f"{str(entry['type'])[:4]:<4}  "
            f"{int(entry['channel']):>2}  "
            f"{int(entry['number']):>3}  "
            f"{float(entry['value']):>4.0f}  "
            f"{float(entry['normalized']):>5.2f}  "
            f"{entry['outcome']!s:<7}  "
            f"{entry['detail']}"
        )
    return "\n".join(lines)


def format_controls(rows: list[dict[str, Any]]) -> str:
    """The controls pane text: one aligned row per control (ASCII only)."""
    if not rows:
        return "No control seen yet."
    lines = ["PORT            TYPE  CH  NUM   LAST   NORM   MIN..MAX      OFF   CNT   AGE  OUTCOME"]
    for row in rows:
        lines.append(
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
    return (
        f"viSeq MIDI Monitor report - {stamp}\n"
        f"messages: {len(_stream)}  controls: {len(_controls)}\n\n"
        f"--- STREAM (newest first) ---\n{stream_text}\n\n"
        f"--- CONTROLS ---\n{controls_text}\n"
    )
