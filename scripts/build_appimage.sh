#!/usr/bin/env bash
# Build a self-contained viseq AppImage (e21s02). Local only — nothing is
# published to GitHub; the artifact lands in dist/.
#
# Usage: bash scripts/build_appimage.sh [VERSION]
#   VERSION defaults to the APP_VERSION string in viseq.py (overridable, e.g.
#   bash scripts/build_appimage.sh 0.3.0 for the release build).
#
# Pipeline (python-appimage docs recipe — extract -> pip -> repack):
#   1. fetch the CPython 3.13 manylinux_2_28 base AppImage from the
#      niess/python-appimage releases. The manylinux_2_28 base is REQUIRED:
#      numpy 2.5.2 wheels need glibc >= 2.27/2.28 (a manylinux2014 base, glibc
#      2.17, would not load them); essentia (manylinux2014) and dearpygui
#      (manylinux1) are compatible with that base.
#   2. --appimage-extract the base to an AppDir (no FUSE needed).
#   3. pip install -r requirements.txt into the AppDir (site packages land in
#      AppDir/opt/python3.13/lib/python3.13/site-packages).
#   4. copy viseq.py + viseqapp/ + controllers/ to AppDir/opt/viseq.
#   5. write a custom AppRun: PYTHONPATH -> opt/viseq, interpreter run with -s
#      (blocks the host's ~/.local site-packages so the bundled wheels win).
#   6. repack with appimagetool (FUSE-less fallback) -> dist/viseq-<VERSION>-x86_64.AppImage.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(grep -o 'APP_VERSION: str = "[^"]*"' "$ROOT/viseq.py" | cut -d'"' -f2)}"
BASE_FILE="python3.13.15-cp313-cp313-manylinux_2_28_x86_64.AppImage"
BASE_URL="https://github.com/niess/python-appimage/releases/download/python3.13/${BASE_FILE}"
TOOL_URL="https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"

CACHE="${CACHE_DIR:-$ROOT/.appimage-cache}"
mkdir -p "$CACHE" "$ROOT/dist"

fetch() { # fetch <url> <dest>
  if [ ! -s "$2" ]; then
    echo "==> downloading $(basename "$2")"
    curl -fL --retry 3 -o "$2.part" "$1"
    mv "$2.part" "$2"
  fi
}

echo "==> viseq AppImage build (version $VERSION)"
fetch "$BASE_URL" "$CACHE/$BASE_FILE"
fetch "$TOOL_URL" "$CACHE/appimagetool-x86_64.AppImage"
chmod +x "$CACHE/$BASE_FILE" "$CACHE/appimagetool-x86_64.AppImage"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
APPDIR="$WORK/viseq.AppDir"

echo "==> extracting base runtime"
( cd "$WORK" && "$CACHE/$BASE_FILE" --appimage-extract >/dev/null )
mv "$WORK/squashfs-root" "$APPDIR"

PY="$(ls -d "$APPDIR"/usr/bin/python3* | head -1)"
echo "==> installing runtime dependencies (interpreter: $PY)"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$ROOT/requirements.txt"

echo "==> bundling viseq sources"
mkdir -p "$APPDIR/opt/viseq"
cp "$ROOT/viseq.py" "$APPDIR/opt/viseq/"
cp -r "$ROOT/viseqapp" "$APPDIR/opt/viseq/"
cp -r "$ROOT/controllers" "$APPDIR/opt/viseq/"

echo "==> writing AppRun + desktop entry + icon"
PY_NAME="$(basename "$PY")"
# The base AppDir's AppRun is a symlink to usr/bin/python3.13 (the runtime
# wrapper); writing through it would clobber the wrapper. Write to a temp file
# and mv over the symlink so AppRun becomes a regular file and the wrapper stays.
cat > "$APPDIR/AppRun.tmp" <<EOF
#!/bin/sh
# viseq launcher (e21s02): isolated interpreter (-s blocks the host ~/.local
# site-packages so the bundled wheels win), app sources under \$APPDIR/opt/viseq.
export PYTHONPATH="\$APPDIR/opt/viseq\${PYTHONPATH:+:\$PYTHONPATH}"
exec "\$APPDIR/usr/bin/$PY_NAME" -s "\$APPDIR/opt/viseq/viseq.py" "\$@"
EOF
mv "$APPDIR/AppRun.tmp" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

mkdir -p "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cat > "$APPDIR/usr/share/applications/viseq.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=viseq
Comment=Audio-reactive VJ controller for Vimix
Exec=viseq
Icon=viseq
Categories=AudioVideo;Audio;
Terminal=false
EOF
# Minimal icon rendered with the BUNDLED Pillow (installed above).
"$PY" - <<'PYEOF' > "$APPDIR/usr/share/icons/hicolor/256x256/apps/viseq.png"
from PIL import Image, ImageDraw
img = Image.new("RGBA", (256, 256), (24, 24, 24, 255))
d = ImageDraw.Draw(img)
d.rectangle([8, 8, 248, 248], outline=(50, 255, 50, 255), width=8)
d.line([64, 192, 128, 64, 192, 192], fill=(50, 255, 50, 255), width=12, joint="curve")
img.save("/dev/stdout", format="PNG")
PYEOF

echo "==> repacking with appimagetool"
OUT="$ROOT/dist/viseq-$VERSION-x86_64.AppImage"
TOOL="$CACHE/appimagetool-x86_64.AppImage"
if ! "$TOOL" "$APPDIR" "$OUT" >/dev/null 2>&1; then
  echo "    (FUSE unavailable — retrying with --appimage-extract-and-run)"
  "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT" >/dev/null
fi
chmod +x "$OUT"
echo "==> built $OUT ($(du -h "$OUT" | cut -f1))"
