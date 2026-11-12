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
RUN_ID="${RUN_ID:-$(date -u +%Y%m%d%H%M%S)}"
ATTEMPT="${ATTEMPT:-1}"
[[ "$RUN_ID" =~ ^[0-9]+$ && "$ATTEMPT" =~ ^[0-9]+$ ]]
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "GPOINT_BACKUP_INSTALL=START SHA=$CANDIDATE_SHA"
set -euo pipefail
release="/opt/gpoint/backup-releases/$CANDIDATE_SHA"
stage="/run/gpoint-backup-install-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
ssh -T "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$stage" "$release" <<'REMOTE_SETUP'
set -euo pipefail
[[ "$1" =~ ^/run/gpoint-backup-install-[0-9]+-[0-9]+$ ]]
[[ "$2" =~ ^/opt/gpoint/backup-releases/[0-9a-f]{40}$ ]]
test ! -e "$1"
install -d -o root -g root -m 0700 "$1"
REMOTE_SETUP
tar -czf "$TMP/gpoint-vps-backup.tar.gz" scripts/gpoint-scheduled-backup.py scripts/gpoint-backup-policy.py scripts/gpoint-media-tree.py scripts/agata-production-migration.py scripts/verify-gpoint-scheduled-backup-restore.sh systemd/gpoint-backup.service systemd/gpoint-backup.timer
scp -p "$TMP/gpoint-vps-backup.tar.gz" "root@${GPOINT_PRODUCTION_HOST}:$stage/payload.tar.gz"
archive_sha="$(sha256sum "$TMP/gpoint-vps-backup.tar.gz" | awk '{print $1}')"
ssh -T "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$stage" "$release" "$archive_sha" "$RUN_ID" "$ATTEMPT" <<'REMOTE_INSTALL'
set -euo pipefail
stage="$1"; release="$2"; expected_sha="$3"; run_id="$4"; attempt="$5"
[[ "$stage" == "/run/gpoint-backup-install-${run_id}-${attempt}" ]]
[[ "$release" =~ ^/opt/gpoint/backup-releases/[0-9a-f]{40}$ ]]
[[ "$expected_sha" =~ ^[0-9a-f]{64}$ ]]
test "$(sha256sum "$stage/payload.tar.gz" | awk '{print $1}')" = "$expected_sha"
test -f /opt/gpoint/env/gpoint-backup.agekey && test ! -L /opt/gpoint/env/gpoint-backup.agekey
test "$(stat -c '%a:%u' /opt/gpoint/env/gpoint-backup.agekey)" = 600:0
test ! -e "$release"
install -d -o root -g root -m 0700 "$release"
tar -xzf "$stage/payload.tar.gz" -C "$release" --no-same-owner
find "$release" -type d -exec chmod 0755 {} +
chmod 0755 "$release/scripts/gpoint-scheduled-backup.py" "$release/scripts/gpoint-backup-policy.py" "$release/scripts/gpoint-media-tree.py" "$release/scripts/agata-production-migration.py" "$release/scripts/verify-gpoint-scheduled-backup-restore.sh"
ln -s "$release" "/opt/gpoint/.backup-current-${run_id}-${attempt}"
mv -Tf "/opt/gpoint/.backup-current-${run_id}-${attempt}" /opt/gpoint/gpoint-backup-current
install -m 0644 "$release/systemd/gpoint-backup.service" /etc/systemd/system/gpoint-backup.service
install -m 0644 "$release/systemd/gpoint-backup.timer" /etc/systemd/system/gpoint-backup.timer
systemctl daemon-reload
systemctl enable --now gpoint-backup.timer
systemctl start gpoint-backup.service
for _ in $(seq 1 10800); do
  state="$(systemctl show gpoint-backup.service --property=ActiveState --value)"
  [[ "$state" == inactive ]] && break
  [[ "$state" == failed ]] && { journalctl -u gpoint-backup.service -n 30 --no-pager; exit 1; }
  sleep 10
done
test "$(systemctl is-enabled gpoint-backup.timer)" = enabled
systemctl is-active --quiet gpoint-backup.timer
python3 /opt/gpoint/gpoint-backup-current/scripts/gpoint-backup-policy.py freshness --manifest /opt/gpoint/backups/scheduled/latest.json --max-age-hours 26
echo GPOINT_VPS_BACKUP_TIMER=PASS
rm -rf -- "$stage"
REMOTE_INSTALL
echo GPOINT_VPS_BACKUP_INSTALL=PASS >> "$GITHUB_STEP_SUMMARY"

echo "GPOINT_BACKUP_INSTALL=PASS SHA=$CANDIDATE_SHA"
