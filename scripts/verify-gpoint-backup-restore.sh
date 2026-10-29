#!/usr/bin/env bash
set -euo pipefail

candidate="${1:?candidate SHA required}"
run_id="${2:?GitHub run id required}"
attempt="${3:?GitHub run attempt required}"
baseline_file="${4:?baseline file required}"
baseline_sha="${5:?baseline SHA-256 required}"
backup_helper="${6:?GPoint backup helper required}"
age_recipient="${7:?protected age recipient required}"
offsite_remote="${8:?protected rclone destination required}"
media_tree_helper="${9:?media tree manifest helper required}"

[[ "$candidate" =~ ^[0-9a-f]{40}$ ]]
[[ "$run_id" =~ ^[0-9]+$ && "$attempt" =~ ^[0-9]+$ ]]
[[ "$baseline_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$baseline_file" == "/opt/gpoint/release-logs/${candidate}/kamal-cutover-baseline-${run_id}-${attempt}.txt" ]]
[[ "$backup_helper" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/backup.py" ]]
[[ "$media_tree_helper" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/media-tree.py" ]]
[[ "${BASH_SOURCE[0]}" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/restore.sh" ]]
test -s "$baseline_file" && test -s "$backup_helper" && test -s "$media_tree_helper"
test "$(sha256sum "$baseline_file" | awk '{print $1}')" = "$baseline_sha"
[[ "$age_recipient" =~ ^age1[0-9a-z]{58}$ ]]
[[ "$offsite_remote" =~ ^[A-Za-z0-9_-]+:[A-Za-z0-9_./-]+$ && "$offsite_remote" != *..* ]]
age_identity=/opt/gpoint/env/gpoint-backup.agekey
rclone_config=/root/.config/rclone/rclone.conf
test -f "$age_identity" && test ! -L "$age_identity" && test "$(stat -c '%a:%u' "$age_identity")" = 600:0
test -f "$rclone_config" && test ! -L "$rclone_config" && test "$(stat -c '%a:%u' "$rclone_config")" = 600:0
command -v age >/dev/null && command -v age-keygen >/dev/null && command -v rclone >/dev/null
test "$(age-keygen -y "$age_identity")" = "$age_recipient"
test "$(stat -f -c %T /run)" = tmpfs
plaintext_dir="/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/plaintext"
install -d -m 700 "$plaintext_dir"
roles_file=""
restore_dir=""
media_restore_dir=""
media_backup_work=""
restore_name="gpoint-restore-${candidate:0:12}-${run_id}-${attempt}"
restore_container_id=""
cleanup() {
  if [[ -n "$restore_container_id" ]]; then docker rm -f "$restore_container_id" >/dev/null 2>&1 || true; fi
  if [[ -n "$restore_dir" ]]; then rm -rf -- "$restore_dir"; fi
  if [[ -n "$media_restore_dir" ]]; then rm -rf -- "$media_restore_dir"; fi
  if [[ -n "$media_backup_work" ]]; then rm -rf -- "$media_backup_work"; fi
  if [[ -n "$roles_file" ]]; then rm -f -- "$roles_file"; fi
  if [[ -n "${plaintext_dir:-}" ]]; then rm -rf -- "$plaintext_dir"; fi
  rm -f -- "$backup_helper" "$media_tree_helper" "${BASH_SOURCE[0]}"
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

# Bind the backup to the fresh inventory's exact API media mount before reading
# its contents. The candidate Kamal API reuses this persistent host bind mount.
inventory_file="$(baseline_value INVENTORY_FILE)"
inventory_sha="$(baseline_value INVENTORY_SHA256)"
[[ "$inventory_file" == "/opt/gpoint/release-logs/${candidate}/kamal-vps-inventory-${run_id}-${attempt}.json" ]]
[[ "$inventory_sha" =~ ^[0-9a-f]{64}$ ]]
test -s "$inventory_file"
test "$(sha256sum "$inventory_file" | awk '{print $1}')" = "$inventory_sha"
api_container_id="$(docker inspect --format '{{.Id}}' compose-api-1)"
python3 - "$inventory_file" "$api_container_id" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as source:
    inventory=json.load(source)
rows=[row for row in inventory.get('containers', [])
      if row.get('composeProject') == 'compose' and row.get('composeService') == 'api']
if len(rows) != 1 or rows[0].get('id') != sys.argv[2] or rows[0].get('running') is not True:
    raise SystemExit('fresh inventory does not identify exactly one running Compose API container')
mounts=[mount for mount in rows[0].get('mounts', []) if mount.get('destination') == '/data/media']
if len(mounts) != 1 or mounts[0].get('type') != 'bind' or mounts[0].get('source') != '/opt/gpoint/media' or mounts[0].get('readWrite') is not True:
    raise SystemExit('fresh inventory does not prove the required writable persistent media bind mount')
PY
media_root=/opt/gpoint/media
test -d "$media_root" && test ! -L "$media_root"
media_source_manifest="$plaintext_dir/media-source.json"
media_manifest_output="$(python3 "$media_tree_helper" manifest --root "$media_root" --output "$media_source_manifest")"
grep -qx 'GPOINT_MEDIA_MANIFEST=PASS' <<< "$media_manifest_output"
media_file_count="$(sed -n 's/^GPOINT_MEDIA_FILE_COUNT=//p' <<< "$media_manifest_output")"
media_directory_count="$(sed -n 's/^GPOINT_MEDIA_DIRECTORY_COUNT=//p' <<< "$media_manifest_output")"
media_total_bytes="$(sed -n 's/^GPOINT_MEDIA_TOTAL_BYTES=//p' <<< "$media_manifest_output")"
media_manifest_sha="$(sed -n 's/^GPOINT_MEDIA_MANIFEST_SHA256=//p' <<< "$media_manifest_output")"
[[ "$media_file_count" =~ ^[0-9]+$ && "$media_directory_count" =~ ^[0-9]+$ ]]
[[ "$media_total_bytes" =~ ^[0-9]+$ && "$media_manifest_sha" =~ ^[0-9a-f]{64}$ ]]

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

# Reserve enough space for both encrypted backups and disposable restores before
# writing backup bytes. Account for database WAL/index overhead plus conservative
# per-entry tar metadata overhead for the media tree.
test -d /opt/gpoint/backups
docker_root="$(docker info --format '{{.DockerRootDir}}')"
[[ "$docker_root" == /* && -d "$docker_root" ]]
backup_fs="$(stat -c '%d' /opt/gpoint/backups)"
docker_fs="$(stat -c '%d' "$docker_root")"
backup_free_before="$(df -PB1 /opt/gpoint/backups | awk 'NR==2 {print $4}')"
restore_free_before="$(df -PB1 "$docker_root" | awk 'NR==2 {print $4}')"
pre_backup_capacity_check() {
  python3 - "$database_bytes" "$media_total_bytes" "$media_file_count" "$media_directory_count" \
    "$backup_free_before" "$restore_free_before" "$([[ "$backup_fs" == "$docker_fs" ]] && echo same || echo separate)" <<'PY'
import sys
db_size, media_size, media_files, media_dirs, backup_free, restore_free = map(int, sys.argv[1:7])
metadata = (media_files + media_dirs) * 65536
margin = 1024**3
if sys.argv[7] == 'same':
    required = db_size * 3 + media_size * 2 + metadata + margin * 2
    if backup_free < required:
        raise SystemExit('insufficient free space for encrypted database/media backups, isolated restores and safety margin')
else:
    if backup_free < db_size + media_size + metadata + margin or restore_free < db_size * 2 + media_size + margin:
        raise SystemExit('insufficient free space for database/media backup or isolated restores and safety margin')
PY
}
pre_backup_capacity_check
echo GPOINT_PRE_BACKUP_CAPACITY=PASS

# Reuse GPoint's established complete logical-backup and archive-integrity gate.
backup_output="$(python3 "$backup_helper" backup --sha "$candidate" --backup-directory "$plaintext_dir")"
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
if not proof['path'].startswith('/run/gpoint-kamal/' + sys.argv[2] + '-') or not proof['path'].endswith('.dump'):
    raise SystemExit('backup path is outside the protected temporary namespace')
if not re.fullmatch(r'[0-9a-f]{64}',proof['sha256']):
    raise SystemExit('backup checksum is invalid')
if not {'public','agata'}.issubset(set(proof['schemas'])):
    raise SystemExit('backup does not include required application schemas')
print(proof['path'])
print(proof['sha256'])
PY
)
backup_file="${backup_facts[0]}"
plaintext_sha="${backup_facts[1]}"
[[ "$backup_file" == "$plaintext_dir/"* ]]
test -f "$backup_file" && test ! -L "$backup_file" && test -s "$backup_file"
chmod 600 "$backup_file"
test "$(sha256sum "$backup_file" | awk '{print $1}')" = "$plaintext_sha"
encrypted_name="gpoint-pre-release-${candidate}-${run_id}-${attempt}.dump.age"
backup_file="/opt/gpoint/backups/$encrypted_name"
backup_work="$(mktemp -d /opt/gpoint/backups/.gpoint-age-${run_id}-${attempt}.XXXXXX)"
chmod 700 "$backup_work"
age --recipient "$age_recipient" -o "$backup_work/$encrypted_name" "$plaintext_dir/$(basename "${backup_facts[0]}")"
chmod 600 "$backup_work/$encrypted_name"
ln "$backup_work/$encrypted_name" "$backup_file"
rm -rf "$backup_work"
backup_sha="$(sha256sum "$backup_file" | awk '{print $1}')"
backup_bytes="$(stat -c '%s' "$backup_file")"
[[ "$backup_bytes" =~ ^[0-9]+$ && "$backup_bytes" -gt 0 ]]

offsite_object="${offsite_remote%/}/${candidate}/${run_id}-${attempt}/$encrypted_name"
rclone copyto --immutable --config "$rclone_config" "$backup_file" "$offsite_object"
rclone check --download --config "$rclone_config" "$backup_file" "${offsite_remote%/}/${candidate}/${run_id}-${attempt}"
offsite_copy="$plaintext_dir/offsite.dump.age"
rclone copyto --config "$rclone_config" "$offsite_object" "$offsite_copy"
test "$(sha256sum "$offsite_copy" | awk '{print $1}')" = "$backup_sha"

# Stream the media tree directly through age so no plaintext media archive is
# written to persistent storage. Keep its offsite object separate from the DB
# archive so rclone's full readback comparison is unambiguous.
media_encrypted_name="gpoint-pre-release-${candidate}-${run_id}-${attempt}.media.tar.age"
media_backup_file="/opt/gpoint/backups/$media_encrypted_name"
test ! -e "$media_backup_file" && test ! -L "$media_backup_file"
media_backup_work="$(mktemp -d /opt/gpoint/backups/.gpoint-media-age-${run_id}-${attempt}.XXXXXX)"
chmod 700 "$media_backup_work"
tar --create --file=- --directory="$media_root" --format=pax --numeric-owner \
  --acls --xattrs --hard-dereference -- . | \
  age --recipient "$age_recipient" -o "$media_backup_work/$media_encrypted_name"
chmod 600 "$media_backup_work/$media_encrypted_name"
ln "$media_backup_work/$media_encrypted_name" "$media_backup_file"
rm -rf "$media_backup_work"
media_backup_sha="$(sha256sum "$media_backup_file" | awk '{print $1}')"
media_backup_bytes="$(stat -c '%s' "$media_backup_file")"
[[ "$media_backup_sha" =~ ^[0-9a-f]{64}$ && "$media_backup_bytes" =~ ^[0-9]+$ && "$media_backup_bytes" -gt 0 ]]
media_offsite_dir="${offsite_remote%/}/${candidate}/${run_id}-${attempt}/media"
media_offsite_object="$media_offsite_dir/$media_encrypted_name"
rclone copyto --immutable --config "$rclone_config" "$media_backup_file" "$media_offsite_object"
rclone check --download --config "$rclone_config" "$media_backup_file" "$media_offsite_dir"
media_offsite_sha="$(rclone cat --config "$rclone_config" "$media_offsite_object" | sha256sum | awk '{print $1}')"
test "$media_offsite_sha" = "$media_backup_sha"
echo GPOINT_MEDIA_OFFSITE_READBACK=PASS

backup_fs="$(stat -c '%d' "$backup_file")"
docker_fs="$(stat -c '%d' "$docker_root")"
backup_free="$(df -PB1 "$(dirname "$backup_file")" | awk 'NR==2 {print $4}')"
restore_free="$(df -PB1 "$docker_root" | awk 'NR==2 {print $4}')"
[[ "$backup_free" =~ ^[0-9]+$ && "$restore_free" =~ ^[0-9]+$ ]]
pre_restore_capacity_check() {
  python3 - "$database_bytes" "$media_total_bytes" "$media_file_count" "$media_directory_count" "$restore_free" <<'PY'
import sys
db_size, media_size, media_files, media_dirs, free = map(int, sys.argv[1:6])
metadata = (media_files + media_dirs) * 65536
required = db_size * 2 + media_size + metadata + 1024**3
if free < required:
    raise SystemExit('insufficient Docker storage for database/media isolated restores and safety margin')
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
age -d -i "$age_identity" "$offsite_copy" | \
  docker exec -i "$restore_name" pg_restore --exit-on-error --no-owner -U gpoint_restore_admin -d gpoint >/dev/null
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

media_restore_dir="$(mktemp -d "${docker_root%/}/.gpoint-kamal-media-restore-${run_id}-${attempt}.XXXXXX")"
chmod 700 "$media_restore_dir"
rclone cat --config "$rclone_config" "$media_offsite_object" | age -d -i "$age_identity" | \
  tar --extract --file=- --directory="$media_restore_dir" --numeric-owner \
    --same-owner --same-permissions --acls --xattrs --delay-directory-restore
media_restore_manifest="$plaintext_dir/media-restored.json"
media_restore_output="$(python3 "$media_tree_helper" manifest --root "$media_restore_dir" --output "$media_restore_manifest")"
grep -qx 'GPOINT_MEDIA_MANIFEST=PASS' <<< "$media_restore_output"
media_restore_manifest_sha="$(sed -n 's/^GPOINT_MEDIA_MANIFEST_SHA256=//p' <<< "$media_restore_output")"
[[ "$media_restore_manifest_sha" =~ ^[0-9a-f]{64}$ ]]
test "$media_restore_manifest_sha" = "$media_manifest_sha"
cmp -s "$media_source_manifest" "$media_restore_manifest"
python3 "$media_tree_helper" compare --manifest "$media_source_manifest" --root "$media_root" | \
  grep -qx 'GPOINT_MEDIA_TREE_MATCH=PASS'
echo GPOINT_MEDIA_BACKUP=PASS
echo GPOINT_MEDIA_ISOLATED_RESTORE=PASS

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
BACKUP_ENCRYPTION=AGE_X25519
OFFSITE_COPY_SHA256=$backup_sha
OFFSITE_RESTORE=PASS
MEDIA_BACKUP_PATH=$media_backup_file
MEDIA_BACKUP_SHA256=$media_backup_sha
MEDIA_BACKUP_BYTES=$media_backup_bytes
MEDIA_FILE_COUNT=$media_file_count
MEDIA_DIRECTORY_COUNT=$media_directory_count
MEDIA_TOTAL_BYTES=$media_total_bytes
MEDIA_SOURCE_MANIFEST_SHA256=$media_manifest_sha
MEDIA_RESTORE_MANIFEST_SHA256=$media_restore_manifest_sha
MEDIA_BACKUP_ENCRYPTION=AGE_X25519
MEDIA_OFFSITE_COPY_SHA256=$media_backup_sha
MEDIA_OFFSITE_RESTORE=PASS
MEDIA_ISOLATED_RESTORE=PASS
EOF
chmod 600 "$evidence"
evidence_sha="$(sha256sum "$evidence" | awk '{print $1}')"
printf 'backup_path=%s\nbackup_sha256=%s\nbackup_bytes=%s\nrestore_evidence_path=%s\nrestore_evidence_sha=%s\nrestored_table_count=%s\nmedia_backup_path=%s\nmedia_backup_sha256=%s\nmedia_backup_bytes=%s\nmedia_file_count=%s\nmedia_directory_count=%s\nmedia_total_bytes=%s\nmedia_source_manifest_sha256=%s\nmedia_restore_manifest_sha256=%s\n' \
  "$backup_file" "$backup_sha" "$backup_bytes" "$evidence" "$evidence_sha" "$restored_tables" \
  "$media_backup_file" "$media_backup_sha" "$media_backup_bytes" "$media_file_count" \
  "$media_directory_count" "$media_total_bytes" "$media_manifest_sha" "$media_restore_manifest_sha"
echo GPOINT_ISOLATED_RESTORE=PASS
