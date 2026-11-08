#!/bin/sh
set -eu

HOME="${HOME:?HOME must be set}"
RUNTIME_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd -P)"
SUPPORT="$HOME/Library/Application Support/GPoint/gpoint-eros-worker"
PYTHON="$RUNTIME_DIR/.venv/bin/python3.12"
CODEX_BIN="/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
WORKER="$RUNTIME_DIR/gpoint_mac_worker.py"
COMMAND="${1:-watch}"

export HOME
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONUNBUFFERED=1
export GPOINT_CODEX_CLI="$CODEX_BIN"
export GPOINT_EROS_WORKER_CONFIG="${GPOINT_EROS_WORKER_CONFIG:-$HOME/.config/gpoint/agata-worker.env}"
export GPOINT_EROS_WORKER_SUPPORT="$SUPPORT"

cd "$RUNTIME_DIR"
exec "$PYTHON" "$WORKER" "$COMMAND"
