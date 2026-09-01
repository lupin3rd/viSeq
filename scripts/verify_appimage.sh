#!/usr/bin/env bash
# Run the full pytest suite under the BUNDLED AppImage interpreter (e21s02).
#
# The verification runs against a throwaway copy of the built AppImage so the
# shipped artifact stays clean (no pytest bloat): extract -> copy the test
# harness into opt/viseq/tests -> pip install pytest into the copy -> run the
# suite with the AppDir python. The harness (tests/test_fixes.py) puts
# dirname(dirname(tests)) on sys.path, so it imports the BUNDLED viseq.py +
# viseqapp/ and resolves every dependency from the BUNDLED site-packages.
#
# Usage: bash scripts/verify_appimage.sh [path/to/viseq-*.AppImage]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPIMAGE="${1:-$(ls -1 "$ROOT"/dist/viseq-*.AppImage 2>/dev/null | head -1)}"
if [ -z "$APPIMAGE" ]; then
  echo "error: no AppImage found in $ROOT/dist — run scripts/build_appimage.sh first" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
( cd "$WORK" && "$APPIMAGE" --appimage-extract >/dev/null )
APPDIR="$WORK/squashfs-root"
PY="$(ls -d "$APPDIR"/usr/bin/python3* | head -1)"

echo "==> installing pytest into the verification copy"
"$PY" -m pip install --quiet pytest

echo "==> copying the test harness into the bundled app"
cp -r "$ROOT/tests" "$APPDIR/opt/viseq/tests"

echo "==> running the full suite under the bundled interpreter"
( cd "$APPDIR/opt/viseq" && "$PY" -m pytest tests/ -q )

echo "VERIFY OK — suite green under the bundled AppImage interpreter"
