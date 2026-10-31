#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 4 && !( "$#" -eq 5 && "$5" == --prepare-only ) ]]; then
  echo "usage: retire-gpoint-caddy.sh <candidate-sha> <run-id> <attempt> <expected-caddy-sha256>" >&2
  exit 2
fi

candidate_sha="$1"
run_id="$2"
attempt="$3"
expected_caddy_sha="$4"
current_caddy="${GPOINT_CADDY_CONFIG_PATH:-/etc/caddy/Caddyfile}"
backup_dir="${GPOINT_CADDY_BACKUP_DIR:-/opt/gpoint/backups}"

[[ "$candidate_sha" =~ ^[0-9a-f]{40}$ ]]
[[ "$run_id" =~ ^[0-9]+$ && "$attempt" =~ ^[0-9]+$ ]]
[[ "$expected_caddy_sha" =~ ^[0-9a-f]{64}$ ]]
test -s "$current_caddy"
systemctl is-active --quiet caddy || { echo 'Caddy is not active before the guarded cutover' >&2; exit 1; }

actual_caddy_sha="$(sha256sum "$current_caddy" | awk '{print $1}')"
test "$actual_caddy_sha" = "$expected_caddy_sha" || {
  echo 'Caddy changed after the fresh cutover baseline' >&2
  exit 1
}

backup="$backup_dir/caddy-before-kamal-${candidate_sha}-${run_id}-${attempt}"
install -d -m 0700 "$backup_dir"
if [[ -e "$backup" || -e "${backup}.sha256" || -L "$backup" || -L "${backup}.sha256" ]]; then
  # The server owner prepares this immutable recovery material BEFORE pause.
  # Reuse is permitted only for the exact protected copy, never overwrite it.
  python3 - "$backup" "${backup}.sha256" "$expected_caddy_sha" <<'PY'
import hashlib, os, stat, sys
from pathlib import Path
backup, checksum = map(Path, sys.argv[1:3])
for path in (backup, checksum):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit('unsafe Caddy recovery material')
if hashlib.sha256(backup.read_bytes()).hexdigest() != sys.argv[3]:
    raise SystemExit('Caddy backup differs from baseline')
if checksum.read_text() != f'{sys.argv[3]}  {backup}\n':
    raise SystemExit('Caddy checksum identity differs')
PY
else
  install -m 0600 "$current_caddy" "$backup"
  (umask 077; sha256sum "$backup" > "${backup}.sha256")
fi
sha256sum --check --status "${backup}.sha256"
test "$(awk '{print $1}' "${backup}.sha256")" = "$expected_caddy_sha"

if [[ "${5:-}" == --prepare-only ]]; then
  echo 'GPOINT_CADDY_BACKUP_READY=PASS'
  exit 0
fi

if ! systemctl stop caddy; then
  systemctl start caddy || true
  echo 'Failed to stop Caddy for the Kamal Proxy public bind' >&2
  exit 1
fi
if systemctl is-active --quiet caddy; then
  systemctl start caddy || true
  echo 'Caddy remained active after stop; the cutover must not continue' >&2
  exit 1
fi

printf 'GPOINT_CADDY_RETIRED=PASS\nCADDYFILE_SHA256=%s\nCADDY_BACKUP_PATH=%s\n' \
  "$expected_caddy_sha" "$backup"
