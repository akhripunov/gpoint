#!/usr/bin/env bash
# Remote half of scripts/gpoint-backup-install.sh. Runs on the VPS through ssh,
# and is executed directly by the tests against an isolated ROOT.
#
# Environment:
#   ROOT                 filesystem prefix ("" in production, a temp dir in tests)
#   STAGE                staging directory, already created and owned by root
#   RELEASE              versioned release directory for the exact SHA
#   ARCHIVE_SHA          sha256 of the transferred payload archive
#   RUN_ID / ATTEMPT     identity of this installation operation
#   SYSTEMCTL            systemctl binary (overridable for tests)
set -euo pipefail

ROOT="${ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'
  fi
}
sha256_stream() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | awk '{print $1}'
  else shasum -a 256 | awk '{print $1}'
  fi
}

[[ "$STAGE" =~ ^${ROOT}/run/gpoint-backup-install-[0-9]+-[0-9]+$ ]]
[[ "$RELEASE" =~ ^${ROOT}/opt/gpoint/backup-releases/[0-9a-f]{40}$ ]]
[[ "$ARCHIVE_SHA" =~ ^[0-9a-f]{64}$ ]]
[[ "$RUN_ID" =~ ^[0-9]+$ && "$ATTEMPT" =~ ^[0-9]+$ ]]
test "$(sha256 "$STAGE/payload.tar.gz")" = "$ARCHIVE_SHA"

agekey="$ROOT/opt/gpoint/env/gpoint-backup.agekey"
expected_key_owner="${EXPECTED_KEY_OWNER:-0}"
test -f "$agekey" && test ! -L "$agekey"
if stat -c '%a:%u' "$agekey" >/dev/null 2>&1; then
  test "$(stat -c '%a:%u' "$agekey")" = "600:$expected_key_owner"
else
  test "$(stat -f '%Lp:%u' "$agekey")" = "600:$expected_key_owner"
fi

manifest="$ROOT/opt/gpoint/backups/scheduled/latest.json"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
previous_manifest_sha="NONE"
if [ -f "$manifest" ]; then
  previous_manifest_sha="$(sha256 "$manifest")"
fi

# Reconcile an existing release instead of overwriting it. The identity of the
# payload is a content hash, not the archive bytes, so a retried run of the same
# tree is idempotent while a different tree can never replace a working version.
payload_dir="$STAGE/payload"
mkdir -p "$payload_dir"
tar -xzf "$STAGE/payload.tar.gz" -C "$payload_dir" --no-same-owner
payload_id="$(cd "$payload_dir" && find . -type f -print | LC_ALL=C sort | while IFS= read -r file; do
  printf '%s  %s\n' "$(sha256 "$file")" "$file"
done | sha256_stream)"
[[ "$payload_id" =~ ^[0-9a-f]{64}$ ]]

stamp="$RELEASE/PAYLOAD.sha256"
reuse=false
if [ -e "$RELEASE" ]; then
  test -f "$stamp" || { echo "existing backup release has no payload stamp" >&2; exit 1; }
  test "$(cat "$stamp")" = "$payload_id" || {
    echo "existing backup release differs from this payload; refusing to overwrite" >&2
    exit 1
  }
  reuse=true
fi

if [ "$reuse" != true ]; then
  test ! -e "$RELEASE"
  install -d -m 0700 "$RELEASE"
  cp -a "$payload_dir/." "$RELEASE/"
  find "$RELEASE" -type d -exec chmod 0755 {} +
  find "$RELEASE/scripts" -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' \) -exec chmod 0755 {} +
  printf '%s' "$payload_id" > "$stamp"
  chmod 0644 "$stamp"
fi

current="$ROOT/opt/gpoint/gpoint-backup-current"
if [ "$(readlink "$current" 2>/dev/null || true)" != "$RELEASE" ]; then
  ln -sfn "$RELEASE" "$current"
fi

if [ -n "$ROOT" ]; then install -d -m 0755 "$ROOT/etc/systemd/system"; fi
install -m 0644 "$RELEASE/systemd/gpoint-backup.service" "$ROOT/etc/systemd/system/gpoint-backup.service"
install -m 0644 "$RELEASE/systemd/gpoint-backup.timer" "$ROOT/etc/systemd/system/gpoint-backup.timer"

"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now gpoint-backup.timer
"$SYSTEMCTL" start gpoint-backup.service
for _ in $(seq 1 10800); do
  state="$("$SYSTEMCTL" show gpoint-backup.service --property=ActiveState --value)"
  [[ "$state" == inactive ]] && break
  [[ "$state" == failed ]] && { echo "scheduled backup failed" >&2; exit 1; }
  sleep 10
done
test "$("$SYSTEMCTL" is-enabled gpoint-backup.timer)" = enabled
"$SYSTEMCTL" is-active --quiet gpoint-backup.timer

# The first backup must be a *new* snapshot of this operation, not an old file.
test -f "$manifest"
if [ "$(sha256 "$manifest")" = "$previous_manifest_sha" ]; then
  echo "backup manifest did not change; the first backup did not run" >&2
  exit 1
fi
python3 - "$manifest" "$started_at" <<'PY'
import datetime
import json
import sys

path, started = sys.argv[1], sys.argv[2]
manifest = json.load(open(path, encoding="utf-8"))
if manifest.get("status") != "PASS":
    raise SystemExit("first backup did not pass")
completed = datetime.datetime.fromisoformat(str(manifest["completed_at"]).replace("Z", "+00:00"))
begin = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
if completed.tzinfo is None:
    completed = completed.replace(tzinfo=begin.tzinfo)
if completed < begin:
    raise SystemExit("latest backup predates this installation")
PY
python3 "$ROOT/opt/gpoint/gpoint-backup-current/scripts/gpoint-backup-policy.py" freshness \
  --manifest "$manifest" --max-age-hours 26
echo GPOINT_VPS_BACKUP_TIMER=PASS
rm -rf -- "$STAGE"
