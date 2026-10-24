#!/bin/sh
set -eu

if [ -z "${EROS_PILOT_VPS_SSH_TARGET:-}" ]; then
  echo "EROS_PILOT_VPS_SSH_TARGET_REQUIRED" >&2
  exit 2
fi

ssh_identity=${GPOINT_SSH_IDENTITY_FILE:-}
if [ -z "$ssh_identity" ]; then
  config_file=${GPOINT_RELEASE_CONFIG:-${HOME:-}/.config/gpoint/release-tools.env}
  if [ -r "$config_file" ]; then
    ssh_identity=$(sed -n 's/^GPOINT_SSH_IDENTITY_FILE=//p' "$config_file" | tail -n 1)
    ssh_identity=${ssh_identity#\"}; ssh_identity=${ssh_identity%\"}
    ssh_identity=${ssh_identity#\'}; ssh_identity=${ssh_identity%\'}
    case "$ssh_identity" in
      '$HOME') ssh_identity=${HOME:-} ;;
      '$HOME/'*) ssh_identity="${HOME:-}/${ssh_identity#\$HOME/}" ;;
      '~') ssh_identity=${HOME:-} ;;
      '~/'*) ssh_identity="${HOME:-}/${ssh_identity#\~/}" ;;
    esac
  fi
fi
if [ -z "$ssh_identity" ] || [ ! -r "$ssh_identity" ]; then
  echo "GPOINT_SSH_IDENTITY_FILE_REQUIRED" >&2
  exit 2
fi

node --input-type=module <<'NODE'
const expected = "ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d";
try {
  const response = await fetch("http://127.0.0.1:11434/api/tags", { signal: AbortSignal.timeout(5000) });
  const payload = await response.json();
  const model = payload.models?.find((entry) => entry.name === "qwen3-embedding:0.6b");
  if (!response.ok || model?.digest !== expected) throw new Error("model mismatch");
  console.log("EROS_PILOT_LOCAL_QWEN_DIGEST=PASS");
} catch {
  console.error("EROS_PILOT_LOCAL_OLLAMA_MODEL_UNAVAILABLE_OR_MISMATCH");
  process.exit(1);
}
NODE

stopping=0
ssh_pid=
sleep_pid=
stop_tunnel() {
  stopping=1
  if [ -n "$ssh_pid" ]; then kill "$ssh_pid" 2>/dev/null || true; fi
  if [ -n "$sleep_pid" ]; then kill "$sleep_pid" 2>/dev/null || true; fi
  if [ -n "$ssh_pid" ]; then wait "$ssh_pid" 2>/dev/null || true; fi
  if [ -n "$sleep_pid" ]; then wait "$sleep_pid" 2>/dev/null || true; fi
}
trap stop_tunnel INT TERM HUP

while [ "$stopping" -eq 0 ]; do
  ssh -N -T \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o IdentitiesOnly=yes \
  -i "$ssh_identity" \
  -o ExitOnForwardFailure=yes \
  -o GatewayPorts=no \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:11435:127.0.0.1:11434 \
    "$EROS_PILOT_VPS_SSH_TARGET" &
  ssh_pid=$!
  if wait "$ssh_pid"; then
    ssh_status=0
  else
    ssh_status=$?
  fi
  ssh_pid=
  [ "$stopping" -eq 1 ] && break
  echo "EROS_PILOT_TUNNEL_DISCONNECTED ssh_exit=$ssh_status retry_in_seconds=2" >&2
  sleep 2 &
  sleep_pid=$!
  wait "$sleep_pid" || true
  sleep_pid=
done
