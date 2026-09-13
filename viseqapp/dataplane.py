"""The machine-A HTTP data plane client (e41s03, e41s04).

viOSC already serves HTTP on the preview port (the e38 surface). This module is
the viseq side of that plane: it fetches ONE thumbnail frame by source NAME, and
it reads the state table as a PULL whose cadence viseq owns.

WORKER-SAFE and COUPLING-FREE: no dpg import (HIGH-1), and the endpoint is passed
by the caller instead of read from state, so this module stays a pure transport —
which is also what makes it testable against a real HTTP server on an ephemeral
port, with no daemon, no config and no network.
"""

import urllib.error
import urllib.request

from viseqapp import pairing
from viseqapp.constants import DATA_PLANE_TIMEOUT, STATE_PATH, THUMB_PATH_PREFIX


def thumbnail_url(host: str, port: int, source_name: str, index: int) -> str:
    """HTTP URL of one thumbnail frame on the viOSC data plane."""
    return f"http://{host}:{port}{THUMB_PATH_PREFIX}{source_name}/{index}"


def state_url(host: str, port: int) -> str:
    """HTTP URL of the state table resource on the viOSC data plane."""
    return f"http://{host}:{port}{STATE_PATH}"


def _fetch(url: str) -> tuple[bytes | None, bool]:
    """One GET of a data-plane resource; never raises.

    Returns ``(body, answered)``: ``body`` is the bytes on 200 and None
    otherwise; ``answered`` is False ONLY when the server did not answer at all
    (connection refused, timeout). That flag is what separates a dead or missing
    endpoint from a server that is alive and merely has nothing at this address —
    the lane decisions depend on telling those two apart.
    """
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=pairing.http_headers()),
            timeout=DATA_PLANE_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                return None, True
            return resp.read(), True
    except urllib.error.HTTPError:
        return None, True
    except Exception:
        return None, False


def fetch_thumbnail(
    host: str, port: int, source_name: str, index: int
) -> tuple[bytes | None, bool]:
    """One GET of a thumbnail frame (e41s03). See ``_fetch`` for the contract."""
    if not host or not port:
        return None, False
    return _fetch(thumbnail_url(host, port, source_name, index))


def fetch_state(host: str, port: int) -> tuple[str | None, bool]:
    """One GET of the state table as its exact JSON text (e41s04).

    The text is handed on verbatim: the daemon produced it with the same
    serializer that feeds the OSC broadcast, so parsing it belongs to the
    existing state ingestion, not to the transport.
    """
    if not host or not port:
        return None, False
    body, answered = _fetch(state_url(host, port))
    if body is None:
        return None, answered
    return body.decode("utf-8", errors="replace"), True
