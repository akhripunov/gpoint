#!/bin/sh
# Build an explicitly pinned, unmodified upstream Eros image locally.
set -eu

expected_sha=d1a140083321687212fb7b17c09cc0c3c9639f5e
if [ "$#" -ne 2 ]; then
  printf '%s\n' 'usage: build-stock-image.sh <clean-upstream-checkout> <local-image-tag>' >&2
  exit 2
fi
source_dir=$(CDPATH= cd -- "$1" && pwd -P)
image_tag=$2
case "$image_tag" in
  *[!A-Za-z0-9._:/-]*|'') printf '%s\n' 'invalid_local_image_tag' >&2; exit 2 ;;
esac
command -v docker >/dev/null || { printf '%s\n' 'docker_required' >&2; exit 2; }
actual_sha=$(git -C "$source_dir" rev-parse HEAD)
if [ "$actual_sha" != "$expected_sha" ]; then
  printf 'eros_source_revision_mismatch: expected=%s actual=%s\n' "$expected_sha" "$actual_sha" >&2
  exit 1
fi
if [ -n "$(git -C "$source_dir" status --porcelain)" ]; then
  printf '%s\n' 'eros_source_not_clean' >&2
  exit 1
fi
docker build \
  --label 'org.opencontainers.image.source=https://github.com/etherfunlab/eros-engine' \
  --label "org.opencontainers.image.revision=$actual_sha" \
  --label 'com.gpoint.eros.upstream-revision=d1a140083321687212fb7b17c09cc0c3c9639f5e' \
  --label 'com.gpoint.eros.patch-set.sha256=none' \
  --tag "$image_tag" \
  --file "$source_dir/docker/Dockerfile" \
  "$source_dir"
image_id=$(docker image inspect "$image_tag" --format '{{.Id}}')
printf 'STOCK_EROS_BUILD=PASS\nEROS_SOURCE_SHA=%s\nEROS_IMAGE_ID=%s\n' "$actual_sha" "$image_id"
