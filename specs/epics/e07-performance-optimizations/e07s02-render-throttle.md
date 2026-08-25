# e07s02 — P1: Idle render throttle — frame_sleep() full rate while animating, throttled while idle

**type:** refactor
**risk:** P2
**context:** perf/main-loop

**Context:** The main loop renders unconditionally at 60 fps (`time.sleep(0.016)`), even when
nothing on screen is moving. The render pass cost scales with live widget count, so idle
frames still cost GPU/CPU. Introduce a `frame_sleep()` decision: full rate while something
animates (sequencer running, spectrum on, any monitor video playing), throttled rate (~30
fps) while idle. Input stays responsive at 30 fps (33 ms poll); no frame-work change.

## Requirements

#### ADDED: frame_sleep() cadence decision
`frame_sleep()` returns `FRAME_SLEEP_ANIMATED` (full rate) when `is_playing` or
`is_audio_analyzing` is true, or any monitor player's source video is playing
(`video_is_playing`); otherwise `FRAME_SLEEP_IDLE` (throttled). The main loop sleeps the
returned value.

## Steps

1. Add the two sleep constants + `frame_sleep()`; wire it into the main loop sleep.
   → verify: `.venv/bin/python -m pytest tests/ -q -k frame_sleep`
2. Regression tests: idle → throttled; playing/spectrum/monitor-playing → full rate.
   → verify: `.venv/bin/python -m pytest tests/ -q -k frame_sleep`

## Verification Script (Step-by-Step)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Real rig: with nothing playing/analyzing, the app idles at ~30 fps (lower CPU/GPU); press
   PLAY or enable the spectrum → back to ~60 fps; dragging windows/sliders stays responsive.

## Out of scope

- Rendering on demand (dirty-frame rendering) — larger change, not needed for the win.
- GPU-side tuning (vsync).

## Risks

- 30 fps idle slightly increases input latency (33 ms) — acceptable for idle state; the
  real-rig acceptance covers the feel. If the user notices, the animated threshold can be
  widened (e.g. any focused input keeps full rate).
