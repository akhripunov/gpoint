#!/usr/bin/env bash
# Protected one-shot install/update of the GPoint scheduled VPS backups.
#
# The daily backups stay independent of GitHub: a systemd timer on the VPS runs
# gpoint-scheduled-backup.py, which writes encrypted snapshots to Bunny S3,
# reads them back and performs an isolated restore. This procedure only installs
# or updates that release and proves the first backup.
#
# Required environment:
#   GPOINT_PRODUCTION_HOST  production host name
#   CANDIDATE_SHA           exact 40-char origin/main SHA to install
#   APPROVE_PRODUCTION       must be "true" (owner authorization)
# Run from a clean repository checkout on the operator workstation.
set -euo pipefail

: "${GPOINT_PRODUCTION_HOST:?GPOINT_PRODUCTION_HOST is required}"
: "${CANDIDATE_SHA:?CANDIDATE_SHA is required}"
test "${APPROVE_PRODUCTION:-}" = true
[[ "$CANDIDATE_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$GPOINT_PRODUCTION_HOST" =~ ^[A-Za-z0-9.-]+$ ]]
test -z "$(git status --porcelain)"
test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"
test "$(git ls-remote origin refs/heads/main | awk '{print $1}')" = "$CANDIDATE_SHA"
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'
  fi
}
RUN_ID="${RUN_ID:-$(date -u +%Y%m%d%H%M%S)}"
ATTEMPT="${ATTEMPT:-1}"
[[ "$RUN_ID" =~ ^[0-9]+$ && "$ATTEMPT" =~ ^[0-9]+$ ]]
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "GPOINT_BACKUP_INSTALL=START SHA=$CANDIDATE_SHA"
set -euo pipefail
release="/opt/gpoint/backup-releases/$CANDIDATE_SHA"
stage="/run/gpoint-backup-install-${RUN_ID}-${ATTEMPT}"
ssh -T "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$stage" "$release" <<'REMOTE_SETUP'
set -euo pipefail
[[ "$1" =~ ^/run/gpoint-backup-install-[0-9]+-[0-9]+$ ]]
[[ "$2" =~ ^/opt/gpoint/backup-releases/[0-9a-f]{40}$ ]]
test ! -e "$1"
install -d -o root -g root -m 0700 "$1"
REMOTE_SETUP
tar -czf "$TMP/gpoint-vps-backup.tar.gz" scripts/gpoint-scheduled-backup.py scripts/gpoint-backup-policy.py scripts/gpoint-media-tree.py scripts/agata-production-migration.py scripts/verify-gpoint-scheduled-backup-restore.sh systemd/gpoint-backup.service systemd/gpoint-backup.timer
scp -p "$TMP/gpoint-vps-backup.tar.gz" "root@${GPOINT_PRODUCTION_HOST}:$stage/payload.tar.gz"
archive_sha="$(sha256 "$TMP/gpoint-vps-backup.tar.gz")"
ssh -T "root@${GPOINT_PRODUCTION_HOST}" \
  env ROOT= SYSTEMCTL=systemctl STAGE="$stage" RELEASE="$release" \
      ARCHIVE_SHA="$archive_sha" RUN_ID="$RUN_ID" ATTEMPT="$ATTEMPT" \
  bash -s < scripts/gpoint-backup-install-remote.sh

if [[ -n "${GITHUB_STEP_SUMMARY:-}" && -w "${GITHUB_STEP_SUMMARY}" ]]; then
  echo GPOINT_VPS_BACKUP_INSTALL=PASS >> "$GITHUB_STEP_SUMMARY"
fi
echo "GPOINT_BACKUP_INSTALL=PASS SHA=$CANDIDATE_SHA"
