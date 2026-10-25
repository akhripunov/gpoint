#!/usr/bin/env bash
set -euo pipefail

candidate="${1:?candidate SHA required}"
run_id="${2:?GitHub run id required}"
attempt="${3:?GitHub run attempt required}"
baseline_file="${4:?baseline file required}"
baseline_sha="${5:?baseline SHA-256 required}"
backup_helper="${6:?GPoint backup helper required}"

[[ "$candidate" =~ ^[0-9a-f]{40}$ ]]
[[ "$run_id" =~ ^[0-9]+$ && "$attempt" =~ ^[0-9]+$ ]]
[[ "$baseline_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$baseline_file" == "/opt/gpoint/release-logs/${candidate}/kamal-cutover-baseline-${run_id}-${attempt}.txt" ]]
[[ "$backup_helper" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/backup.py" ]]
[[ "${BASH_SOURCE[0]}" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/restore.sh" ]]
test -s "$baseline_file" && test -s "$backup_helper"
test "$(sha256sum "$baseline_file" | awk '{print $1}')" = "$baseline_sha"
roles_file=""
restore_dir=""
restore_name="gpoint-restore-${candidate:0:12}-${run_id}-${attempt}"
restore_container_id=""
cleanup() {
  if [[ -n "$restore_container_id" ]]; then docker rm -f "$restore_container_id" >/dev/null 2>&1 || true; fi
  if [[ -n "$restore_dir" ]]; then rm -rf -- "$restore_dir"; fi
  if [[ -n "$roles_file" ]]; then rm -f -- "$roles_file"; fi
  rm -f -- "$backup_helper" "${BASH_SOURCE[0]}"
  rmdir "$(dirname "$backup_helper")" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

baseline_value() {
  local result
  result="$(sed -n "s/^$1=//p" "$baseline_file")"
  test -n "$result"
  printf '%s' "$result"
}

test "$(baseline_value BASELINE_SCHEMA)" = gpoint-kamal-cutover-baseline/v1
test "$(baseline_value CANDIDATE_SHA)" = "$candidate"
test "$(baseline_value RUN_ID)" = "$run_id"
test "$(baseline_value ATTEMPT)" = "$attempt"

postgres=shared_postgres_1
test "$(docker inspect --format '{{.Id}}' "$postgres")" = "$(baseline_value POSTGRES_CONTAINER_ID)"
test "$(docker inspect --format '{{.Image}}' "$postgres")" = "$(baseline_value POSTGRES_IMAGE_ID)"
test "$(docker inspect --format '{{.State.Running}}' "$postgres")" = true
postgres_system_identifier="$(docker exec "$postgres" sh -ec 'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint -c "SELECT system_identifier::text FROM pg_control_system()"')"
test "$postgres_system_identifier" = "$(baseline_value POSTGRES_SYSTEM_IDENTIFIER)"
postgres_user="$(docker exec "$postgres" sh -ec 'printf %s "$POSTGRES_USER"')"
pgdata="$(docker exec "$postgres" sh -ec 'printf %s "${PGDATA:-/var/lib/postgresql/data}"')"
[[ "$postgres_user" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]
[[ "$pgdata" == /* && "$pgdata" != / && "$pgdata" != *..* ]]
database_bytes="$(docker exec "$postgres" sh -ec 'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint -c "SELECT pg_database_size('\''gpoint'\'')"')"
[[ "$database_bytes" =~ ^[0-9]+$ && "$database_bytes" -gt 0 ]]
source_tables="$(docker exec "$postgres" sh -ec 'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint -c "SELECT count(*) FROM pg_tables WHERE schemaname IN ('\''public'\'','\''agata'\'','\''gsales'\'','\''gsales_meta'\'','\''agata_identity'\'')"')"
[[ "$source_tables" =~ ^[0-9]+$ && "$source_tables" -gt 0 ]]

# Reserve enough space for the archive and a disposable physical restore before
# writing any backup bytes. The larger same-filesystem budget includes the dump,
# restored database, WAL/index build overhead, and a fixed 1 GiB safety margin.
test -d /opt/gpoint/backups
docker_root="$(docker info --format '{{.DockerRootDir}}')"
[[ "$docker_root" == /* && -d "$docker_root" ]]
backup_fs="$(stat -c '%d' /opt/gpoint/backups)"
docker_fs="$(stat -c '%d' "$docker_root")"
backup_free_before="$(df -PB1 /opt/gpoint/backups | awk 'NR==2 {print $4}')"
restore_free_before="$(df -PB1 "$docker_root" | awk 'NR==2 {print $4}')"
pre_backup_capacity_check() {
  python3 - "$database_bytes" "$backup_free_before" "$restore_free_before" "$([[ "$backup_fs" == "$docker_fs" ]] && echo same || echo separate)" <<'PY'
import sys
size, backup_free, restore_free = map(int, sys.argv[1:4])
margin = 1024**3
if sys.argv[4] == 'same':
    required = size * 3 + margin * 2
    if backup_free < required:
        raise SystemExit('insufficient free space for backup plus isolated restore and safety margin')
else:
    if backup_free < size + margin or restore_free < size * 2 + margin:
        raise SystemExit('insufficient free space for backup or isolated restore and safety margin')
PY
}
pre_backup_capacity_check
echo GPOINT_PRE_BACKUP_CAPACITY=PASS

# Reuse GPoint's established complete logical-backup and archive-integrity gate.
backup_output="$(python3 "$backup_helper" backup --sha "$candidate")"
backup_json="$(sed -n 's/^GPOINT_COMPLETE_BACKUP=//p' <<< "$backup_output")"
test -n "$backup_json"
readarray -t backup_facts < <(python3 - "$backup_json" "$candidate" <<'PY'
import json, re, sys
proof=json.loads(sys.argv[1])
required=('path','sha256','database','schemas','status')
if any(key not in proof for key in required):
    raise SystemExit('backup proof is incomplete')
if proof['database']!='gpoint' or proof['status']!='COMPLETE_PROTECTED':
    raise SystemExit('backup proof does not identify a complete GPoint backup')
if proof.get('candidate_sha') != sys.argv[2]:
    raise SystemExit('backup proof candidate SHA mismatch')
if not re.fullmatch(r'/opt/gpoint/backups/gpoint-pre-release-' + sys.argv[2] + r'-[0-9]+\.dump', proof['path']):
    raise SystemExit('backup path is outside the exact candidate backup namespace')
if not re.fullmatch(r'[0-9a-f]{64}',proof['sha256']):
    raise SystemExit('backup checksum is invalid')
if not {'public','agata'}.issubset(set(proof['schemas'])):
    raise SystemExit('backup does not include required application schemas')
print(proof['path'])
print(proof['sha256'])
PY
)
backup_file="${backup_facts[0]}"
backup_sha="${backup_facts[1]}"
test -f "$backup_file" && test ! -L "$backup_file" && test -s "$backup_file"
chmod 600 "$backup_file"
test "$(sha256sum "$backup_file" | awk '{print $1}')" = "$backup_sha"
backup_bytes="$(stat -c '%s' "$backup_file")"
[[ "$backup_bytes" =~ ^[0-9]+$ && "$backup_bytes" -gt 0 ]]

backup_fs="$(stat -c '%d' "$backup_file")"
docker_fs="$(stat -c '%d' "$docker_root")"
backup_free="$(df -PB1 "$(dirname "$backup_file")" | awk 'NR==2 {print $4}')"
restore_free="$(df -PB1 "$docker_root" | awk 'NR==2 {print $4}')"
[[ "$backup_free" =~ ^[0-9]+$ && "$restore_free" =~ ^[0-9]+$ ]]
pre_restore_capacity_check() {
  python3 - "$database_bytes" "$restore_free" <<'PY'
import sys
size, free = map(int, sys.argv[1:3])
required = size * 2 + 1024**3
if free < required:
    raise SystemExit('insufficient Docker storage for isolated restore plus WAL safety margin')
PY
}
pre_restore_capacity_check
echo GPOINT_PRE_RESTORE_CAPACITY=PASS

postgres_image="$(baseline_value POSTGRES_IMAGE_ID)"
docker image inspect "$postgres_image" >/dev/null
roles_file="${backup_file}.restore-roles-${run_id}-${attempt}.sql"
umask 077

# Role names and grants are recreated without production password hashes. The
# archive, role SQL and restored database stay on this host and in a private dir.
docker exec "$postgres" sh -ec 'exec pg_dumpall --roles-only --no-role-passwords -U "$POSTGRES_USER"' > "$roles_file"
test -s "$roles_file"
chmod 600 "$roles_file"
restore_dir="$(mktemp -d "${docker_root%/}/.gpoint-kamal-restore-${run_id}-${attempt}.XXXXXX")"
chmod 700 "$restore_dir"
restore_password="$(openssl rand -hex 32)"
restore_container_id="$(docker run -d --name "$restore_name" --label "gpoint.kamal.restore-run=${run_id}-${attempt}" \
  --network none --env POSTGRES_USER=gpoint_restore_admin \
  --env POSTGRES_PASSWORD="$restore_password" --env POSTGRES_DB=gpoint \
  --volume "$restore_dir:$pgdata" "$postgres_image")"
test -n "$restore_container_id"

ready=0
for _ in $(seq 1 60); do
  if docker exec "$restore_name" pg_isready -q -U gpoint_restore_admin -d gpoint; then ready=1; break; fi
  sleep 2
done
test "$ready" = 1
docker exec -i "$restore_name" psql -Xq -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
  < "$roles_file" >/dev/null
docker exec -i "$restore_name" pg_restore --exit-on-error --no-owner -U gpoint_restore_admin -d gpoint \
  < "$backup_file" >/dev/null
restored_tables="$(docker exec "$restore_name" psql -XqAt -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
  -c "SELECT count(*) FROM pg_tables WHERE schemaname IN ('public','agata','gsales','gsales_meta','agata_identity')")"
test "$restored_tables" = "$source_tables"
restored_bytes="$(docker exec "$restore_name" psql -XqAt -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
  -c "SELECT pg_database_size('gpoint')")"
[[ "$restored_bytes" =~ ^[0-9]+$ && "$restored_bytes" -gt 0 ]]
restored_prisma=NONE
if [[ "$(baseline_value PRISMA_HEAD)" != NONE ]]; then
  restored_prisma="$(docker exec "$restore_name" psql -XqAt -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
    -c "SELECT migration_name FROM public._prisma_migrations WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL ORDER BY finished_at DESC LIMIT 1")"
fi
restored_agata=ABSENT
if [[ "$(baseline_value AGATA_HEAD)" != ABSENT ]]; then
  restored_agata="$(docker exec "$restore_name" psql -XqAt -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
    -c "SELECT version_num FROM agata.alembic_version")"
fi
test "$restored_prisma" = "$(baseline_value PRISMA_HEAD)"
test "$restored_agata" = "$(baseline_value AGATA_HEAD)"

evidence="/opt/gpoint/release-logs/${candidate}/kamal-backup-restore-${run_id}-${attempt}.txt"
cat > "$evidence" <<EOF
BACKUP_RESTORE_SCHEMA=gpoint-kamal-backup-restore/v1
CANDIDATE_SHA=$candidate
RUN_ID=$run_id
ATTEMPT=$attempt
BACKUP_PATH=$backup_file
BACKUP_SHA256=$backup_sha
BACKUP_BYTES=$backup_bytes
RESTORED_TABLE_COUNT=$restored_tables
RESTORED_DATABASE_BYTES=$restored_bytes
RESTORED_PRISMA_HEAD=$restored_prisma
RESTORED_AGATA_HEAD=$restored_agata
ISOLATED_RESTORE=PASS
EOF
chmod 600 "$evidence"
evidence_sha="$(sha256sum "$evidence" | awk '{print $1}')"
printf 'backup_path=%s\nbackup_sha256=%s\nbackup_bytes=%s\nrestore_evidence_path=%s\nrestore_evidence_sha=%s\nrestored_table_count=%s\n' \
  "$backup_file" "$backup_sha" "$backup_bytes" "$evidence" "$evidence_sha" "$restored_tables"
echo GPOINT_ISOLATED_RESTORE=PASS
