#!/usr/bin/env bash
# Smoke-verify the built viseq AppImage (e21s02): extract it FUSE-free with
# --appimage-extract (also proves the squashfs is intact) and run checks under
# the BUNDLED interpreter:
#   - every runtime dependency imports (native wheels load: essentia, numpy,
#     dearpygui, sounddevice, ...)
#   - a real essentia computation runs (the native .so actually works)
#   - viseqapp imports and CONFIG_PATH resolves under a set XDG_CONFIG_HOME
#   - the bundled controller profiles load
#
# Usage: bash scripts/smoke_appimage.sh [path/to/viseq-*.AppImage]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPIMAGE="${1:-$(ls -1 "$ROOT"/dist/viseq-*.AppImage 2>/dev/null | head -1)}"
if [ -z "$APPIMAGE" ]; then
  echo "error: no AppImage found in $ROOT/dist — run scripts/build_appimage.sh first" >&2
  exit 1
fi
[ -x "$APPIMAGE" ] || { echo "error: $APPIMAGE is not executable (chmod +x)" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
( cd "$WORK" && "$APPIMAGE" --appimage-extract >/dev/null )
PY="$(ls -d "$WORK"/squashfs-root/usr/bin/python3* | head -1)"

echo "==> smoke: bundled interpreter $PY"
XDG_CONFIG_HOME="$WORK/xdg" "$PY" - <<PYEOF
import importlib.util
import os
import sys

os.environ["XDG_CONFIG_HOME"] = "$WORK/xdg"

deps = ["essentia", "numpy", "dearpygui", "sounddevice", "mido", "pythonosc", "PIL"]
missing = [m for m in deps if importlib.util.find_spec(m) is None]
assert not missing, f"missing bundled dependencies: {missing}"

import numpy as np
from essentia.standard import Energy
assert Energy()(np.ones(1024)) > 0, "essentia native computation failed"

import viseqapp.config as config
assert config.CONFIG_PATH.startswith("$WORK/xdg"), config.CONFIG_PATH
assert config.user_config_dir().endswith(os.path.join("$WORK/xdg", "viseq")), config.user_config_dir()

import viseqapp.profiles as profiles
loaded = profiles.load_controller_profiles()
assert {"launchpad_mk1", "launchpad_mk2", "launchpad_mk3"} <= set(loaded), set(loaded)

print("SMOKE OK — bundled deps, essentia compute, XDG config path, bundled profiles")
PYEOF
