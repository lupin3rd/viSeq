# viSeq

Audio-reactive VJ controller for **Vimix**, driven by **viOSC**.

An 8×8 step sequencer that fires OSC messages to Vimix in sync with the music
(VU/BPM analysis, MIDI clock, band peaks), with a thumbnail grid, monitor
players and optional MIDI controller support (Novation Launchpad profiles
included).

[![Watch the video](https://img.youtube.com/vi/dtzOFJbv7ko/maxresdefault.jpg)](https://youtu.be/dtzOFJbv7ko)

## Quick start — AppImage (Linux)

The easiest way to run viSeq on Linux is the self-contained AppImage: a single
file with Python, all libraries and the app bundled — **no Python, no pip, no
root**. Starting with release **0.4.0**, download
`viseq-<version>-x86_64.AppImage` from the
[GitHub Releases](https://github.com/lupin3rd/viseq/releases) page, then:

```bash
chmod +x viseq-0.4.0-x86_64.AppImage
./viseq-0.4.0-x86_64.AppImage
```

### Where your data lives

- Config: `~/.config/viseq/viseq_config.json` (honors `$XDG_CONFIG_HOME`).
- Controller profiles: drop custom `*.json` profiles into
  `~/.config/viseq/controllers/` (the Launchpad profiles ship built-in).
- Projects: the Open/Save dialogs default to `~/.config/viseq/projects/`; you
  can save anywhere you like.

## Quick start — from source (developers)

Requires Python 3.13:

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python viseq.py
```

## Requirements

- [viOSC](https://github.com/lupin3rd/viosc) (the OSC daemon that bridges viSeq
  and Vimix)
- Vimix
- (optional) a Novation Launchpad MK1/MK2/MK3 for pad control

## License

GPL-3.0 — see [LICENSE](LICENSE).
