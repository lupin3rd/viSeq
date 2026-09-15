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
from typing import Any

from viseqapp import pairing, state
from viseqapp.constants import DATA_PLANE_TIMEOUT, FS_THUMB_PREFIX
from viseqapp.queues import log_error

FS_ROOTS_PATH = "/fs/roots"
FS_LIST_PATH = "/fs/list"
FS_THUMB_PATH = "/fs/thumb"
FS_RAW_PATH = "/fs/raw"
FS_SESSIONS_PATH = "/fs/sessions"
FS_SESSION_PATH = "/fs/session"


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


def thumbnail_url(host: str, port: int, path: str) -> str:
    """URL of one file's JPEG thumbnail (the path is URL-encoded)."""
    query = urllib.parse.urlencode({"path": str(path)})
    return f"http://{host}:{port}{FS_THUMB_PATH}?{query}"


def fetch_thumbnail(host: str, port: int, path: str) -> tuple[bytes | None, int | None]:
    """One thumbnail JPEG, or ``(None, status|None)`` on any failure."""
    if not host or not port:
        return None, None
    return _get(thumbnail_url(host, port, path))


def raw_url(host: str, port: int, path: str) -> str:
    """URL of a file's bytes (RFC 7233 Range), for the local video preview."""
    query = urllib.parse.urlencode({"path": str(path)})
    return f"http://{host}:{port}{FS_RAW_PATH}?{query}"


def sessions_url(host: str, port: int) -> str:
    """URL of the written Session Drafts (e43s06)."""
    return f"http://{host}:{port}{FS_SESSIONS_PATH}"


def session_url(host: str, port: int) -> str:
    """URL that writes a Session Draft (POST, e43s06)."""
    return f"http://{host}:{port}{FS_SESSION_PATH}"


def _post_json(url: str, payload: dict) -> tuple[bytes | None, int | None]:
    """One JSON POST with the pairing headers; never raises.

    Unlike ``_get``, the ERROR body is returned too, because the session errors
    carry a machine-readable tag (``bad_name`` / ``missing_file`` / …) the UI
    words for the user.
    """
    try:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", **pairing.http_headers()}
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=DATA_PLANE_TIMEOUT) as resp:
            if resp.status != 200:
                return None, resp.status
            return resp.read(), resp.status
    except urllib.error.HTTPError as e:
        try:
            return e.read(), e.code
        except Exception:
            return None, e.code
    except Exception:
        return None, None


def fetch_sessions(host: str, port: int) -> tuple[list | None, int | None]:
    """The Session Drafts already written on machine A, or ``(None, status)``."""
    if not host or not port:
        return None, None
    body, status = _get(sessions_url(host, port))
    if body is None:
        return None, status
    data = _decode(body)
    return (data if isinstance(data, list) else None), status


def write_session(
    host: str, port: int, name: str, files: list[str], *, overwrite: bool = False
) -> tuple[dict | None, int | None]:
    """Write one Session Draft on machine A; ``(payload, status)``.

    ``overwrite`` (e44s02) asks viOSC to replace the exact named file instead of
    suffixing a collision, so Save re-writes the session under the chosen name.

    On a rejection the payload is the error object (``{"error": "missing_file"}``)
    when the daemon answered with one, so the caller can word the reason.
    """
    if not host or not port:
        return None, None
    payload: dict[str, Any] = {"name": str(name), "files": [str(item) for item in files]}
    if overwrite:
        payload["overwrite"] = True
    body, status = _post_json(session_url(host, port), payload)
    if body is None:
        return None, status
    data = _decode(body)
    return (data if isinstance(data, dict) else None), status


def fs_thumb_worker() -> None:
    """Drain the browser's thumbnail requests into the decode pipeline (e43s03).

    A JPEG blob is enqueued under the ``fs:<path>`` namespace, so the EXISTING
    decoder creates a texture without touching any source thumbnail. The worker
    is daemon-safe: a fetch failure logs and the loop continues, and the caller
    dropped stale requests by clearing the queue on navigation.
    """
    while True:
        path = state.fs_thumb_queue.get()
        try:
            blob, _status = fetch_thumbnail(state.dataplane_host, state.dataplane_port, path)
            if blob is not None:
                state.blob_queue.put((f"{FS_THUMB_PREFIX}{path}", "0", blob))
            else:
                state.fs_thumb_requested.discard(path)
        except Exception as e:  # a worker thread must never die (Defensive Code)
            log_error("File Manager thumbnails", str(e))
        finally:
            state.fs_thumb_queue.task_done()
