#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 4 ]]; then
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
test ! -e "$backup" && test ! -e "${backup}.sha256" || {
  echo 'Caddy recovery copy already exists for this exact workflow attempt' >&2
  exit 1
}
install -d -m 0700 "$backup_dir"
install -m 0600 "$current_caddy" "$backup"
sha256sum "$backup" > "${backup}.sha256"
sha256sum --check --status "${backup}.sha256"
test "$(awk '{print $1}' "${backup}.sha256")" = "$expected_caddy_sha"

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
