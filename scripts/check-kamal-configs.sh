#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${KAMAL_IMAGE:?KAMAL_IMAGE must be the pinned Kamal image}"
[[ "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "GITHUB_SHA must be a full lowercase commit SHA" >&2
  exit 2
}

expected_kamal_image='ghcr.io/basecamp/kamal:v2.12.0@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625'
[[ "$KAMAL_IMAGE" == "$expected_kamal_image" ]] || {
  echo "KAMAL_IMAGE does not match the pinned Kamal 2.12.0 image" >&2
  exit 2
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
temp_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
test_dir="$(mktemp -d "$temp_root/gpoint-kamal-config.XXXXXX")"
chmod 700 "$test_dir"
output_file="$test_dir/config-output.log"
ssh_key="$test_dir/id_ed25519"
synthetic_registry_password='config-validation-placeholder'

cleanup() {
  rm -rf "$test_dir"
}
trap cleanup EXIT

if ! ssh-keygen -q -t ed25519 -N '' -C kamal-config-validation -f "$ssh_key"; then
  echo "Unable to create a temporary synthetic SSH key for Kamal config validation" >&2
  exit 2
fi

docker_mount_args=(--volume "$repo_root:/workdir:ro")
if [[ -f "$repo_root/.git" ]]; then
  git_common_dir="$(git -C "$repo_root" rev-parse --path-format=absolute --git-common-dir)"
  docker_mount_args+=(--volume "$repo_root/.git:$repo_root/.git:ro")
  docker_mount_args+=(--volume "$git_common_dir:$git_common_dir:ro")
fi

run_config() {
  local config_file="$1"
  local status
  shift

  if docker run --rm --platform linux/amd64 --network none \
    "${docker_mount_args[@]}" \
    --workdir /workdir \
    --volume "$ssh_key:/tmp/gpoint-kamal-config-test-key:ro" \
    --env "GPOINT_COMMIT_SHA=$GITHUB_SHA" \
    --env GPOINT_SSH_KEY=/tmp/gpoint-kamal-config-test-key \
    --env GPOINT_KNOWN_HOSTS_FILE=/dev/null \
    --env GPOINT_PRODUCTION_HOST=192.0.2.10 \
    --env "KAMAL_REGISTRY_PASSWORD=$synthetic_registry_password" \
    "$KAMAL_IMAGE" "$@" --config-file "$config_file" >"$output_file" 2>&1; then
    return 0
  else
    status=$?
  fi

  printf 'Kamal config validation failed for %s (exit status %s)\n' \
    "$config_file" "$status" >&2
  sed \
    -e "s/${synthetic_registry_password}/[redacted synthetic registry credential]/g" \
    -e "s|/tmp/gpoint-kamal-config-test-key|[synthetic SSH key path]|g" \
    "$output_file" >&2
  return "$status"
}

for config_file in \
  config/deploy.yml \
  config/deploy.agata.yml \
  config/deploy.miniapp.yml; do
  run_config "$config_file" config
  run_config "$config_file" secrets print
  grep -Fq "KAMAL_REGISTRY_PASSWORD=$synthetic_registry_password" "$output_file" || {
    echo "Kamal registry credential did not resolve from the synthetic environment" >&2
    exit 1
  }
done
