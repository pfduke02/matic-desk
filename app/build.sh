#!/usr/bin/env bash
# Build "Matic Desk.app" with the Command Line Tools swiftc (no Xcode project).
#
#   app/build.sh [--backend-python PATH] [--out DIR] [--workdir DIR]
#
# PATH defaults to <repo>/.venv/bin/python (create it with `uv sync`), DIR to
# <repo>/build. Absolute paths are written only into the built Info.plist.
set -euo pipefail

APP_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$APP_SRC/.." && pwd)"
BACKEND_PYTHON="$REPO/.venv/bin/python"
OUT_DIR="$REPO/build"

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-python) BACKEND_PYTHON="${2:?--backend-python needs a path}"; shift 2 ;;
    --out) OUT_DIR="${2:?--out needs a directory}"; shift 2 ;;
    --workdir) WORK_DIR="${2:?--workdir needs a directory}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# Absolute paths; keep the venv symlink as-is so the venv's site-packages apply.
[[ "$BACKEND_PYTHON" = /* ]] || BACKEND_PYTHON="$PWD/$BACKEND_PYTHON"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

if [[ ! -x "$BACKEND_PYTHON" ]]; then
  echo "warning: backend python not found at $BACKEND_PYTHON (run 'uv sync' in $REPO)" >&2
fi

APP="$OUT_DIR/Matic Desk.app"
CONTENTS="$APP/Contents"
rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

PLIST="$CONTENTS/Info.plist"
cp "$APP_SRC/Info.plist.template" "$PLIST"
plutil -replace MDBackendExecutable -string "$BACKEND_PYTHON" "$PLIST"
plutil -replace MDBackendWorkingDirectory -string "${WORK_DIR:-$HOME}" "$PLIST"
plutil -lint "$PLIST" >/dev/null

# Build for this Mac's architecture (arm64 on Apple Silicon, x86_64 on Intel).
ARCH="$(uname -m)"
case "$ARCH" in
  arm64 | x86_64) ;;
  *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

SOURCES=("$APP_SRC"/Sources/MaticDesk/*.swift)
swiftc -O \
  -swift-version 5 \
  -target "$ARCH-apple-macos14.0" \
  -parse-as-library \
  -module-name MaticDesk \
  "${SOURCES[@]}" \
  -o "$CONTENTS/MacOS/MaticDesk"

# swiftc's linker already ad-hoc signs the binary; re-sign the whole bundle.
if ! sign_out="$(codesign --force --sign - "$APP" 2>&1)"; then
  echo "$sign_out" >&2
  exit 1
fi
echo "$APP"
