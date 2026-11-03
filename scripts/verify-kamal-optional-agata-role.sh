#!/usr/bin/env bash
set -euo pipefail

role="${1:-}"
case "$role" in
  api|memory-worker) ;;
  *) echo 'unsupported optional AGATA role' >&2; exit 2 ;;
esac

prefix="gpoint-agata-$role-"
matches="$(docker ps -q --filter "name=^/${prefix}")"
if [[ -n "$matches" ]] && [[ "$(wc -l <<< "$matches" | tr -d ' ')" -gt 1 ]]; then
  echo "duplicate optional AGATA role: $role" >&2
  exit 1
fi
if [[ -n "$matches" ]]; then
  printf 'ROLE_PRESENT=true\n'
else
  printf 'ROLE_PRESENT=false\n'
fi
