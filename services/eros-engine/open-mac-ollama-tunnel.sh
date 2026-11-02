#!/bin/sh
set -eu

: "${EROS_PILOT_VPS_SSH_TARGET:?EROS_PILOT_VPS_SSH_TARGET_REQUIRED}"
ssh_identity=${GPOINT_SSH_IDENTITY_FILE:-}
known_hosts=${GPOINT_KNOWN_HOSTS_FILE:-}
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
[ -n "$ssh_identity" ] && [ -r "$ssh_identity" ] || { echo 'GPOINT_SSH_IDENTITY_FILE_REQUIRED' >&2; exit 2; }
[ -n "$known_hosts" ] || known_hosts=${HOME:-}/.ssh/known_hosts
[ -r "$known_hosts" ] || { echo 'GPOINT_KNOWN_HOSTS_FILE_REQUIRED' >&2; exit 2; }

script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
node_bin=${EROS_PILOT_NODE_BIN:-$(command -v node)}
verifier="$script_dir/verify-mac-ollama-tags.mjs"
[ -x "$node_bin" ] && [ -r "$verifier" ] || { echo 'EROS_PILOT_LOCAL_OLLAMA_VERIFIER_MISSING' >&2; exit 1; }
EROS_PILOT_OLLAMA_URL=http://127.0.0.1:11434 "$node_bin" "$verifier" local

stopping=0
ssh_pid=
sleep_pid=
stop_tunnel() {
  stopping=1
  [ -z "$ssh_pid" ] || kill "$ssh_pid" 2>/dev/null || true
  [ -z "$sleep_pid" ] || kill "$sleep_pid" 2>/dev/null || true
  [ -z "$ssh_pid" ] || wait "$ssh_pid" 2>/dev/null || true
  [ -z "$sleep_pid" ] || wait "$sleep_pid" 2>/dev/null || true
}
trap stop_tunnel INT TERM HUP

while [ "$stopping" -eq 0 ]; do
  ssh -F /dev/null -N -T \
    -o BatchMode=yes \
    -o ClearAllForwardings=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$known_hosts" \
    -o IdentitiesOnly=yes \
    -i "$ssh_identity" \
    -o ExitOnForwardFailure=yes \
    -o GatewayPorts=no \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -R 127.0.0.1:11435:127.0.0.1:11434 \
    "$EROS_PILOT_VPS_SSH_TARGET" &
  ssh_pid=$!
  verified=0
  for attempt in $(seq 1 30); do
    kill -0 "$ssh_pid" 2>/dev/null || break
    if ssh -F /dev/null -T \
      -o BatchMode=yes \
      -o ClearAllForwardings=yes \
      -o StrictHostKeyChecking=yes \
      -o UserKnownHostsFile="$known_hosts" \
      -o IdentitiesOnly=yes \
      -i "$ssh_identity" \
      -o ConnectTimeout=5 \
      "$EROS_PILOT_VPS_SSH_TARGET" \
      'curl --fail --silent --show-error --max-time 5 http://127.0.0.1:11435/api/tags' \
      | "$node_bin" "$verifier" --stdin vps-loopback >/dev/null 2>&1; then
      verified=1
      break
    fi
    sleep 2
  done
  if [ "$verified" -eq 1 ]; then
    echo 'EROS_PILOT_REMOTE_QWEN3=PASS'
    if wait "$ssh_pid"; then ssh_status=0; else ssh_status=$?; fi
  else
    echo 'EROS_PILOT_REMOTE_QWEN3=RETRY' >&2
    kill "$ssh_pid" 2>/dev/null || true
    if wait "$ssh_pid"; then ssh_status=0; else ssh_status=$?; fi
  fi
  ssh_pid=
  [ "$stopping" -eq 1 ] && break
  echo "EROS_PILOT_TUNNEL_DISCONNECTED ssh_exit=$ssh_status retry_in_seconds=5" >&2
  sleep 5 &
  sleep_pid=$!
  wait "$sleep_pid" || true
  sleep_pid=
done
