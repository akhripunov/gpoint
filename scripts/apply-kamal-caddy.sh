#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 5 ]]; then
  echo "usage: apply-kamal-caddy.sh <candidate-sha> <caddy-sha256> <run-id> <attempt> <candidate-file>" >&2
  exit 2
fi

candidate_sha="$1"
expected_caddy_sha="$2"
run_id="$3"
attempt="$4"
candidate_file="$5"
current_caddy="${GPOINT_CADDY_CONFIG_PATH:-/etc/caddy/Caddyfile}"
backup_dir="${GPOINT_CADDY_BACKUP_DIR:-/opt/gpoint/backups}"

[[ "$candidate_sha" =~ ^[0-9a-f]{40}$ ]]
[[ "$expected_caddy_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$run_id" =~ ^[0-9]+$ && "$attempt" =~ ^[0-9]+$ ]]
[[ "$candidate_file" == "/tmp/gpoint-caddy-${candidate_sha}-${run_id}-${attempt}" ]]
test -s "$candidate_file"
test -f "$current_caddy"

actual_caddy_sha="$(sha256sum "$candidate_file" | awk '{print $1}')"
test "$actual_caddy_sha" = "$expected_caddy_sha" || {
  echo "candidate Caddyfile checksum mismatch" >&2
  exit 1
}
caddy validate --config "$candidate_file" --adapter caddyfile

backup="$backup_dir/caddy-before-${candidate_sha}-${run_id}-${attempt}"
test ! -e "$backup" && test ! -e "${backup}.sha256" || {
  echo "Caddy backup already exists for this exact workflow attempt" >&2
  exit 1
}
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
install -m 600 "$current_caddy" "$backup"
sha256sum "$backup" > "${backup}.sha256"
sha256sum --check --status "${backup}.sha256"

current_dir="$(dirname "$current_caddy")"
candidate_install=""
trap '[[ -z "${candidate_install:-}" ]] || rm -f "$candidate_install"' EXIT
candidate_install="$(mktemp "${current_dir}/.Caddyfile-${candidate_sha}.XXXXXX")"
install -m 644 "$candidate_file" "$candidate_install"
mv -f "$candidate_install" "$current_caddy"
candidate_install=""

previous_sha="$(awk '{print $1}' "${backup}.sha256")"

restore_previous() {
  local restore_file
  restore_file="$(mktemp "${current_dir}/.Caddyfile-rollback.XXXXXX")" || return 1
  install -m 644 "$backup" "$restore_file" || { rm -f "$restore_file"; return 1; }
  mv -f "$restore_file" "$current_caddy" || { rm -f "$restore_file"; return 1; }
  caddy validate --config "$current_caddy" --adapter caddyfile || return 1
  systemctl reload caddy || return 1
  systemctl is-active --quiet caddy || return 1
  echo 'GPOINT_CADDY_ROLLBACK=PASS'
}

rollback_after_failure() {
  local reason="$1"
  if restore_previous; then
    echo "Candidate Caddy apply failed ($reason); previous config restored and reloaded." >&2
  else
    echo "GPOINT_CADDY_ROLLBACK=FAILED; previous config backup is $backup; manual recovery is required." >&2
  fi
  exit 1
}

if ! caddy validate --config "$current_caddy" --adapter caddyfile; then
  rollback_after_failure candidate_validation
fi
if ! systemctl reload caddy; then
  rollback_after_failure candidate_reload
fi
if ! systemctl is-active --quiet caddy; then
  rollback_after_failure caddy_inactive_after_reload
fi

printf 'GPOINT_CADDY_APPLY=PASS SHA=%s CADDY_SHA256=%s PREVIOUS_SHA256=%s BACKUP=%s\n' \
  "$candidate_sha" "$expected_caddy_sha" "$previous_sha" "$backup"
rm -f "$candidate_file"
