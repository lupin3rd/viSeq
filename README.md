# viSeq

Audio-reactive VJ controller for **Vimix**, driven by **viOSC**.

An 8×8 step sequencer that fires OSC messages to Vimix in sync with the music
(VU/BPM analysis, MIDI clock, band peaks), with a thumbnail grid, monitor
players and optional MIDI controller support (Novation Launchpad profiles
included).

[![Watch the video](https://img.youtube.com/vi/dtzOFJbv7ko/maxresdefault.jpg)](https://youtu.be/dtzOFJbv7ko)

## Requirements

- Python 3.13
- [viOSC](https://github.com/lupin3rd/viosc) (the OSC daemon that bridges viSeq and Vimix)
- Vimix
- (optional) a Novation Launchpad MK1/MK2/MK3 for pad control

## Quick start

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python viseq.py
```

## License

GPL-3.0 — see [LICENSE](LICENSE).
