"""viseq-side link pairing client (e42s02).

viOSC is paired by default: an unauthenticated request to its :8686 data plane
answers 401 and an unbound peer's OSC never reaches vimix. This module is the
client half — where the token lives (in memory only, never on disk), the
``/auth`` code exchange, and the headers the two transports must carry: ordinary
``urllib`` requests and the PyAV HTTP range stream (which takes headers as one
CRLF-terminated option string).

DPG-FREE by construction (HIGH-1): the UI thread exchanges the code and sets the
token; worker threads only read it.
"""

import json
import urllib.request

from viseqapp.constants import DATA_PLANE_TIMEOUT

AUTH_PATH = "/auth"
BEARER_PREFIX = "Bearer "

_token = ""


def get_token() -> str:
    """The in-memory bearer token (empty when not paired)."""
    return _token


def set_token(token: object) -> None:
    """Store a bearer token for every subsequent data-plane request."""
    global _token
    _token = str(token or "")


def clear_token() -> None:
    """Forget the token (a failed re-pairing, or an explicit disconnect)."""
    global _token
    _token = ""


def http_headers() -> dict[str, str]:
    """Headers for a urllib request: the bearer token, or none when unpaired."""
    return {"Authorization": BEARER_PREFIX + _token} if _token else {}


def ffmpeg_headers() -> str | None:
    """The token header in the CRLF form FFmpeg/PyAV accepts, or None."""
    return f"Authorization: {BEARER_PREFIX}{_token}\r\n" if _token else None


def auth_url(host: str, port: int) -> str:
    """URL of the daemon's code-exchange endpoint."""
    return f"http://{host}:{port}{AUTH_PATH}"


def authenticate(
    host: str, port: int, code: object, timeout: float = DATA_PLANE_TIMEOUT
) -> str | None:
    """Exchange the 4-digit code for a bearer token; None on any failure.

    Never raises: an older daemon without ``/auth`` answers 404, a down machine
    gives a connection error, and both must leave viseq usable (unpaired) rather
    than crashing the caller. A successful token is stored in memory.
    """
    if not host or not port:
        return None
    try:
        body = json.dumps({"code": str(code)}).encode("utf-8")
        request = urllib.request.Request(
            auth_url(host, port),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
        token = str(payload.get("token") or "")
    except Exception:
        return None
    if not token:
        return None
    set_token(token)
    return token
