#!/bin/sh
set -eu

model='qwen3-embedding:0.6b'
export OLLAMA_HOST=127.0.0.1:11434

ollama serve >/tmp/eros-pilot-ollama-pull.log 2>&1 &
ollama_pid=$!
cleanup() {
  kill "$ollama_pid" 2>/dev/null || true
  wait "$ollama_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

attempt=0
until ollama list >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo 'EROS_PILOT_OLLAMA_START_FAILED' >&2
    exit 1
  fi
  sleep 1
done

ollama pull "$model"
echo 'EROS_PILOT_MODEL_PULL=COMPLETE'
