"""viseq-side /fs client (e43s02).

The File Manager window browses machine A through the viOSC data plane. This
module is the dpg-free transport: it fetches ``/fs/roots`` and ``/fs/list``
carrying the pairing token, with a bounded timeout, and **never raises** — the
window shows a clear offline/forbidden/unpaired state instead of a traceback.

Returns ``(payload, status)``: ``status`` is the HTTP status when the daemon
answered (403 = outside the Media Roots, 401 = unpaired) and ``None`` when it did
not answer at all, so the caller can word the state correctly.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from viseqapp import pairing
from viseqapp.constants import DATA_PLANE_TIMEOUT

FS_ROOTS_PATH = "/fs/roots"
FS_LIST_PATH = "/fs/list"


def roots_url(host: str, port: int) -> str:
    """URL of the configured Media Roots."""
    return f"http://{host}:{port}{FS_ROOTS_PATH}"


def list_url(host: str, port: int, path: str, offset: int = 0) -> str:
    """URL of one directory page (the path is URL-encoded)."""
    query = urllib.parse.urlencode({"path": str(path), "offset": int(offset)})
    return f"http://{host}:{port}{FS_LIST_PATH}?{query}"


def _get(url: str) -> tuple[bytes | None, int | None]:
    """One GET with the pairing headers; never raises."""
    try:
        request = urllib.request.Request(url, headers=pairing.http_headers())
        with urllib.request.urlopen(request, timeout=DATA_PLANE_TIMEOUT) as resp:
            if resp.status != 200:
                return None, resp.status
            return resp.read(), resp.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, None


def _decode(body: bytes):
    """JSON body, or None when it is not valid JSON (a malformed answer)."""
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def fetch_roots(host: str, port: int) -> tuple[list | None, int | None]:
    """The Media Roots list, or ``(None, status|None)`` on any failure."""
    if not host or not port:
        return None, None
    body, status = _get(roots_url(host, port))
    if body is None:
        return None, status
    data = _decode(body)
    return (data if isinstance(data, list) else None), status


def fetch_listing(
    host: str, port: int, path: str, offset: int = 0
) -> tuple[dict | None, int | None]:
    """One directory page, or ``(None, status|None)`` on any failure."""
    if not host or not port:
        return None, None
    body, status = _get(list_url(host, port, path, offset))
    if body is None:
        return None, status
    data = _decode(body)
    return (data if isinstance(data, dict) else None), status
