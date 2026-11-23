#!/usr/bin/env bash
# Single pinned Kamal CLI entrypoint for the verified production release.
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: run-pinned-kamal.sh inventory|apply|rollback .|rollback-core|rollback-miniapp KAMAL_ARGS..." >&2
  exit 2
fi
phase="$1"; checkout="$2"; shift 2
case "$phase:$checkout" in
  inventory:.|apply:.|rollback:.|rollback:rollback-core|rollback:rollback-miniapp) ;;
  *) echo "unsupported Kamal phase/checkout" >&2; exit 2 ;;
esac
[[ "${GITHUB_WORKSPACE:-}" == /* && -d "${HOME:-}/.ssh" ]] || { echo "Kamal runner workspace or SSH is missing" >&2; exit 1; }
checkout_dir="$GITHUB_WORKSPACE"
if [[ "$checkout" != "." ]]; then checkout_dir="$GITHUB_WORKSPACE/$checkout"; fi
[[ -d "$checkout_dir" ]] || { echo "verified Kamal checkout is missing" >&2; exit 1; }

pinned="ghcr.io/basecamp/kamal:v2.12.0@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625"
[[ -z "${KAMAL_IMAGE:-}" || "$KAMAL_IMAGE" == "$pinned" ]] || { echo "Kamal CLI image pin mismatch" >&2; exit 1; }

env_args=(--env GPOINT_COMMIT_SHA --env GPOINT_APP_SOURCE_SHA
          --env GPOINT_PRODUCTION_HOST --env GPOINT_SSH_KEY
          --env GPOINT_KNOWN_HOSTS_FILE --env KAMAL_REGISTRY_PASSWORD)
if [[ "$phase" != inventory ]]; then
  env_args+=(--env GPOINT_KAMAL_EXPECTED_DIGEST --env GPOINT_KAMAL_SERVICE --env KAMAL_ROLES)
fi
if [[ "$phase" == apply ]]; then
  env_args+=(--env GPOINT_WORKER_PROXY_DIGEST --env GPOINT_KAMAL_CORE_API_VERSION
             --env GPOINT_KAMAL_CORE_API_SOURCE_SHA)
fi

docker run --rm --pull=never --platform linux/amd64 \
  --volume "$checkout_dir:/workdir" \
  --volume "$HOME/.ssh:/root/.ssh:ro" \
  --volume "$HOME/.ssh:$HOME/.ssh:ro" \
  --workdir /workdir "${env_args[@]}" "$pinned" "$@"
