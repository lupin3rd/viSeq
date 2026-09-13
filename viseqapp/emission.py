"""Mapping engine for viseq (e40s01) — State Origins, dead-reckoning, coalescing.

Dpg-free (HIGH-1) and state-free: the caller passes the live properties and a
per-Mapping bookkeeping dict, so every function here is pure and headless-testable.
The Mapper window and the main-loop tick stay in the composition root.

Transport coalescing (ADR-mapping-model, decision 2): one subscription per
``(source, property)`` serves every Mapping that reads it, so a State band requests
the union of its properties in a single message.
"""

from collections.abc import Callable
from typing import Any

from viseqapp import mapper
from viseqapp.constants import (
    CLOCK_DEFAULT,
    MAPPING_DEFAULT_CADENCE_MS,
    MAPPING_RESYNC_EPSILON,
    ORIGIN_CLOCK,
    ORIGIN_CONST,
    ORIGIN_STATE,
)


def state_subscriptions(mappings: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The source -> sorted read-properties union the transport must subscribe to.

    Only ENABLED State Mappings count (a disabled Mapping must not keep asking viOSC
    for values nobody emits).
    """
    out: dict[str, set[str]] = {}
    for mapping in mappings:
        if mapper.origin_of(mapping) != ORIGIN_STATE or not mapping.get("enabled"):
            continue
        target = str(mapping.get("target_id") or "")
        if target:
            out.setdefault(target, set()).add(str(mapping["property"]))
    return {target: sorted(props) for target, props in out.items()}


def state_watch_plan(mappings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The per-source watch plan of the enabled State Mappings (e40s06).

    {source: {"props": sorted properties, "cadence_ms": the FASTEST cadence of
    that source's Mappings}} — one watch entry per source, so several Mappings
    reading one source share a single subscription and the fastest Mapping wins
    (ADR decisions 2-3).
    """
    plan: dict[str, dict[str, Any]] = {}
    for mapping in mappings:
        if mapper.origin_of(mapping) != ORIGIN_STATE or not mapping.get("enabled"):
            continue
        target = str(mapping.get("target_id") or "")
        if not target:
            continue
        cadence = int(mapping.get("cadence") or MAPPING_DEFAULT_CADENCE_MS)
        entry = plan.setdefault(target, {"props": [], "cadence_ms": cadence})
        prop = str(mapping["property"])
        if prop not in entry["props"]:
            entry["props"].append(prop)
        entry["cadence_ms"] = min(int(entry["cadence_ms"]), cadence)
    for entry in plan.values():
        entry["props"].sort()
    return plan


def mapping_raw_value(
    mapping: dict[str, Any], props: dict[str, Any], book: dict[int, dict[str, Any]], now: float
) -> float | None:
    """The current raw Origin value of a State Mapping, dead-reckoned (e40s01).

    ``book`` is the per-Mapping runtime memory ``{mapping_id: {"real", "real_ts"}}``:
    a live value that CHANGED resyncs the memory (a real broadcast arrived);
    between refreshes a derivable property is extrapolated by ``mapper.dead_reckon``.
    Returns None when the property is absent from the state.
    """
    prop = str(mapping["property"])
    raw = props.get(prop)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    entry = book.setdefault(int(mapping["id"]), {"real": None, "real_ts": now})
    if entry["real"] is None or abs(float(raw) - float(entry["real"])) > MAPPING_RESYNC_EPSILON:
        entry["real"] = float(raw)
        entry["real_ts"] = float(now)
    elapsed = float(now) - float(entry["real_ts"])
    return mapper.dead_reckon(mapping, float(entry["real"]), elapsed, props)


def refresh_state_mappings(
    mappings: list[dict[str, Any]],
    props_lookup: Callable[[str], dict[str, Any] | None],
    book: dict[int, dict[str, Any]],
    now: float,
) -> list[tuple[dict[str, Any], float]]:
    """(mapping, raw_value) for every enabled State Mapping with a live value (e40s01).

    Pure over the passed lookups/book: the caller owns ``state`` and decides what
    to do with each value (emit, display, log).
    """
    out: list[tuple[dict[str, Any], float]] = []
    for mapping in mappings:
        if mapper.origin_of(mapping) != ORIGIN_STATE or not mapping.get("enabled"):
            continue
        props = props_lookup(str(mapping.get("target_id") or ""))
        if not props:
            continue
        raw = mapping_raw_value(mapping, props, book, now)
        if raw is not None:
            out.append((mapping, raw))
    return out


def origin_raw_value(
    mapping: dict[str, Any],
    props_lookup: Callable[[str], dict[str, Any] | None],
    book: dict[int, dict[str, Any]],
    now: float,
    clock_lookup: Callable[[str], float | None] | None = None,
) -> float | None:
    """The current raw Origin value of ANY Mapping (e40s02).

    State -> the live source property (dead-reckoned); Clock -> the live
    transport/app quantity from ``clock_lookup``; Constant -> the fixed
    ``origin_spec['value']``. None when the value is unavailable (no state, no
    clock lookup, malformed constant).
    """
    origin = mapper.origin_of(mapping)
    if origin == ORIGIN_STATE:
        props = props_lookup(str(mapping.get("target_id") or ""))
        if not props:
            return None
        return mapping_raw_value(mapping, props, book, now)
    if origin == ORIGIN_CLOCK:
        if clock_lookup is None:
            return None
        spec = mapping.get("origin_spec") or {}
        return clock_lookup(str(spec.get("clock") or CLOCK_DEFAULT))
    if origin == ORIGIN_CONST:
        value = (mapping.get("origin_spec") or {}).get("value", 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)
    return None


def plan_emissions(
    mappings: list[dict[str, Any]],
    props_lookup: Callable[[str], dict[str, Any] | None],
    book: dict[int, dict[str, Any]],
    last_values: dict[int, float],
    now: float,
    clock_lookup: Callable[[str], float | None] | None = None,
) -> list[tuple[dict[str, Any], float]]:
    """(mapping, destination_value) for every enabled Mapping whose value changed.

    Folds the Origin read (State/Clock/Constant), the pure Destination rescale and
    the per-Destination epsilon dedupe, updating ``last_values`` in place. The
    caller performs the I/O: the engine never talks to a device (HIGH-1).
    """
    out: list[tuple[dict[str, Any], float]] = []
    for mapping in mappings:
        if not mapping.get("enabled"):
            continue
        raw = origin_raw_value(mapping, props_lookup, book, now, clock_lookup)
        if raw is None:
            continue
        value = mapper.mapping_output_value(mapping, raw)
        mapping_id = int(mapping["id"])
        previous = last_values.get(mapping_id)
        epsilon = mapper.mapping_emit_epsilon(mapper.destination_of(mapping))
        if previous is not None and abs(value - previous) < epsilon:
            continue
        last_values[mapping_id] = value
        out.append((mapping, value))
    return out


def prune_book(
    book: dict[int, dict[str, Any]], last_values: dict[int, float], live_ids: set[int]
) -> None:
    """Drop the runtime memory of Mappings that no longer exist (e40s01)."""
    for store in (book, last_values):
        for mapping_id in [rid for rid in store if rid not in live_ids]:
            del store[mapping_id]
