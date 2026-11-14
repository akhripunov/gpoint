#!/usr/bin/env bash
# Bounded, retrying SSH/SCP transport for the protected Layer7 staging job.
#
# The owner Mac runner intermittently loses TCP connections to the Layer7
# staging host before the SSH banner completes ("Connection closed by <host>
# port 22"). Those are transport failures: the remote command did not run, so a
# bounded retry is safe. An SSH session that exits non-zero is treated as a
# remote command failure and is never retried. SCP transfers are idempotent, so
# they retry on any non-zero status.
#
# Host-key, identity and keepalive policy lives here so every staging call site
# shares exactly one definition.
set -uo pipefail

mode="${1:-}"
if [ "$mode" != "ssh" ] && [ "$mode" != "scp" ]; then
  echo "usage: layer7-transport.sh ssh|scp [arguments...]" >&2
  exit 64
fi
shift

attempts="${LAYER7_TRANSPORT_ATTEMPTS:-5}"
backoff="${LAYER7_TRANSPORT_BACKOFF_SECONDS:-4}"
connect_timeout="${LAYER7_TRANSPORT_CONNECT_TIMEOUT:-20}"
identity="${LAYER7_SSH_KEY:-${GPOINT_SSH_KEY:-}}"
[ -n "$identity" ] || { echo 'LAYER7_SSH_KEY or GPOINT_SSH_KEY is required' >&2; exit 78; }
known_hosts="${LAYER7_KNOWN_HOSTS_FILE:-${GITHUB_WORKSPACE:-.}/infra/staging/layer7_known_hosts}"

# Replay stdin so a retried `ssh host python3 - < script.py` still receives the
# script. CI steps have stdin on /dev/null; an interactive terminal is never
# buffered.
replayable_stdin=""
cleanup() { [ -n "$replayable_stdin" ] && rm -f "$replayable_stdin"; }
trap cleanup EXIT
if [ ! -t 0 ]; then
  replayable_stdin="$(mktemp)"
  cat > "$replayable_stdin"
fi

run_once() {
  local stdin_source="/dev/null"
  if [ -n "$replayable_stdin" ]; then
    stdin_source="$replayable_stdin"
  fi
  if [ "$mode" = "ssh" ]; then
    ssh -F /dev/null -i "$identity" -T \
      -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
      -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
      -o ConnectionAttempts=3 -o ConnectTimeout="$connect_timeout" \
      -o UserKnownHostsFile="$known_hosts" "$@" < "$stdin_source"
    return $?
  fi
  scp -q -F /dev/null -i "$identity" \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o ConnectionAttempts=3 -o ConnectTimeout="$connect_timeout" \
    -o UserKnownHostsFile="$known_hosts" "$@" < "$stdin_source"
  return $?
}

retryable() {
  if [ "$mode" = "scp" ]; then
    return 0
  fi
  [ "$1" -eq 255 ]
}

attempt=1
while :; do
  run_once "$@"
  status=$?
  if [ "$status" -eq 0 ]; then
    exit 0
  fi
  if ! retryable "$status" || [ "$attempt" -ge "$attempts" ]; then
    echo "layer7-transport: $mode failed with status $status after $attempt attempt(s)" >&2
    exit "$status"
  fi
  echo "layer7-transport: $mode transport failure (status $status); retry $attempt/$attempts in $(( attempt * backoff ))s" >&2
  sleep $(( attempt * backoff ))
  attempt=$(( attempt + 1 ))
done
