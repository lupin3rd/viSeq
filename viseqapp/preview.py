"""Source video preview transport for viseq (e38s02).

Decodes the FILE of a vimix media source from the viOSC HTTP preview server
on machine A (SOURCE_PREVIEW_LATEST.md: family-A content preview, O2 local
decode) and pushes frames for the UI. WORKER-SAFE: this module never imports
dpg (HIGH-1) — the decode loop runs on its own thread and only touches state
queues and PyAV; the main thread owns every dpg call.

Endpoints are injected by the caller (the UI resolves them from the config)
or defaulted from the constants, so this module stays free of config/queues
imports (both pull dearpygui into the process).
"""

import json
import threading
import time
import urllib.request
from typing import Any

import av
import numpy as np
from PIL import Image

from viseqapp import state
from viseqapp.constants import (
    PREVIEW_CAP_HEIGHT,
    PREVIEW_CAP_WIDTH,
    PREVIEW_HTTP_TIMEOUT,
    PREVIEW_MAX_FPS,
    PREVIEW_PATH_PREFIX,
    PREVIEW_PORT,
)

# The av package ships no type stubs; mypy runs with ignore_missing_imports.

_DEFAULT_CAP = (PREVIEW_CAP_WIDTH, PREVIEW_CAP_HEIGHT)


def preview_file_url(host: str, port: int, source_name: str) -> str:
    """HTTP URL of a source's media file on the viOSC preview server."""
    return f"http://{host}:{port}{PREVIEW_PATH_PREFIX}{source_name}/file"


def preview_meta_url(host: str, port: int, source_name: str) -> str:
    """HTTP URL of a source's ffprobe meta JSON on the viOSC preview server."""
    return f"http://{host}:{port}{PREVIEW_PATH_PREFIX}{source_name}/meta"


def preview_availability(media_kind: Any) -> bool | None:
    """Map a source's state media_kind to preview availability.

    video -> True (show the action/panel), image -> False (excluded per user
    decision), None/absent -> None (kind unknown: classify on activation via
    the meta endpoint).
    """
    if media_kind == "video":
        return True
    if media_kind == "image":
        return False
    return None


def fetch_media_meta(host: str, port: int, source_name: str) -> dict[str, Any] | None:
    """One GET of the source's meta JSON; None on error/404/non-media."""
    try:
        with urllib.request.urlopen(
            preview_meta_url(host, port, source_name), timeout=PREVIEW_HTTP_TIMEOUT
        ) as resp:
            if resp.status != 200:
                return None
            payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            return payload
    except Exception:
        return None


class PreviewPlayer:
    """One decode loop over an HTTP preview URL (e38s02). DPG-free.

    Pushes decoded RGBA float32 arrays (h, w, 4) as ``(source_name, frame)``
    onto ``state.preview_frames``. Playback: start() begins playing; pause() /
    resume() gate the loop; seek(fraction) jumps and — when paused — publishes
    exactly one frame at the target so a scrub bar shows feedback; close()
    stops and joins the thread. Frames are scaled down to the cap and pushed at
    most ``fps_cap`` per second (pass ``fps_cap=0`` to disable the gate, e.g.
    in tests). ``position()`` is the seconds of the most recent decoded frame;
    ``error``/``done`` report terminal state.
    """

    def __init__(
        self,
        source_name: str,
        url: str | None = None,
        host: str | None = None,
        port: int | None = None,
        *,
        fps_cap: float = PREVIEW_MAX_FPS,
        cap: tuple[int, int] = _DEFAULT_CAP,
        autoplay: bool = True,
    ) -> None:
        self.source_name = source_name
        self.url = url or preview_file_url(host or "127.0.0.1", port or PREVIEW_PORT, source_name)
        self._fps_cap = float(fps_cap or 0.0)
        self._cap = cap
        self._autoplay = autoplay

        self._thread: threading.Thread | None = None
        self._running = False
        self._paused = not autoplay
        self._seek_frac: float | None = None  # pending seek (worker consumes)
        self._position = 0.0  # seconds of the last decoded frame (worker writes)
        self._duration = 0.0  # seconds, resolved at open
        self.error: str | None = None  # terminal open/decode failure message
        self.done = False  # True once the thread has exited

    # -- public controls (any thread; called by the UI on the main thread) ----

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name=f"preview-{self.source_name}", daemon=True
        )
        self._thread.start()

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def seek(self, fraction: float) -> None:
        """Request a seek to a fraction (0..1) of the playable duration.

        The worker performs the seek and (when paused) publishes one frame at
        the target. Safe to call from any thread.
        """
        self._seek_frac = max(0.0, min(1.0, float(fraction)))

    def close(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def position(self) -> float:
        return self._position

    @property
    def duration(self) -> float:
        return self._duration

    # -- internals ----------------------------------------------------------

    def _publish(self, frame: Any, tb: float) -> None:
        """Scale + convert one decoded frame and push it onto the queue."""
        arr = frame.to_ndarray(format="rgb24")  # (h, w, 3) uint8
        img = Image.fromarray(arr).convert("RGBA")
        if img.width > self._cap[0] or img.height > self._cap[1]:
            img.thumbnail(self._cap, Image.Resampling.LANCZOS)
        rgba = np.asarray(img, dtype=np.float32) / 255.0
        state.preview_frames.put((self.source_name, rgba))
        pts = frame.pts
        if pts is not None:
            self._position = pts * tb

    def _run(self) -> None:
        try:
            container = av.open(self.url, options={"seekable": "1"}, timeout=PREVIEW_HTTP_TIMEOUT)
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                raise ValueError(f"no video stream in '{self.source_name}'")
            duration = container.duration
            self._duration = float(duration) / av.time_base if duration else 0.0
            tb_base = stream.time_base
            tb = float(tb_base) if tb_base is not None else 1.0
            frames: Any = None  # decode generator — created lazily, fresh after every seek

            while self._running:
                # 1) a pending seek is served before anything else: seek, then
                #    rebuild the generator FROM the seek point (a generator
                #    created before the seek resumes from the wrong position)
                frac = self._seek_frac
                self._seek_frac = None
                if frac is not None:
                    target = frac * self._duration
                    container.seek(int(target / tb), stream=stream)
                    frames = container.decode(stream)
                    for frame in frames:
                        pts = frame.pts
                        if pts is None:
                            continue
                        if pts * tb >= target - 0.5:
                            self._publish(frame, tb)
                            break
                    last_push = time.monotonic()
                    if self._paused:
                        continue
                # 2) paused: idle until a command arrives
                if self._paused:
                    time.sleep(0.02)
                    continue
                # 3) playing: decode the next frame
                if frames is None:
                    frames = container.decode(stream)
                frame = next(frames, None)
                if frame is None:
                    # end of stream: restart from the beginning (preview loop)
                    self._seek_frac = 0.0
                    continue
                if frame.pts is not None:
                    self._position = frame.pts * tb
                if self._fps_cap and time.monotonic() - last_push < 1.0 / self._fps_cap:
                    continue  # frame dropped (decoded but not pushed)
                self._publish(frame, tb)
                last_push = time.monotonic()
        except Exception as e:
            self.error = str(e)
        finally:
            self._running = False
            self.done = True
