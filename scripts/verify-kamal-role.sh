#!/usr/bin/env bash
# Keep every exact-role/SHA/digest assertion, with one Docker inspect per object.
set -euo pipefail

prefix="${1:-}"
version="${2:-}"
expected_sha="${3:-}"

case "$prefix" in
  gpoint-core-api) expected_service=gpoint-core; expected_role=api; repository=ghcr.io/akhripunov/gpoint-runtime ;;
  gpoint-core-worker) expected_service=gpoint-core; expected_role=worker; repository=ghcr.io/akhripunov/gpoint-runtime ;;
  gpoint-core-bot) expected_service=gpoint-core; expected_role=bot; repository=ghcr.io/akhripunov/gpoint-runtime ;;
  gpoint-miniapp-web) expected_service=gpoint-miniapp; expected_role=web; repository=ghcr.io/akhripunov/gpoint-miniapp ;;
  gpoint-eros-pilot-engine) expected_service=gpoint-eros-pilot; expected_role=engine; repository=ghcr.io/akhripunov/gpoint-eros ;;
  *) echo "unsupported Kamal role prefix" >&2; exit 2 ;;
esac
[[ "$version" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$ ]] || { echo "invalid Kamal version" >&2; exit 2; }
[[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid expected source SHA" >&2; exit 2; }

running_names="$(docker ps --no-trunc --format '{{.Names}}')"
matching_names=()
while IFS= read -r name; do
  [[ "$name" =~ ^${prefix}-[0-9a-f]{40}-[0-9]+-[0-9]+$ ]] && matching_names+=("$name")
done <<< "$running_names"
count="${#matching_names[@]}"
if [[ "$count" -ne 1 ]]; then
  echo "expected exactly one running role $prefix, found $count" >&2
  exit 1
fi
expected_name="${prefix}-${version}"
[[ "${matching_names[0]}" == "$expected_name" ]] || { echo "running role does not match candidate version: $prefix" >&2; exit 1; }

# Inspect the container once; the running state is rechecked after docker ps.
container_facts="$(docker inspect --format '{{.State.Running}}|{{index .Config.Labels "service"}}|{{index .Config.Labels "role"}}|{{.Image}}' "$expected_name")"
IFS='|' read -r running service container_role image_id <<< "$container_facts"
[[ "$running" == true ]] || { echo "role is not running: $prefix" >&2; exit 1; }
[[ "$service" == "$expected_service" && "$container_role" == "$expected_role" ]] || {
  echo "role service labels do not match expected identity: $prefix" >&2; exit 1;
}
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "role Docker image ID is invalid: $prefix" >&2; exit 1; }

# One immutable image read checks both GHCR RepoDigest and the source revision.
image_format='{{json .RepoDigests}}|{{index .Config.Labels "org.opencontainers.image.revision"}}'
if [[ "$prefix" == gpoint-eros-pilot-engine ]]; then
  image_format='{{json .RepoDigests}}|{{index .Config.Labels "com.gpoint.source.revision"}}'
fi
image_facts="$(docker image inspect --format "$image_format" "$image_id")"
IFS='|' read -r repo_digests revision <<< "$image_facts"
python3 - "$repo_digests" "$repository" <<'PY'
import json, re, sys
refs=json.loads(sys.argv[1])
matches=[value for value in refs if re.fullmatch(re.escape(sys.argv[2])+r'@sha256:[0-9a-f]{64}',value)]
if len(matches)!=1:
    raise SystemExit('role image does not have exactly one immutable repository digest')
PY
[[ "$revision" == "$expected_sha" ]] || { echo "role source revision does not match expected SHA: $prefix" >&2; exit 1; }
printf 'ROLE_VERIFY=PASS role=%s version=%s source_sha=%s\n' "$prefix" "$version" "$expected_sha"
