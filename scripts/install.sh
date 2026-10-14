#!/usr/bin/env bash
# Install Matic Desk from a clone of the repo:
#   1. create the Python environment (.venv) with uv
#   2. build the menu bar app and copy it to ~/Applications
#   3. optionally register the MCP server with Claude Code
#   4. print config snippets for Claude Desktop and Codex
# Safe to re-run. Never edits Claude Desktop or Codex config files.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/install.sh [--dry-run] [--no-app] [--register-claude-code]

  --dry-run               print what would be done, change nothing
  --no-app                skip building and installing the menu bar app
  --register-claude-code  run `claude mcp add --scope user matic-desk ...`
                          (skipped if matic-desk is already registered)
USAGE
}

DRY_RUN=0
BUILD_APP=1
REGISTER_CLAUDE_CODE=0
CLAUDE_CODE_REGISTERED=0
UV=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-app) BUILD_APP=0 ;;
    --register-claude-code) REGISTER_CLAUDE_CODE=1 ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
APP_NAME="Matic Desk"
BUILT_APP="$REPO/build/$APP_NAME.app"
DEST_DIR="$HOME/Applications"
DEST_APP="$DEST_DIR/$APP_NAME.app"
STAGING_APP="$DEST_DIR/.$APP_NAME.app.installing"
# The app and MCP clients run Matic Desk from a runtime environment outside the
# repo. Clones often live in ~/Desktop, ~/Documents or ~/Downloads, which macOS
# privacy protection freezes for a newly installed app until a dialog is answered.
RUNTIME="${MATIC_DESK_RUNTIME:-$HOME/.local/share/matic-desk}"
VENV_PYTHON="$RUNTIME/venv/bin/python"
MCP_BIN="$RUNTIME/venv/bin/matic-desk-mcp"
ROBOT_JSON="${MATIC_CONFIG:-$HOME/.config/matic/robot.json}"

say() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Run a command, or only print it (shell-quoted) in --dry-run mode.
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

# Escape a string for a JSON or TOML basic string.
quote_string() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

check_prerequisites() {
  [[ "$(uname -s)" == "Darwin" ]] || die "Matic Desk runs on macOS only."
  [[ $EUID -ne 0 ]] || die "Run this as your normal user, not with sudo."

  local version major
  version="$(sw_vers -productVersion)"
  major="${version%%.*}"
  [[ "$major" =~ ^[0-9]+$ ]] || die "Could not read the macOS version ($version)."
  ((major >= 14)) || die "macOS 14 or later is required (this Mac has $version)."
  [[ "$(uname -m)" == "arm64" ]] || warn "Only tested on Apple Silicon; building for $(uname -m) anyway."

  if [[ -x "$HOME/.local/bin/uv" ]]; then
    UV="$HOME/.local/bin/uv"
  elif command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
  else
    die "uv not found. Install it (https://docs.astral.sh/uv/) and run this again."
  fi

  if [[ $BUILD_APP -eq 1 ]]; then
    # /usr/bin/swiftc exists even without the tools (it opens an installer), so
    # ask xcode-select first; it never prompts.
    if ! xcode-select -p >/dev/null 2>&1 || ! xcrun --find swiftc >/dev/null 2>&1; then
      die "swiftc not found. Run: xcode-select --install   (or re-run with --no-app)"
    fi
    [[ -f "$REPO/app/build.sh" ]] || die "Missing $REPO/app/build.sh"
  fi

  say "macOS $version, uv at $UV, repo at $REPO"
}

sync_python() {
  say "Creating the development environment (uv sync)"
  run "$UV" sync
  say "Installing the runtime into $RUNTIME/venv"
  if [[ ! -x "$VENV_PYTHON" ]]; then
    run "$UV" venv --python 3.14 "$RUNTIME/venv"
  fi
  run "$UV" pip install --python "$VENV_PYTHON" --reinstall-package matic-desk "$REPO"
}

build_and_install_app() {
  say "Building $APP_NAME.app"
  run bash "$REPO/app/build.sh" --backend-python "$VENV_PYTHON" --workdir "$RUNTIME"
  if [[ $DRY_RUN -eq 0 && ! -d "$BUILT_APP" ]]; then
    die "Build finished but $BUILT_APP is missing."
  fi

  say "Installing to $DEST_APP"
  if [[ -d "$DEST_APP" ]]; then
    quit_running_app
  fi
  run mkdir -p "$DEST_DIR"
  run rm -rf "${STAGING_APP:?}"
  run ditto "$BUILT_APP" "$STAGING_APP"
  run rm -rf "${DEST_APP:?}"
  run mv "$STAGING_APP" "$DEST_APP"
}

# Quit an installed copy that is running, so it is not replaced underneath itself.
quit_running_app() {
  local script="if application \"$APP_NAME\" is running then tell application \"$APP_NAME\" to quit"
  if [[ $DRY_RUN -eq 1 ]]; then
    run osascript -e "$script"
    return 0
  fi
  osascript -e "$script" >/dev/null 2>&1 || true
  local waited=0
  while pgrep -f "$DEST_APP/Contents/MacOS/" >/dev/null 2>&1 && ((waited < 10)); do
    sleep 1
    waited=$((waited + 1))
  done
  if pgrep -f "$DEST_APP/Contents/MacOS/" >/dev/null 2>&1; then
    warn "$APP_NAME is still running; quit it from the menu bar to pick up the new build."
  fi
}

register_claude_code() {
  if ! command -v claude >/dev/null 2>&1; then
    warn "Claude Code CLI (claude) not found; skipping registration."
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    # Do not start the claude CLI at all in a dry run.
    say "Registering matic-desk with Claude Code (user scope), unless already registered"
    run claude mcp add --scope user matic-desk -- "$MCP_BIN"
    return 0
  fi
  if claude mcp get matic-desk >/dev/null 2>&1; then
    say "Claude Code already has an MCP server named matic-desk; leaving it alone."
    CLAUDE_CODE_REGISTERED=1
    return 0
  fi
  say "Registering matic-desk with Claude Code (user scope)"
  run claude mcp add --scope user matic-desk -- "$MCP_BIN"
  CLAUDE_CODE_REGISTERED=1
}

print_next_steps() {
  local cmd uv_cmd
  cmd="$(quote_string "$MCP_BIN")"
  if command -v uv >/dev/null 2>&1; then uv_cmd="uv"; else uv_cmd="$(printf '%q' "$UV")"; fi

  if [[ $DRY_RUN -eq 1 ]]; then
    printf '\nDry run complete. After a real install, connect your AI client of choice:\n\n'
  else
    printf '\nMatic Desk is installed. Connect your AI client of choice:\n\n'
  fi

  if [[ $CLAUDE_CODE_REGISTERED -eq 1 ]]; then
    printf 'Claude Code: matic-desk is registered.\n'
  else
    printf 'Claude Code:\n  claude mcp add --scope user matic-desk -- %s\n' "$(printf '%q' "$MCP_BIN")"
  fi

  cat <<NEXT

Claude Desktop: Settings > Developer > Edit Config, add this to
~/Library/Application Support/Claude/claude_desktop_config.json
(merge into "mcpServers" if you already have one), then quit and reopen Claude:

  {
    "mcpServers": {
      "matic-desk": {
        "command": $cmd
      }
    }
  }

Codex: add this to ~/.codex/config.toml, then restart Codex:

  [mcp_servers.matic-desk]
  command = $cmd
NEXT

  if [[ $BUILD_APP -eq 1 ]]; then
    printf '\nMenu bar app: open %s\n' "$(printf '%q' "$DEST_APP")"
    printf 'Allow Local Network access when macOS asks.\n'
  fi

  if [[ ! -e "$ROBOT_JSON" ]]; then
    cat <<PAIR

Not paired yet. Turn on Pairing mode in the Matic app (Settings > Connectivity >
Add another user), then run this in Terminal or iTerm yourself (macOS blocks
Bluetooth for processes started by other apps, including Claude):

  cd $(printf '%q' "$REPO") && $uv_cmd run matic-desk pair

It restarts itself inside a small "Matic Desk Pairing" helper (macOS only allows
Bluetooth inside an app bundle); if macOS asks whether it may use Bluetooth,
allow it. When the robot shows a six-digit code, type it into the Mac's
Bluetooth pairing dialog.
PAIR
  fi
}

main() {
  cd "$REPO"
  if [[ $DRY_RUN -eq 1 ]]; then
    say "Dry run: nothing will be changed."
  fi
  check_prerequisites
  sync_python
  if [[ $BUILD_APP -eq 1 ]]; then
    build_and_install_app
  fi
  if [[ $REGISTER_CLAUDE_CODE -eq 1 ]]; then
    register_claude_code
  fi
  print_next_steps
}

main
