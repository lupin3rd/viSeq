"""Route engine for viseq (e40s01) — State Origins, dead-reckoning, coalescing.

Dpg-free (HIGH-1) and state-free: the caller passes the live properties and a
per-Route bookkeeping dict, so every function here is pure and headless-testable.
The Mapper window and the main-loop tick stay in the composition root.

Transport coalescing (ADR-route-model, decision 2): one subscription per
``(source, property)`` serves every Route that reads it, so a Get line requests
the union of its properties in a single message.
"""

from collections.abc import Callable
from typing import Any

from viseqapp import mapper
from viseqapp.constants import ORIGIN_STATE, ROUTE_RESYNC_EPSILON


def state_subscriptions(routes: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The source -> sorted read-properties union the transport must subscribe to.

    Only ENABLED State Routes count (a disabled Route must not keep asking viOSC
    for values nobody emits).
    """
    out: dict[str, set[str]] = {}
    for route in routes:
        if mapper.origin_of(route) != ORIGIN_STATE or not route.get("enabled"):
            continue
        target = str(route.get("target_id") or "")
        if target:
            out.setdefault(target, set()).add(str(route["property"]))
    return {target: sorted(props) for target, props in out.items()}


def route_raw_value(
    route: dict[str, Any], props: dict[str, Any], book: dict[int, dict[str, Any]], now: float
) -> float | None:
    """The current raw Origin value of a State Route, dead-reckoned (e40s01).

    ``book`` is the per-Route runtime memory ``{route_id: {"real", "real_ts"}}``:
    a live value that CHANGED resyncs the memory (a real broadcast arrived);
    between refreshes a derivable property is extrapolated by ``mapper.dead_reckon``.
    Returns None when the property is absent from the state.
    """
    prop = str(route["property"])
    raw = props.get(prop)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    entry = book.setdefault(int(route["id"]), {"real": None, "real_ts": now})
    if entry["real"] is None or abs(float(raw) - float(entry["real"])) > ROUTE_RESYNC_EPSILON:
        entry["real"] = float(raw)
        entry["real_ts"] = float(now)
    elapsed = float(now) - float(entry["real_ts"])
    return mapper.dead_reckon(route, float(entry["real"]), elapsed, props)


def refresh_state_routes(
    routes: list[dict[str, Any]],
    props_lookup: Callable[[str], dict[str, Any] | None],
    book: dict[int, dict[str, Any]],
    now: float,
) -> list[tuple[dict[str, Any], float]]:
    """(route, raw_value) for every enabled State Route with a live value (e40s01).

    Pure over the passed lookups/book: the caller owns ``state`` and decides what
    to do with each value (emit, display, log).
    """
    out: list[tuple[dict[str, Any], float]] = []
    for route in routes:
        if mapper.origin_of(route) != ORIGIN_STATE or not route.get("enabled"):
            continue
        props = props_lookup(str(route.get("target_id") or ""))
        if not props:
            continue
        raw = route_raw_value(route, props, book, now)
        if raw is not None:
            out.append((route, raw))
    return out


def plan_emissions(
    routes: list[dict[str, Any]],
    props_lookup: Callable[[str], dict[str, Any] | None],
    book: dict[int, dict[str, Any]],
    last_values: dict[int, float],
    now: float,
) -> list[tuple[dict[str, Any], float]]:
    """(route, destination_value) for every enabled Route whose value changed.

    Folds the state refresh, the pure Destination remap and the per-Destination
    epsilon dedupe, updating ``last_values`` in place. The caller performs the
    I/O: the engine never talks to a device (HIGH-1).
    """
    out: list[tuple[dict[str, Any], float]] = []
    for route, raw in refresh_state_routes(routes, props_lookup, book, now):
        value = mapper.route_output_value(route, raw)
        route_id = int(route["id"])
        previous = last_values.get(route_id)
        epsilon = mapper.route_emit_epsilon(mapper.destination_of(route))
        if previous is not None and abs(value - previous) < epsilon:
            continue
        last_values[route_id] = value
        out.append((route, value))
    return out


def prune_book(
    book: dict[int, dict[str, Any]], last_values: dict[int, float], live_ids: set[int]
) -> None:
    """Drop the runtime memory of Routes that no longer exist (e40s01)."""
    for store in (book, last_values):
        for route_id in [rid for rid in store if rid not in live_ids]:
            del store[route_id]
