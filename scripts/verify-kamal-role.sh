#!/usr/bin/env bash
set -euo pipefail

prefix="${1:-}"
version="${2:-}"
expected_sha="${3:-}"

case "$prefix" in
  gpoint-core-api|gpoint-core-worker|gpoint-core-bot|gpoint-agata-api|gpoint-agata-memory-worker|gpoint-miniapp-web) ;;
  *) echo "unsupported Kamal role prefix" >&2; exit 2 ;;
esac
[[ "$version" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$ ]] || { echo "invalid Kamal version" >&2; exit 2; }
[[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid expected source SHA" >&2; exit 2; }

running_names="$(docker ps --filter "name=^/${prefix}-" --format '{{.Names}}')"
if [[ -z "$running_names" ]]; then
  count=0
else
  count="$(printf '%s\n' "$running_names" | wc -l | tr -d ' ')"
fi
if [[ "$count" -ne 1 ]]; then
  echo "expected exactly one running role $prefix, found $count" >&2
  exit 1
fi

expected_name="${prefix}-${version}"
if [[ "$running_names" != "$expected_name" ]]; then
  echo "running role does not match candidate version: $prefix" >&2
  exit 1
fi

running="$(docker inspect --format '{{.State.Running}}' "$expected_name")"
[[ "$running" == true ]] || { echo "role is not running: $prefix" >&2; exit 1; }
revision="$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$expected_name")"
[[ "$revision" == "$expected_sha" ]] || { echo "role source revision does not match expected SHA: $prefix" >&2; exit 1; }
printf 'ROLE_VERIFY=PASS role=%s version=%s source_sha=%s\n' "$prefix" "$version" "$expected_sha"
