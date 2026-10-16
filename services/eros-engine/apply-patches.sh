#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  printf '%s\n' 'usage: apply-patches.sh /path/to/eros-engine-v2.2.1' >&2
  exit 2
fi

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
source_dir=$1
expected_sha=d1a140083321687212fb7b17c09cc0c3c9639f5e
patch_file="$repo_root/services/eros-engine/patches/0001-provider-and-startup-gates.patch"
ledger_patch_file="$repo_root/services/eros-engine/patches/0002-isolate-sqlx-ledger-in-engine-schema.patch"
bridge_patch_file="$repo_root/services/eros-engine/patches/0003-gpoint-worker-bridge-identity.patch"

actual_sha=$(git -C "$source_dir" rev-parse HEAD)
if [ "$actual_sha" != "$expected_sha" ]; then
  printf 'eros_source_revision_mismatch: expected=%s actual=%s\n' "$expected_sha" "$actual_sha" >&2
  exit 1
fi
if [ -n "$(git -C "$source_dir" status --porcelain)" ]; then
  printf '%s\n' 'eros_source_not_clean' >&2
  exit 1
fi

git -C "$source_dir" apply --unidiff-zero --check "$patch_file"
git -C "$source_dir" apply --unidiff-zero --check "$ledger_patch_file"
git -C "$source_dir" apply --unidiff-zero --check "$bridge_patch_file"
git -C "$source_dir" apply --unidiff-zero "$patch_file"
git -C "$source_dir" apply --unidiff-zero "$ledger_patch_file"
git -C "$source_dir" apply --unidiff-zero "$bridge_patch_file"
printf 'EROS_PATCH_APPLIED=PASS\nEROS_SOURCE_SHA=%s\n' "$actual_sha"
