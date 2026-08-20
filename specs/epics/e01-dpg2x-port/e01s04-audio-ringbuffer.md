# e01s04 — Audio analysis (VU/BPM) + ring buffer on 2.x

**type:** refactor
**risk:** P2
**context:** domain

**Context:** The audio path (sounddevice InputStream callback -> VU meter; essentia
RhythmExtractor2013 on a 6 s rolling buffer -> BPM display) must behave identically on the new
runtime. This story folds in audit item L-2: replace the per-callback `np.roll` (which
allocates a fresh ~1 MB buffer ~43x/s) with a preallocated ring buffer using modulo indexing.
The BPM path runs on essentia 2.1b6.dev1389 (cp313 wheel) — functionally the same API.

## Requirements

#### MODIFIED: Audio analysis runs on dpg 2.x + essentia 2.1b6.dev1389
**Before:** VU/BPM driven by dpg 1.x calls and essentia on Python <= 3.11.
**After:** identical VU/BPM behavior on 2.3.1 (UI updates still via `enqueue_set_value`, never
from the audio thread) with essentia 2.1b6.dev1389.

#### ADDED: Ring-buffer audio capture (L-2)
`audio_buffer` is a preallocated buffer with modulo indexing; no full-buffer reallocation in
the audio callback; `essentia_analyzer_loop` reads a consistent snapshot of the last 6 s.

## Steps

1. Replace `audio_buffer = np.roll(audio_buffer, -frames); audio_buffer[-frames:] = samples` with a ring buffer (preallocated `np.zeros`, head index via modulo, write in place) → verify: `python3 -m py_compile viseq.py`
2. Add a harness unit test for the ring buffer: after N callbacks, the buffer contains the newest `frames` samples at the tail and is length-consistent → verify: `python3 tests/test_fixes.py 2>&1 | grep -c "ring buffer" | grep -q 1`
3. Re-verify the audio-thread UI path is still queue-only (no dpg calls in `audio_callback`, VU via `enqueue_set_value`) → verify: `python3 -c "import re; src=open('viseq.py').read(); body=re.search(r'def audio_callback\\(.*?(?=\\ndef |\\n# ===)', src, re.S).group(0); assert not re.search(r'dpg\\.\\w+', body), 'direct dpg in audio_callback'; print('OK')"`
4. Re-verify the BPM path (essentia loop uses `lowpass_enabled` flag, not dpg.get_value; BPM text via enqueue) → verify: `python3 -c "import re; src=open('viseq.py').read(); body=re.search(r'def essentia_analyzer_loop\\(.*?(?=\\ndef |\\n# ===)', src, re.S).group(0); assert 'lowpass_enabled' in body and 'dpg.get_value' not in body, 'BPM path regressed'; print('OK')"` && `python3 tests/test_fixes.py`

## Verification Script (Step-by-Step)

1. `python3 tests/test_fixes.py` — ring-buffer and audio checks pass.
2. At user acceptance (e01s05): enable Level Analysis — VU meter moves with input; enable BPM — BPM/Confidence display updates; no crashes when toggling on/off rapidly.

## Out of scope

- Beat-sync features beyond current behavior; new audio analysis (onset, spectrum).
- Microphone device enumeration changes.

## Risks

- Ring-buffer read/write race between the audio thread and the analyzer thread — the analyzer
  already copies via `audio_buffer.copy()`; keep the snapshot consistent (copy under GIL is
  atomic enough for this app, as before).
- essentia 2.1b6.dev1389 may report slightly different BPM values than older builds — behavior
  (display updates) is preserved; exact numeric parity is not a goal.
