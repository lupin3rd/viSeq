---
bug_id: BUG-2026-08-27T201742
status: fixed
severity: high
scope: thumbnail-pipeline
title: OSC server drops thumbnail blobs > 8 KB — tile stuck on "Loading..." forever
---

# BUG-2026-08-27T201742: Thumbnail blobs > 8 KB silently dropped by the OSC receiver

## Problem

- **What happens (actual):** On the real rig with 5 media loaded, 4 thumbnails
  display and the 5th tile stays on " [ Loading... ]" forever, even though the
  daemon answers every request. The tile never flips to the e10s04 failed state
  and no retry appears to help. The media file itself is valid — ffmpeg
  extracts frames from it fine.
- **What should happen (expected):** Every source whose daemon replies with a
  thumbnail must display it, regardless of the frame's JPEG size. A failed
  generation must be visible (failed tile + retry), never an eternal "Loading".
- **How to reproduce:** Load a video whose extracted 320x180 JPEG thumbnails
  exceed ~8 KB per frame (high-detail frames compress poorly). Watch the tile
  stay on "Loading..." while the daemon sends the frames every 3 s.

`Security impact: NONE` — local GUI display only; no I/O or data exposure.

## Root Cause Analysis

Live diagnosis of the running stack (viosc pid 3379 / viseq pid 4389 /
vimix pid 3383) via passive heap forensics and a deterministic reproduction:

1. viseq requests `/viosc/thumb/<name>` for the stuck source every 3 s (54
   logged requests), and viOSC replies — the daemon's cache holds the frames
   and `send_thumbnail_blob` sends `"all"` indices back-to-back. In viseq's
   memory the replies `/viosc/replythumb/.../0..2` are mostly absent, and the
   OSC server printed
   `Found incorrect datagram, ignoring it', ParseError('Datagram is too short.')`
   — a python-osc parse failure caused by a **truncated UDP datagram**.
2. Extracting the stuck source's frames exactly like viOSC does
   (`ffmpeg -vframes 1 -s 320x180 -f image2pipe -vcodec mjpeg`) yields blobs of
   **13 396 / 13 774 / 16 558 bytes**. The working sources' frames happen to
   fall under the limit.
3. `socketserver.UDPServer.max_packet_size` defaults to **8192** and
   `get_request()` does `self.socket.recvfrom(self.max_packet_size)`.
   python-osc's `ThreadingOSCUDPServer` (used by
   `start_osc_server()`) does **not** override it. Any datagram longer than
   8 KB is truncated at recv, the OSC parser raises
   `ParseError("Datagram is too short")`, and python-osc silently drops the
   packet — the handler (`incoming_osc_handler`) is never invoked, no IN log
   line is appended, and the blob never reaches the decoder.
4. Deterministic reproduction (standalone ThreadingOSCUDPServer on a scratch
   port): a 4 KB thumbnail blob is delivered to the handler, a 13 KB blob is
   dropped with the same parse error. Confirmed: the socket buffer, not the
   daemon, is the failure point.
5. Secondary UX defect that hides the failure: the e10s04 "failed" tile label
   is rendered only during the Mediagrid structural rebuild (signature
   change). When `thumb_fail_count` crosses the threshold mid-session the
   request loop sends the one-shot regen but **nothing re-renders the tile**,
   so the user keeps seeing " [ Loading... ]" — "stuck on loading" is doubly
   guaranteed (no data AND no visible failure).

- **Modules involved:** viseq `start_osc_server()` (socket buffer size) and
  the e10s04 request loop / tile-label rendering.
- **Why it fails:** the recv buffer (8 KB) is smaller than legitimately sized
  thumbnail blobs (viseq's own accepted cap is 8 MB) and far smaller than the
  daemon's 320x180 mjpeg frames (~10–17 KB); python-osc reports truncation
  only to stdout; the failed-state UI is build-time, not transition-time.
- **Contributing factors:** JPEG size is content-dependent, so the same
  pipeline works for some sources and not others; the 3-frame "all" reply
  means the loss is per-frame and mostly total (the largest frames are exactly
  the ones dropped).
- **Risk level:** High for this user — one permanently missing thumbnail per
  load is common with high-detail media; also a latent risk for the replydata
  JSON (state > 8 KB with many sources would go stale silently).

## TDD Fix Plan (viseq repo)

1. **RED**: test asserting the OSC server class used by `start_osc_server`
   receives a > 8 KB thumbnail blob end-to-end (scratch port, fake dispatcher)
   and the handler fires → verify: `.venv/bin/python -m pytest tests/ -q -k osc_recv`
2. **GREEN**: define a `ThumbnailOSCUDPServer(osc_server.ThreadingOSCUDPServer)`
   with `max_packet_size = MAX_THUMBNAIL_BLOB_BYTES + 4096` (header margin)
   and instantiate it in `start_osc_server` → verify:
   `.venv/bin/python -m pytest tests/ -q -k osc_recv`
3. **RED**: test asserting the tile label flips to the failed label in place
   when the request loop crosses the threshold (no rebuild required), and the
   Loading label returns after a reply → verify:
   `.venv/bin/python -m pytest tests/ -q -k failed`
4. **GREEN**: on the threshold transition, update the existing
   `loading_txt_<id>` widget text to `THUMB_FAIL_LABEL` (main thread);
   `apply_thumbnail_texture` already deletes the label when the first texture
   lands → verify: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/mypy`
5. Full-suite verification → verify:
   `.venv/bin/python -m pytest tests/ -q`

## Acceptance Criteria

- [ ] A source whose frames are > 8 KB displays a thumbnail (all 3 frames).
- [ ] The OSC server no longer prints "Datagram is too short" for thumbnails.
- [ ] A genuinely unanswerable source flips to the failed tile with right-click
      retry *without* waiting for a grid rebuild.
- [ ] A later successful reply clears the failed state and shows the thumb.
- [ ] The frozen OSC contract is unchanged; the replydata JSON path also
      benefits (state > 8 KB no longer truncated).
- [ ] All new tests pass; existing tests still pass.

## Resolution

Fixed on 2026-08-27 (TDD, all gates green: 192 tests, ruff, mypy).

- `ViseqOSCUDPServer(osc_server.ThreadingOSCUDPServer)` overrides
  `max_packet_size = MAX_THUMBNAIL_BLOB_BYTES + 4096` (8 392 704 bytes) so
  recvfrom() no longer truncates thumbnail datagrams; `start_osc_server`
  instantiates it. Verified end-to-end with the real stuck source's frames
  (13 396 / 13 774 / 16 558 bytes) — all 3 arrive intact (was: all dropped).
- `_show_failed_tile_label()` flips the tile label to the failed state in
  place when the unanswered-request counter crosses the threshold, so a
  genuinely unanswerable source degrades visibly (with right-click retry)
  instead of showing "Loading..." forever.
- Live rig pending: restart viseq with the fixed code and confirm the 5th
  thumbnail (04-Demanty-altracorsa) displays and cycles.
