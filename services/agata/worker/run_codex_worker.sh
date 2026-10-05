#!/bin/sh
set -eu

HOME="${HOME:?HOME must be set}"
RUNTIME_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd -P)"
SUPPORT="$HOME/Downloads/Codex/Workers/AGATA Workers"
POOL_CONFIG="$SUPPORT/config/pool.env"
PYTHON="$RUNTIME_DIR/.venv/bin/python3.12"
CODEX_BIN="/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
WORKER="$RUNTIME_DIR/gpoint_mac_worker.py"
COMMAND="${1:-watch}"

export HOME
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONUNBUFFERED=1
export GPOINT_CODEX_CLI="$CODEX_BIN"
export GPOINT_AGATA_WORKER_CONFIG="${GPOINT_AGATA_WORKER_CONFIG:-$HOME/.config/gpoint/agata-worker.env}"
export GPOINT_AGATA_WORKER_SUPPORT="$SUPPORT"
export AGATA_WORKER_POOL_CONFIG="${AGATA_WORKER_POOL_CONFIG:-$POOL_CONFIG}"

cd "$RUNTIME_DIR"
exec "$PYTHON" "$WORKER" "$COMMAND"
