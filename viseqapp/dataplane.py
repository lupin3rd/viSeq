"""The machine-A HTTP data plane client (e41s03).

viOSC already serves HTTP on the preview port (the e38 surface). This module is
the viseq side of that plane: it fetches ONE thumbnail frame by source NAME with
a bounded timeout. e41s04 adds the state pull on the same server.

WORKER-SAFE and COUPLING-FREE: no dpg import (HIGH-1), and the endpoint is passed
by the caller instead of read from state, so this module stays a pure transport —
which is also what makes it testable against a real HTTP server on an ephemeral
port, with no daemon, no config and no network.
"""

import urllib.error
import urllib.request

from viseqapp.constants import THUMB_HTTP_TIMEOUT, THUMB_PATH_PREFIX


def thumbnail_url(host: str, port: int, source_name: str, index: int) -> str:
    """HTTP URL of one thumbnail frame on the viOSC data plane."""
    return f"http://{host}:{port}{THUMB_PATH_PREFIX}{source_name}/{index}"


def fetch_thumbnail(
    host: str, port: int, source_name: str, index: int
) -> tuple[bytes | None, bool]:
    """One GET of a thumbnail frame; never raises (e41s03).

    Returns ``(blob, answered)``:

    - ``blob`` is the JPEG bytes on 200, None otherwise;
    - ``answered`` is False ONLY when the server did not answer at all
      (connection refused, timeout). That is what separates a dead or missing
      endpoint from a server that is alive and merely has no such frame — the
      caller uses it to decide whether the fast lane is still worth keeping.
    """
    if not host or not port:
        return None, False
    try:
        with urllib.request.urlopen(
            thumbnail_url(host, port, source_name, index), timeout=THUMB_HTTP_TIMEOUT
        ) as resp:
            if resp.status != 200:
                return None, True
            return resp.read(), True
    except urllib.error.HTTPError:
        return None, True
    except Exception:
        return None, False
