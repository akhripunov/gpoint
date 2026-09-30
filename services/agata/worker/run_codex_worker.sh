#!/bin/sh
set -eu

HOME="${HOME:?HOME must be set}"
SUPPORT="$HOME/Library/Application Support/GPoint/agata-codex-worker"
PYTHON="$SUPPORT/.venv/bin/python3.12"
CODEX_BIN="/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
WORKER="$SUPPORT/gpoint_mac_worker.py"
COMMAND="${1:-watch}"

export HOME
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONUNBUFFERED=1
export GPOINT_CODEX_CLI="$CODEX_BIN"
export GPOINT_AGATA_WORKER_CONFIG="${GPOINT_AGATA_WORKER_CONFIG:-$HOME/.config/gpoint/agata-worker.env}"
export GPOINT_AGATA_WORKER_SUPPORT="$SUPPORT"

cd "$SUPPORT"
exec "$PYTHON" "$WORKER" "$COMMAND"
