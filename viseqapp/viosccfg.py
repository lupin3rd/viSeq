"""viOSC config client (e58s03) — the HTTP /config surface from viseq.

DPG-FREE by construction (HIGH-1): pure urllib transport + pairing headers, no
dpg, no app state. The host/port are passed by the caller (the same machine-A
data-plane endpoint used for preview/thumbnails), so this module stays a pure
transport like dataplane.py.

Never raises: a down machine, a 401 (unpaired), or a malformed reply all leave
the caller with ``(None, reason)`` instead of an exception, exactly like the
rest of the data-plane client.
"""

import json
import urllib.error
import urllib.request
from typing import Any

from viseqapp import pairing
from viseqapp.constants import DATA_PLANE_TIMEOUT

CONFIG_PATH = "/config"
RESTART_PATH = "/restart"
NO_ENDPOINT = "viOSC endpoint not configured"


def config_url(host: str, port: int) -> str:
    """URL of the daemon's config resource."""
    return f"http://{host}:{port}{CONFIG_PATH}"


def restart_url(host: str, port: int) -> str:
    """URL of the daemon's restart trigger."""
    return f"http://{host}:{port}{RESTART_PATH}"


def fetch_config(host: str, port: int) -> tuple[dict | None, str | None]:
    """GET /config -> (payload, None) or (None, reason). Never raises."""
    if not host or not port:
        return None, NO_ENDPOINT
    try:
        request = urllib.request.Request(config_url(host, port), headers=pairing.http_headers())
        with urllib.request.urlopen(request, timeout=DATA_PLANE_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return None, "invalid config payload"
        return payload, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return None, str(e)


def apply_config(host: str, port: int, changes: dict) -> tuple[dict | None, str | None]:
    """POST /config -> (report, None) or (None, reason). Never raises.

    A 400 response still carries the per-field report (invalid/unknown/
    local_only), so it is surfaced as a report, not folded into a generic error.
    """
    if not host or not port:
        return None, NO_ENDPOINT
    body = json.dumps(changes).encode("utf-8")
    request = urllib.request.Request(
        config_url(host, port),
        data=body,
        headers={**pairing.http_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=DATA_PLANE_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return None, "invalid config payload"
        return payload, None
    except urllib.error.HTTPError as e:
        if e.code == 400:
            try:
                payload = json.loads(e.read().decode("utf-8"))
                if isinstance(payload, dict):
                    return payload, None
            except (json.JSONDecodeError, OSError):
                pass
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return None, str(e)


def trigger_restart(host: str, port: int) -> tuple[bool, str | None]:
    """POST /restart -> (True, None) or (False, reason). Never raises."""
    if not host or not port:
        return False, NO_ENDPOINT
    request = urllib.request.Request(
        restart_url(host, port), data=b"", headers=pairing.http_headers(), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=DATA_PLANE_TIMEOUT) as resp:
            return resp.status == 200, None if resp.status == 200 else f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, str(e)


def config_diff(original: dict, edited: dict) -> dict:
    """The changed fields only — the POST /config body (e58s03)."""
    return {key: value for key, value in edited.items() if original.get(key) != value}


def parse_field(raw: str, original: Any) -> Any:
    """Parse a UI string into the type of the current value (e58s03).

    The viOSC schema owns the types; viseq only mirrors them, so the original
    effective value decides how an edited field is coerced.
    """
    text = raw.strip()
    if isinstance(original, bool):
        return text.lower() in ("true", "1", "yes", "on")
    if isinstance(original, int):
        return int(text)
    if isinstance(original, float):
        return float(text)
    if isinstance(original, list):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw


def report_lines(report: dict) -> list[str]:
    """Human-readable lines for an apply report — errors inline (e58s03)."""
    lines = []
    for key, value in (report.get("invalid") or {}).items():
        lines.append(f"{key}: invalid value {value!r}")
    for key in report.get("unknown") or []:
        lines.append(f"{key}: unknown setting (ignored)")
    for key in report.get("local_only") or []:
        lines.append(f"{key}: local-only — set it on the viOSC machine")
    staged = report.get("staged") or []
    if staged:
        lines.append("restart required for: " + ", ".join(staged))
    return lines


def has_problems(report: dict) -> bool:
    """True when a report carries any field the daemon rejected (e58s03)."""
    return bool(report.get("invalid") or report.get("unknown") or report.get("local_only"))


def link_fields(listen_ip: str, listen_port: int) -> dict:
    """viOSC's push destination, derived from viseq's listen endpoint (e58s03).

    ``ui_ip``/``reply_port`` are where viOSC sends state back; they MUST match
    viseq's listening endpoint, so viSeq derives them instead of letting them be
    set independently on viOSC (the collapsed link config).
    """
    return {"ui_ip": str(listen_ip), "reply_port": int(listen_port)}


def format_value(value: Any) -> str:
    """The UI text for a config value (list fields join with commas) (e58s03)."""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)
