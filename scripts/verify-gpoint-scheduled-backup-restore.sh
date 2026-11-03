#!/usr/bin/env bash
set -euo pipefail

candidate="${1:?candidate SHA required}"
run_id="${2:?GitHub run id required}"
attempt="${3:?GitHub run attempt required}"
baseline_file="${4:?baseline file required}"
baseline_sha="${5:?baseline SHA-256 required}"
backup_helper="${6:?GPoint backup helper required}"
age_recipient="${7:?protected age recipient required}"
backup_policy="${9:?GPoint backup policy helper required}"
media_tree_helper="${8:?media tree manifest helper required}"

[[ "$candidate" =~ ^[0-9a-f]{40}$ ]]
[[ "$run_id" =~ ^[0-9]+$ && "$attempt" =~ ^[0-9]+$ ]]
[[ "$baseline_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$baseline_file" == "/opt/gpoint/backups/scheduled/runs/${candidate}-${run_id}-${attempt}/baseline.txt" ]]
[[ "$backup_helper" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/backup.py" ]]
[[ "$media_tree_helper" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/media-tree.py" ]]
[[ "$backup_policy" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/backup-policy.py" ]]
[[ "${BASH_SOURCE[0]}" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/restore.sh" ]]
test -s "$baseline_file" && test -s "$backup_helper" && test -s "$media_tree_helper" && test -s "$backup_policy"
test "$(sha256sum "$baseline_file" | awk '{print $1}')" = "$baseline_sha"
[[ "$age_recipient" =~ ^age1[0-9a-z]{58}$ ]]
age_identity=/opt/gpoint/env/gpoint-backup.agekey
test -f "$age_identity" && test ! -L "$age_identity" && test "$(stat -c '%a:%u' "$age_identity")" = 600:0
command -v age >/dev/null && command -v age-keygen >/dev/null
test "$(age-keygen -y "$age_identity")" = "$age_recipient"
test "$(stat -f -c %T /run)" = tmpfs
plaintext_dir="/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/plaintext"
install -d -m 700 "$plaintext_dir"
rclone_config=/root/.config/rclone/rclone.conf
bunny_remote=""
bunny_snapshot_prefix=""
bunny_database_object=""
bunny_database_readback="NOT_REQUESTED"
bunny_media_object=""
bunny_media_readback="NOT_REQUESTED"
bunny_media_manifest_object=""
bunny_media_manifest_encrypted=""
bunny_media_manifest_readback="NOT_REQUESTED"
bunny_manifest_object=""
bunny_manifest_sha=""
bunny_receipt_object=""
bunny_receipt_sha=""
roles_file=""
backup_work=""
restore_dir=""
media_restore_dir=""
media_backup_work=""
restore_name="gpoint-restore-${candidate:0:12}-${run_id}-${attempt}"
restore_container_id=""
cleanup() {
  if [[ -n "$restore_container_id" ]]; then docker rm -f "$restore_container_id" >/dev/null 2>&1 || true; fi
  if [[ -n "$restore_dir" ]]; then rm -rf -- "$restore_dir"; fi
  if [[ -n "$media_restore_dir" ]]; then rm -rf -- "$media_restore_dir"; fi
  if [[ -n "$backup_work" ]]; then rm -rf -- "$backup_work"; fi
  if [[ -n "$media_backup_work" ]]; then rm -rf -- "$media_backup_work"; fi
  if [[ -n "$roles_file" ]]; then rm -f -- "$roles_file"; fi
  if [[ -n "${plaintext_dir:-}" ]]; then rm -rf -- "$plaintext_dir"; fi
  rm -f -- "$backup_helper" "$media_tree_helper" "$backup_policy" "${BASH_SOURCE[0]}"
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

if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" == "1" ]]; then
  test "$(baseline_value BASELINE_SCHEMA)" = gpoint-migration-rehearsal-baseline/v1
  test "$(baseline_value CANDIDATE_SHA)" = "$candidate"
  test "$(baseline_value RUN_ID)" = "$run_id"
  test "$(baseline_value ATTEMPT)" = "$attempt"
  api_container_id=""
  if [[ -n "${GPOINT_MIGRATION_BACKUP_EVIDENCE:-}" ]]; then
    evidence_sha="${GPOINT_MIGRATION_BACKUP_EVIDENCE_SHA256:-}"
    [[ "$evidence_sha" =~ ^[0-9a-f]{64}$ ]]
    release_backup_output="$(python3 "$backup_policy" release-evidence \
      --evidence "$GPOINT_MIGRATION_BACKUP_EVIDENCE" --sha256 "$evidence_sha")"
    grep -qx 'GPOINT_RELEASE_BACKUP_EVIDENCE=PASS' <<< "$release_backup_output"
    release_value() { sed -n "s/^GPOINT_RELEASE_BACKUP_$1=//p" <<< "$release_backup_output"; }
    media_file_count="$(release_value MEDIA_FILE_COUNT)"
    media_directory_count="$(release_value MEDIA_DIRECTORY_COUNT)"
    media_total_bytes="$(release_value MEDIA_TOTAL_BYTES)"
    media_manifest_sha="$(release_value MEDIA_MANIFEST_SHA256)"
    database_bytes="$(release_value RESTORED_DATABASE_BYTES)"
    test "$(release_value PRISMA_HEAD)" = "$(baseline_value PRISMA_HEAD)"
    test "$(release_value AGATA_HEAD)" = "$(baseline_value AGATA_HEAD)"
    [[ "$(release_value DATABASE_PATH)" == /opt/gpoint/backups/gpoint-pre-release-*.dump.age ]]
    [[ "$(release_value MEDIA_PATH)" == /opt/gpoint/backups/gpoint-pre-release-*.media.tar.age ]]
  else
    latest_manifest=/opt/gpoint/backups/scheduled/latest.json
    test -f "$latest_manifest" && test ! -L "$latest_manifest"
    readarray -t media_manifest_facts < <(python3 - "$latest_manifest" <<'PY'
import json, re, sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
p=m.get('media', {})
if p.get('encrypted') is not True or p.get('integrity') != 'PASS' or p.get('isolated_restore') != 'PASS':
    raise SystemExit('existing media backup proof is incomplete')
for key in ('file_count','directory_count','total_bytes','manifest_sha256'):
    value=p.get(key)
    if not isinstance(value,int) or value < 0:
        if key == 'manifest_sha256' and isinstance(value,str) and re.fullmatch(r'[0-9a-f]{64}',value):
            continue
        raise SystemExit('existing media backup metadata is incomplete')
print(p['file_count'])
print(p['directory_count'])
print(p['total_bytes'])
print(p['manifest_sha256'])
PY
    )
    test "${#media_manifest_facts[@]}" -eq 4
    media_file_count="${media_manifest_facts[0]}"
    media_directory_count="${media_manifest_facts[1]}"
    media_total_bytes="${media_manifest_facts[2]}"
    media_manifest_sha="${media_manifest_facts[3]}"
    database_bytes="$(python3 - "$latest_manifest" <<'PY'
import json, sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
value=m.get('database',{}).get('restored_database_bytes')
if not isinstance(value,int) or value <= 0:
    raise SystemExit('existing database backup size is invalid')
print(value)
PY
    )"
  fi
  media_restore_manifest_sha="$media_manifest_sha"
  source_tables=0
  pgdata=/var/lib/postgresql/data
  postgres_image="$(baseline_value POSTGRES_IMAGE_ID)"
  [[ "$postgres_image" == "pgvector/pgvector:0.8.6-pg16-bookworm@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b" ]]
else
  # Bind a normal scheduled backup to the fresh inventory's exact API media mount.
  inventory_file="$(baseline_value INVENTORY_FILE)"
  inventory_sha="$(baseline_value INVENTORY_SHA256)"
  [[ "$inventory_file" == "/opt/gpoint/backups/scheduled/runs/${candidate}-${run_id}-${attempt}/inventory.json" ]]
  [[ "$inventory_sha" =~ ^[0-9a-f]{64}$ ]]
  test -s "$inventory_file"
  test "$(sha256sum "$inventory_file" | awk '{print $1}')" = "$inventory_sha"
  api_container_id="$(baseline_value API_CONTAINER_ID)"
  python3 - "$inventory_file" "$api_container_id" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as source:
    inventory=json.load(source)
rows=[row for row in inventory.get('containers', [])
      if row.get('name') == 'gpoint-core-api' and row.get('role') == 'core-api']
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
fi

# Reserve enough space for both encrypted backups and disposable restores before
# writing backup bytes. Account for database WAL/index overhead plus conservative
# per-entry tar metadata overhead for the media tree.
postgres_image="$(baseline_value POSTGRES_IMAGE_ID)"
test -d /opt/gpoint/backups
docker_root="$(docker info --format '{{.DockerRootDir}}')"
[[ "$docker_root" == /* && -d "$docker_root" ]]
restore_image_id="$(docker image inspect "$postgres_image" --format '{{.Id}}')"
[[ "$restore_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]
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
if [[ "${GPOINT_USE_EXISTING_BACKUP:-0}" != "1" ]]; then
  pre_backup_capacity_check
  echo GPOINT_PRE_BACKUP_CAPACITY=PASS
fi

# Rehearsals consume the existing scheduled PASS backup. They never capture a
# new dump from production or replace latest.json.
if [[ "${GPOINT_USE_EXISTING_BACKUP:-0}" == "1" ]]; then
  if [[ -n "${GPOINT_MIGRATION_BACKUP_EVIDENCE:-}" ]]; then
    test "${GPOINT_MIGRATION_REHEARSAL:-0}" = 1
    evidence_sha="${GPOINT_MIGRATION_BACKUP_EVIDENCE_SHA256:-}"
    [[ "$evidence_sha" =~ ^[0-9a-f]{64}$ ]]
    release_backup_output="$(python3 "$backup_policy" release-evidence \
      --evidence "$GPOINT_MIGRATION_BACKUP_EVIDENCE" --sha256 "$evidence_sha")"
    grep -qx 'GPOINT_RELEASE_BACKUP_EVIDENCE=PASS' <<< "$release_backup_output"
    release_value() { sed -n "s/^GPOINT_RELEASE_BACKUP_$1=//p" <<< "$release_backup_output"; }
    existing_facts=("$(release_value DATABASE_PATH)" "$(release_value DATABASE_SHA256)" \
      "$(release_value DATABASE_BYTES)" "$(release_value MEDIA_PATH)" \
      "$(release_value MEDIA_SHA256)" "$(release_value MEDIA_BYTES)")
  else
    latest_manifest=/opt/gpoint/backups/scheduled/latest.json
    test -f "$latest_manifest" && test ! -L "$latest_manifest"
    test "$(stat -c '%a:%u' "$latest_manifest")" = 600:0
    readarray -t existing_facts < <(python3 - "$latest_manifest" "$candidate" \
      "$(baseline_value PRISMA_HEAD)" "$(baseline_value AGATA_HEAD)" <<'PY'
import hashlib, json, os, re, stat, sys
from pathlib import Path
manifest_path=Path(sys.argv[1])
try:
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
except (OSError, json.JSONDecodeError):
    raise SystemExit('existing scheduled backup manifest is invalid')
if manifest.get('schema')!='gpoint-scheduled-backup/v1' or manifest.get('status')!='PASS':
    raise SystemExit('existing scheduled backup is not PASS')
if manifest.get('database',{}).get('prisma_head')!=sys.argv[3] or manifest.get('database',{}).get('agata_head')!=sys.argv[4]:
    raise SystemExit('existing scheduled backup migration heads do not match the source baseline')
for name, suffix in (('database','.dump.age'),('media','.media.tar.age')):
    proof=manifest.get(name)
    if not isinstance(proof,dict) or proof.get('encrypted') is not True or proof.get('integrity')!='PASS' or proof.get('isolated_restore')!='PASS':
        raise SystemExit(f'existing {name} backup proof is incomplete')
    path=Path(str(proof.get('path','')))
    try:
        info=path.lstat()
    except OSError:
        raise SystemExit(f'existing {name} backup is unavailable') from None
    if (path.parent != Path('/opt/gpoint/backups/scheduled') or not path.name.endswith(suffix)
            or not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600):
        raise SystemExit(f'existing {name} backup identity is invalid')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if digest!=proof.get('sha256') or info.st_size!=proof.get('bytes'):
        raise SystemExit(f'existing {name} backup checksum or size mismatch')
    print(path)
    print(digest)
    print(info.st_size)
PY
    )
  fi
  test "${#existing_facts[@]}" -eq 6
  backup_file="${existing_facts[0]}"
  backup_sha="${existing_facts[1]}"
  backup_bytes="${existing_facts[2]}"
  media_backup_file="${existing_facts[3]}"
  media_backup_sha="${existing_facts[4]}"
  media_backup_bytes="${existing_facts[5]}"
  if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" == "1" && -z "${GPOINT_MIGRATION_BACKUP_EVIDENCE:-}" ]]; then
    latest_manifest=/opt/gpoint/backups/scheduled/latest.json
    readarray -t bunny_facts < <(python3 - "$latest_manifest" "$backup_sha" "$media_backup_sha" <<'PY'
import json, re, sys
from pathlib import Path
manifest=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
b=manifest.get('bunny', {})
db=manifest.get('database', {})
media=manifest.get('media', {})
restore=manifest.get('restore_evidence', {})
remote_db=b.get('database', {})
remote_media=b.get('media', {})
remote_restore=b.get('restore', {})
root=str(b.get('remote_root',''))
candidate=str(manifest.get('candidate_sha',''))
run_id=str(manifest.get('run_id',''))
attempt=str(manifest.get('attempt',''))
if (b.get('schema')!='gpoint-bunny-backup/v1' or b.get('status')!='PASS'
        or b.get('bucket')!='gpoint-backups' or not re.fullmatch(r'[A-Za-z0-9_-]+:gpoint-backups',root)
        or not re.fullmatch(r'[0-9a-f]{40}',candidate) or not run_id.isdigit() or not attempt.isdigit()):
    raise SystemExit('existing Bunny snapshot identity is invalid')
prefix=f'{root}/gpoint/scheduled/{candidate}/{run_id}-{attempt}'
if b.get('snapshot_prefix')!=prefix or b.get('snapshot')!=f'{candidate}/{run_id}-{attempt}':
    raise SystemExit('existing Bunny snapshot prefix mismatch')
expected={
    'database': (remote_db, f'{prefix}/{Path(str(db.get("path",""))).name}', sys.argv[2]),
    'media': (remote_media, f'{prefix}/{Path(str(media.get("path",""))).name}', sys.argv[3]),
}
for name,(item,path,digest) in expected.items():
    if (item.get('object')!=path or item.get('sha256')!=digest or item.get('readback')!='PASS'
            or item.get('isolated_restore')!='PASS' or item.get('encrypted') is not True):
        raise SystemExit(f'existing Bunny {name} snapshot proof is incomplete')
media_manifest=f'{prefix}/media-source.json.age'
manifest_object=f'{prefix}/manifest.json'
receipt_object=f'{prefix}/restore.txt'
if (remote_media.get('source_manifest_object')!=media_manifest
        or remote_media.get('source_manifest_readback')!='PASS'
        or not re.fullmatch(r'[0-9a-f]{64}',str(remote_media.get('source_manifest_ciphertext_sha256','')))
        or not re.fullmatch(r'[0-9a-f]{64}',str(remote_media.get('source_manifest_sha256','')))
        or remote_media.get('source_manifest_sha256')!=media.get('manifest_sha256')):
    raise SystemExit('existing Bunny media source manifest proof is incomplete')
if (remote_restore.get('source')!='BUNNY_READBACK' or remote_restore.get('status')!='PASS'
        or remote_restore.get('receipt_object')!=receipt_object
        or remote_restore.get('receipt_sha256')!=restore.get('sha256')):
    raise SystemExit('existing Bunny manifest or restore receipt proof is incomplete')
for value in (root,prefix,remote_db['object'],remote_db['sha256'],remote_media['object'],remote_media['sha256'],
              media_manifest,remote_media['source_manifest_ciphertext_sha256'],remote_media['source_manifest_sha256'],
              manifest_object,b['manifest_sha256'],receipt_object,remote_restore['receipt_sha256']):
    print(value)
PY
    )
    test "${#bunny_facts[@]}" -eq 13
    bunny_remote="${bunny_facts[0]}"
    bunny_snapshot_prefix="${bunny_facts[1]}"
    bunny_database_object="${bunny_facts[2]}"
    bunny_database_sha="${bunny_facts[3]}"
    bunny_media_object="${bunny_facts[4]}"
    bunny_media_sha="${bunny_facts[5]}"
    bunny_media_manifest_object="${bunny_facts[6]}"
    bunny_media_manifest_ciphertext_sha="${bunny_facts[7]}"
    bunny_media_manifest_plaintext_sha="${bunny_facts[8]}"
    bunny_manifest_object="${bunny_facts[9]}"
    bunny_manifest_sha="${bunny_facts[10]}"
    bunny_receipt_object="${bunny_facts[11]}"
    bunny_receipt_sha="${bunny_facts[12]}"
  fi
  echo GPOINT_EXISTING_BACKUP=PASS
else
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
  encrypted_name="gpoint-scheduled-${candidate}-${run_id}-${attempt}.dump.age"
  backup_file="/opt/gpoint/backups/scheduled/$encrypted_name"
  backup_work="$(mktemp -d /opt/gpoint/backups/.gpoint-age-${run_id}-${attempt}.XXXXXX)"
  chmod 700 "$backup_work"
  age --recipient "$age_recipient" -o "$backup_work/$encrypted_name" "$plaintext_dir/$(basename "${backup_facts[0]}")"
  chmod 600 "$backup_work/$encrypted_name"
  ln "$backup_work/$encrypted_name" "$backup_file"
  rm -rf "$backup_work"
  backup_sha="$(sha256sum "$backup_file" | awk '{print $1}')"
  backup_bytes="$(stat -c '%s' "$backup_file")"
  [[ "$backup_bytes" =~ ^[0-9]+$ && "$backup_bytes" -gt 0 ]]
  test "$(sha256sum "$backup_file" | awk '{print $1}')" = "$backup_sha"
  echo GPOINT_BACKUP_INTEGRITY=PASS
fi

# Stream the media tree directly through age so no plaintext media archive is
# written to persistent storage.
if [[ "${GPOINT_USE_EXISTING_BACKUP:-0}" != "1" ]]; then
  media_encrypted_name="gpoint-scheduled-${candidate}-${run_id}-${attempt}.media.tar.age"
  media_backup_file="/opt/gpoint/backups/scheduled/$media_encrypted_name"
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
  test "$(sha256sum "$media_backup_file" | awk '{print $1}')" = "$media_backup_sha"
  echo GPOINT_MEDIA_BACKUP_INTEGRITY=PASS
else
  echo GPOINT_EXISTING_MEDIA_BACKUP=PASS
fi

# The routine restore consumes exact read-back objects from the existing Bunny
# zone. Local recovery files remain intact until the separate policy transition.
restore_backup_file="$backup_file"
restore_media_backup_file="$media_backup_file"
if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" != "1" && "${GPOINT_USE_EXISTING_BACKUP:-0}" != "1" ]]; then
  bunny_remote="${GPOINT_BACKUP_BUNNY_REMOTE:-}"
  [[ "$bunny_remote" =~ ^[A-Za-z0-9_-]+:gpoint-backups$ ]]
  test -f "$rclone_config" && test ! -L "$rclone_config"
  test "$(stat -c '%a:%u' "$rclone_config")" = 600:0
  command -v rclone >/dev/null
  bunny_snapshot_prefix="${bunny_remote}/gpoint/scheduled/${candidate}/${run_id}-${attempt}"
  bunny_database_object="${bunny_snapshot_prefix}/$(basename "$backup_file")"
  bunny_media_object="${bunny_snapshot_prefix}/$(basename "$media_backup_file")"
  bunny_media_manifest_object="${bunny_snapshot_prefix}/media-source.json.age"
  bunny_readback_dir="${plaintext_dir}/bunny-readback"
  install -d -m 700 "$bunny_readback_dir"
  bunny_media_manifest_encrypted="${plaintext_dir}/media-source.json.age"
  age --recipient "$age_recipient" -o "$bunny_media_manifest_encrypted" "$media_source_manifest"
  chmod 600 "$bunny_media_manifest_encrypted"

  for pair in \
    "$backup_file|$bunny_database_object|$(basename "$backup_file")" \
    "$media_backup_file|$bunny_media_object|$(basename "$media_backup_file")" \
    "$bunny_media_manifest_encrypted|$bunny_media_manifest_object|media-source.json.age"; do
    IFS='|' read -r source object name <<< "$pair"
    rclone copyto --immutable --config "$rclone_config" "$source" "$object"
    rclone copyto --config "$rclone_config" "$object" "${bunny_readback_dir}/${name}"
    test "$(sha256sum "$source" | awk '{print $1}')" = \
      "$(sha256sum "${bunny_readback_dir}/${name}" | awk '{print $1}')"
  done
  bunny_database_readback=PASS
  bunny_media_readback=PASS
  bunny_media_manifest_readback=PASS
  restore_backup_file="${bunny_readback_dir}/$(basename "$backup_file")"
  restore_media_backup_file="${bunny_readback_dir}/$(basename "$media_backup_file")"
  restore_media_source_manifest="${bunny_readback_dir}/media-source.json"
  age -d -i "$age_identity" "${bunny_readback_dir}/media-source.json.age" > "$restore_media_source_manifest"
  chmod 600 "$restore_media_source_manifest"
  test "$(sha256sum "$restore_media_source_manifest" | awk '{print $1}')" = "$media_manifest_sha"
elif [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" == "1" && -n "$bunny_database_object" ]]; then
  test "${GPOINT_BACKUP_BUNNY_REMOTE:-}" = "$bunny_remote"
  test -f "$rclone_config" && test ! -L "$rclone_config"
  test "$(stat -c '%a:%u' "$rclone_config")" = 600:0
  command -v rclone >/dev/null
  bunny_readback_dir="${plaintext_dir}/bunny-readback"
  install -d -m 700 "$bunny_readback_dir"
  rclone copyto --config "$rclone_config" "$bunny_manifest_object" "$bunny_readback_dir/manifest.json"
  rclone copyto --config "$rclone_config" "$bunny_receipt_object" "$bunny_readback_dir/restore.txt"
  rclone copyto --config "$rclone_config" "$bunny_database_object" "$bunny_readback_dir/$(basename "$backup_file")"
  rclone copyto --config "$rclone_config" "$bunny_media_object" "$bunny_readback_dir/$(basename "$media_backup_file")"
  bunny_media_manifest_encrypted="$bunny_readback_dir/media-source.json.age"
  rclone copyto --config "$rclone_config" "$bunny_media_manifest_object" "$bunny_media_manifest_encrypted"
  test "$(sha256sum "$bunny_readback_dir/manifest.json" | awk '{print $1}')" = "$bunny_manifest_sha"
  test "$(sha256sum "$bunny_readback_dir/restore.txt" | awk '{print $1}')" = "$bunny_receipt_sha"
  test "$(sha256sum "$bunny_readback_dir/$(basename "$backup_file")" | awk '{print $1}')" = "$bunny_database_sha"
  test "$(sha256sum "$bunny_readback_dir/$(basename "$media_backup_file")" | awk '{print $1}')" = "$bunny_media_sha"
  test "$(sha256sum "$bunny_media_manifest_encrypted" | awk '{print $1}')" = "$bunny_media_manifest_ciphertext_sha"
  python3 - "$bunny_readback_dir/manifest.json" "$bunny_remote" "$bunny_snapshot_prefix" \
    "$bunny_database_object" "$bunny_database_sha" "$bunny_media_object" "$bunny_media_sha" \
    "$bunny_media_manifest_object" "$bunny_media_manifest_ciphertext_sha" "$bunny_media_manifest_plaintext_sha" \
    "$bunny_receipt_object" "$bunny_receipt_sha" <<'PY'
import json, re, sys
from pathlib import Path
manifest=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
b=manifest
db=b.get('database',{})
media=b.get('media',{})
restore=b.get('restore',{})
if (b.get('schema')!='gpoint-bunny-backup/v1' or b.get('status')!='PASS'
        or b.get('bucket')!='gpoint-backups' or b.get('remote_root')!=sys.argv[2]
        or b.get('snapshot_prefix')!=sys.argv[3]):
    raise SystemExit('read-back Bunny manifest identity mismatch')
if (db.get('object')!=sys.argv[4] or db.get('sha256')!=sys.argv[5]
        or db.get('readback')!='PASS' or db.get('isolated_restore')!='PASS'):
    raise SystemExit('read-back Bunny database manifest mismatch')
if (media.get('object')!=sys.argv[6] or media.get('sha256')!=sys.argv[7]
        or media.get('readback')!='PASS' or media.get('isolated_restore')!='PASS'
        or media.get('source_manifest_object')!=sys.argv[8]
        or media.get('source_manifest_ciphertext_sha256')!=sys.argv[9]
        or media.get('source_manifest_sha256')!=sys.argv[10]
        or media.get('source_manifest_readback')!='PASS'):
    raise SystemExit('read-back Bunny media manifest mismatch')
if (restore.get('source')!='BUNNY_READBACK' or restore.get('status')!='PASS'
        or restore.get('receipt_object')!=sys.argv[11] or restore.get('receipt_sha256')!=sys.argv[12]):
    raise SystemExit('read-back Bunny restore receipt mismatch')
PY
  restore_backup_file="$bunny_readback_dir/$(basename "$backup_file")"
  restore_media_backup_file="$bunny_readback_dir/$(basename "$media_backup_file")"
  restore_media_source_manifest="$bunny_readback_dir/media-source.json"
  age -d -i "$age_identity" "$bunny_media_manifest_encrypted" > "$restore_media_source_manifest"
  chmod 600 "$restore_media_source_manifest"
  test "$(sha256sum "$restore_media_source_manifest" | awk '{print $1}')" = "$bunny_media_manifest_plaintext_sha"
  test "$bunny_media_manifest_plaintext_sha" = "$media_manifest_sha"
  bunny_database_readback=PASS
  bunny_media_readback=PASS
  bunny_media_manifest_readback=PASS
fi

backup_fs="$(stat -c '%d' "$backup_file")"
docker_fs="$(stat -c '%d' "$docker_root")"
backup_free="$(df -PB1 "$(dirname "$backup_file")" | awk 'NR==2 {print $4}')"
restore_free="$(df -PB1 "$docker_root" | awk 'NR==2 {print $4}')"
[[ "$backup_free" =~ ^[0-9]+$ && "$restore_free" =~ ^[0-9]+$ ]]
pre_restore_capacity_check() {
  if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" == "1" ]]; then
    python3 - "$database_bytes" "$restore_free" <<'PY'
import sys
db_size, free = map(int, sys.argv[1:3])
required = db_size * 10 + 2 * 1024**3
if free < required:
    raise SystemExit('insufficient Docker storage for isolated migration restore')
PY
  else
    python3 - "$database_bytes" "$media_total_bytes" "$media_file_count" "$media_directory_count" "$restore_free" <<'PY'
import sys
db_size, media_size, media_files, media_dirs, free = map(int, sys.argv[1:6])
metadata = (media_files + media_dirs) * 65536
required = db_size * 2 + media_size + metadata + 1024**3
if free < required:
    raise SystemExit('insufficient Docker storage for database/media isolated restores and safety margin')
PY
  fi
}
pre_restore_capacity_check
echo GPOINT_PRE_RESTORE_CAPACITY=PASS

docker image inspect "$postgres_image" >/dev/null
roles_file="$plaintext_dir/restore-roles-${run_id}-${attempt}.sql"
umask 077

# initdb already created its pinned postgres role. Replay its ALTER/GRANT
# statements and every other role, while avoiding a duplicate CREATE failure.
restore_role_dump() {
  sed '/^CREATE ROLE postgres;$/d' "$1"
}

# Role names and grants are recreated without production password hashes. The
# archive, role SQL and restored database stay on this host and in a private dir.
if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" != "1" ]]; then
  docker exec "$postgres" sh -ec 'exec pg_dumpall --roles-only --no-role-passwords -U "$POSTGRES_USER"' > "$roles_file"
  test -s "$roles_file"
  chmod 600 "$roles_file"
fi
restore_dir="$(mktemp -d "${docker_root%/}/.gpoint-kamal-restore-${run_id}-${attempt}.XXXXXX")"
chmod 700 "$restore_dir"
restore_password="$(openssl rand -hex 32)"
restore_container_id="$(docker run -d --name "$restore_name" --label "gpoint.kamal.restore-run=${run_id}-${attempt}" \
  --network none --env POSTGRES_USER=postgres \
  --env POSTGRES_PASSWORD="$restore_password" --env POSTGRES_DB=gpoint \
  --volume "$restore_dir:$pgdata" "$postgres_image")"
[[ "$restore_container_id" =~ ^[0-9a-f]{64}$ ]]
[[ "$(docker inspect --format '{{.Id}} {{.Image}} {{.HostConfig.NetworkMode}}' "$restore_container_id")" == "$restore_container_id $restore_image_id none" ]]

ready=0
for _ in $(seq 1 60); do
  if docker exec "$restore_container_id" pg_isready -q -U postgres -d gpoint; then ready=1; break; fi
  sleep 2
done
test "$ready" = 1
# The bootstrap `postgres` role is pinned by initdb and cannot be the source
# of REASSIGN OWNED. Create an ordinary, isolated restore administrator instead.
docker exec "$restore_container_id" psql -Xq -v ON_ERROR_STOP=1 -U postgres -d gpoint \
  -c "CREATE ROLE gpoint_restore_admin LOGIN SUPERUSER PASSWORD '$restore_password'" >/dev/null
if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" == "1" ]]; then
  docker exec "$restore_container_id" psql -Xq -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
    -c "CREATE ROLE gpoint LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD '$restore_password'; CREATE ROLE eros_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; CREATE ROLE eros_migrator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; CREATE ROLE eros_ddl NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; CREATE ROLE gsales_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; CREATE ROLE gsales_agata_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS" >/dev/null
else
  restore_role_dump "$roles_file" |
    docker exec -i "$restore_container_id" psql -Xq -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint >/dev/null
fi

# The isolated PostgreSQL image creates an empty `public` schema during
# initdb, while this archive contains its own CREATE SCHEMA entry. Remove only
# the stock empty schema so pg_restore can recreate the archived schema ACL and
# ownership. Never cascade: any unexpected object or customized schema state
# fails closed before the restore continues.
public_schema_state="$(docker exec -i "$restore_container_id" psql -XqAt -v ON_ERROR_STOP=1 \
  -U gpoint_restore_admin -d gpoint <<'SQL'
WITH public_schema AS (
  SELECT oid, nspowner, nspacl
  FROM pg_namespace
  WHERE nspname = 'public'
), schema_objects AS (
  SELECT
    EXISTS (SELECT 1 FROM pg_class WHERE relnamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_type WHERE typnamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_collation WHERE collnamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_conversion WHERE connamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_operator WHERE oprnamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_opclass WHERE opcnamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_opfamily WHERE opfnamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgnamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_ts_dict WHERE dictnamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_ts_parser WHERE prsnamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_ts_template WHERE tmplnamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_extension WHERE extnamespace = p.oid)
    OR EXISTS (SELECT 1 FROM pg_statistic_ext WHERE stxnamespace = p.oid)
    AS has_objects
  FROM public_schema p
), schema_acl AS (
  SELECT
    count(*) = 3
    AND bool_and(
      (grantee = p.nspowner AND privilege_type IN ('USAGE', 'CREATE'))
      OR (grantee = 0 AND privilege_type = 'USAGE')
    ) AS is_default
  FROM public_schema p
  CROSS JOIN LATERAL aclexplode(COALESCE(p.nspacl, acldefault('n', p.nspowner)))
)
SELECT CASE
  WHEN NOT EXISTS (SELECT 1 FROM public_schema) THEN 'PUBLIC_SCHEMA_MISSING'
  WHEN (SELECT pg_get_userbyid(nspowner) FROM public_schema) <> 'pg_database_owner' THEN 'PUBLIC_SCHEMA_OWNER_CHANGED'
  WHEN COALESCE((SELECT has_objects FROM schema_objects), true) THEN 'PUBLIC_SCHEMA_NOT_EMPTY'
  WHEN COALESCE((SELECT is_default FROM schema_acl), false) IS NOT TRUE THEN 'PUBLIC_SCHEMA_ACL_CHANGED'
  ELSE 'PUBLIC_SCHEMA_PRISTINE'
END;
SQL
)"
test "$public_schema_state" = PUBLIC_SCHEMA_PRISTINE
docker exec "$restore_container_id" psql -Xq -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
  -c 'DROP SCHEMA public RESTRICT' >/dev/null
test "$(docker exec "$restore_container_id" psql -XqAt -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
  -c "SELECT to_regnamespace('public') IS NULL")" = t

age -d -i "$age_identity" "$restore_backup_file" | \
  docker exec -i "$restore_container_id" pg_restore --exit-on-error --no-owner -U gpoint_restore_admin -d gpoint >/dev/null
restored_tables="$(docker exec "$restore_container_id" psql -XqAt -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
  -c "SELECT count(*) FROM pg_tables WHERE schemaname IN ('public','agata','gsales','gsales_meta','agata_identity')")"
if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" == "1" ]]; then
  [[ "$restored_tables" =~ ^[0-9]+$ && "$restored_tables" -gt 0 ]]
  docker exec "$restore_container_id" psql -Xq -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
    -c 'GRANT CONNECT, CREATE ON DATABASE gpoint TO gpoint; REASSIGN OWNED BY gpoint_restore_admin TO gpoint' >/dev/null
else
  test "$restored_tables" = "$source_tables"
fi
restored_bytes="$(docker exec "$restore_container_id" psql -XqAt -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
  -c "SELECT pg_database_size('gpoint')")"
[[ "$restored_bytes" =~ ^[0-9]+$ && "$restored_bytes" -gt 0 ]]
restored_prisma=NONE
if [[ "$(baseline_value PRISMA_HEAD)" != NONE ]]; then
  restored_prisma="$(docker exec "$restore_container_id" psql -XqAt -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
    -c "SELECT migration_name FROM public._prisma_migrations WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL ORDER BY finished_at DESC LIMIT 1")"
fi
restored_agata=ABSENT
if [[ "$(baseline_value AGATA_HEAD)" != ABSENT ]]; then
  restored_agata="$(docker exec "$restore_container_id" psql -XqAt -v ON_ERROR_STOP=1 -U gpoint_restore_admin -d gpoint \
    -c "SELECT version_num FROM agata.alembic_version")"
fi
test "$restored_prisma" = "$(baseline_value PRISMA_HEAD)"
test "$restored_agata" = "$(baseline_value AGATA_HEAD)"

migration_parity=NOT_REQUESTED
migration_data=NOT_REQUESTED
migration_access=NOT_REQUESTED
migration_baseline_checksums_sha=NOT_RUN
migration_table_counts_sha=NOT_RUN
migration_profile_semantics_sha=NOT_RUN
if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" == "1" ]]; then
  [[ "${GPOINT_USE_EXISTING_BACKUP:-0}" == "1" ]]
  migration_candidate="${GPOINT_MIGRATION_CANDIDATE_SHA:-}"
  [[ "$migration_candidate" =~ ^[0-9a-f]{40}$ ]]
  app_image="${GPOINT_APP_IMAGE:-}"
  agata_image="${GPOINT_AGATA_IMAGE:-}"
  [[ "$app_image" =~ ^ghcr\.io/akhripunov/gpoint-runtime@sha256:[0-9a-f]{64}$ ]]
  [[ "$agata_image" =~ ^ghcr\.io/akhripunov/gpoint-agata-runtime@sha256:[0-9a-f]{64}$ ]]
  test "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$app_image")" = "$migration_candidate"
  test "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$agata_image")" = "$migration_candidate"
  for name in GPOINT_PRISMA_EROS_QUEUE_SHA256 GPOINT_PRISMA_EROS_MAPPING_SHA256 \
    GPOINT_PRISMA_MESSAGE_STATE_SHA256 GPOINT_AGATA_0012_SHA256; do
    [[ "${!name:-}" =~ ^[0-9a-f]{64}$ ]]
  done
  [[ "$restored_prisma" == "$(baseline_value PRISMA_HEAD)" ]]
  [[ "$restored_agata" == "$(baseline_value AGATA_HEAD)" ]]
  [[ "$restored_prisma" == "20261015130000_codex_telegram_challenge" ]]
  [[ "$restored_agata" == "agata0011" ]]

  table_rowcounts() {
    docker exec -i "$restore_container_id" psql -XqAt -v ON_ERROR_STOP=1 \
      -U gpoint_restore_admin -d gpoint <<'SQL'
SELECT format('SELECT %L || ''='' || count(*) FROM %I.%I', schemaname || '.' || tablename, schemaname, tablename)
FROM pg_tables
WHERE schemaname IN ('public','agata','engine','gsales','gsales_meta','agata_identity')
  AND NOT (schemaname='public' AND tablename='_prisma_migrations')
  AND NOT (schemaname='public' AND tablename IN ('ErosRelationshipMapping','ErosAssistantMessageState'))
  AND NOT (schemaname='engine' AND tablename='eros_inference_jobs')
  AND NOT (schemaname='agata' AND tablename IN ('alembic_version','regional_policy_profiles'))
ORDER BY schemaname, tablename;
\gexec
SQL
  }
  profile_semantics() {
    docker exec "$restore_container_id" psql -XqAt -v ON_ERROR_STOP=1 \
      -U gpoint_restore_admin -d gpoint -c "SELECT coalesce(jsonb_agg(jsonb_build_array(tenant_id::text,jurisdiction,market,effective_from::text,effective_to::text,rules) ORDER BY tenant_id::text,jurisdiction,market,effective_from::text,effective_to::text,rules::text), '[]'::jsonb)::text FROM agata.regional_policy_profiles WHERE status='active' AND rules->>'ai_disclosure_required'='true'"
  }
  table_rowcounts > "$plaintext_dir/table-counts-before.txt"
  profile_semantics > "$plaintext_dir/agata-profile-semantics-before.json"

  existing_failed="$(docker exec "$restore_container_id" psql -XqAt -v ON_ERROR_STOP=1 \
    -U gpoint_restore_admin -d gpoint -c "SELECT count(*) FROM public._prisma_migrations WHERE rolled_back_at IS NULL AND finished_at IS NULL")"
  test "$existing_failed" = 0
  existing_prisma_rows="$(docker exec "$restore_container_id" psql -XqAt -F '=' -v ON_ERROR_STOP=1 \
    -U gpoint_restore_admin -d gpoint -c "SELECT migration_name,checksum FROM public._prisma_migrations WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL ORDER BY migration_name")"
  test -n "$existing_prisma_rows"
  migration_baseline_checksums_sha="$(printf '%s\n' "$existing_prisma_rows" | sha256sum | awk '{print $1}')"
  : "${GPOINT_PRISMA_SOURCE_CHECKSUM_MANIFEST:?exact-SHA migration checksum manifest is required}"
  : "${GPOINT_PRISMA_CHECKSUM_VERIFIER:?migration checksum verifier is required}"
  test -f "$GPOINT_PRISMA_SOURCE_CHECKSUM_MANIFEST"
  test -f "$GPOINT_PRISMA_CHECKSUM_VERIFIER"
  applied_checksum_file="$plaintext_dir/applied-prisma-checksums.txt"
  printf '%s\n' "$existing_prisma_rows" > "$applied_checksum_file"
  chmod 0600 "$applied_checksum_file"
  python3 "$GPOINT_PRISMA_CHECKSUM_VERIFIER" verify \
    --manifest "$GPOINT_PRISMA_SOURCE_CHECKSUM_MANIFEST" \
    --applied "$applied_checksum_file" --expected-count 18
  echo GPOINT_APPLIED_PRISMA_CHECKSUMS=PASS

  # The app and AGATA images are immutable outputs from this candidate's CI build.
  test "$(docker run --rm --network none --entrypoint sha256sum "$app_image" \
    /app/packages/db/prisma/migrations/20261016120000_eros_worker_queue/migration.sql \
    /app/packages/db/prisma/migrations/20261020153000_eros_external_relationship_mapping/migration.sql \
    /app/packages/db/prisma/migrations/20261022110000_eros_gpoint_message_state/migration.sql | awk 'NR==1 {print $1}')" = "$GPOINT_PRISMA_EROS_QUEUE_SHA256"
  test "$(docker run --rm --network none --entrypoint sha256sum "$app_image" \
    /app/packages/db/prisma/migrations/20261020153000_eros_external_relationship_mapping/migration.sql | awk '{print $1}')" = "$GPOINT_PRISMA_EROS_MAPPING_SHA256"
  test "$(docker run --rm --network none --entrypoint sha256sum "$app_image" \
    /app/packages/db/prisma/migrations/20261022110000_eros_gpoint_message_state/migration.sql | awk '{print $1}')" = "$GPOINT_PRISMA_MESSAGE_STATE_SHA256"
  test "$(docker run --rm --network none --entrypoint sha256sum "$agata_image" \
    /srv/agata/migrations/versions/0012_eros_memory_disclosure.py | awk '{print $1}')" = "$GPOINT_AGATA_0012_SHA256"

  migration_db_url="postgresql://gpoint:${restore_password}@127.0.0.1:5432/gpoint?schema=public"
  docker run --rm --network "container:$restore_container_id" --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 128 --memory 2g --cpus 2 \
    --tmpfs /tmp:rw,noexec,nosuid,size=256m \
    --env "DATABASE_URL=$migration_db_url" --workdir /app "$app_image" \
    pnpm --filter @gpoint/db exec prisma migrate deploy
  docker run --rm --network "container:$restore_container_id" --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 128 --memory 2g --cpus 2 \
    --tmpfs /tmp:rw,noexec,nosuid,size=256m \
    --env "AGATA_MIGRATOR_DATABASE_URL=postgresql+psycopg://gpoint_restore_admin:${restore_password}@127.0.0.1:5432/gpoint" \
    --workdir /srv/agata "$agata_image" python -m alembic upgrade agata0012

  prisma_rows="$(docker exec "$restore_container_id" psql -XqAt -F '=' -v ON_ERROR_STOP=1 \
    -U gpoint_restore_admin -d gpoint -c "SELECT migration_name,checksum FROM public._prisma_migrations WHERE migration_name IN ('20261016120000_eros_worker_queue','20261020153000_eros_external_relationship_mapping','20261022110000_eros_gpoint_message_state') AND finished_at IS NOT NULL AND rolled_back_at IS NULL ORDER BY migration_name")"
  expected_prisma="20261016120000_eros_worker_queue=$GPOINT_PRISMA_EROS_QUEUE_SHA256
20261020153000_eros_external_relationship_mapping=$GPOINT_PRISMA_EROS_MAPPING_SHA256
20261022110000_eros_gpoint_message_state=$GPOINT_PRISMA_MESSAGE_STATE_SHA256"
  test "$prisma_rows" = "$expected_prisma"
  test "$(docker exec "$restore_container_id" psql -XqAt -v ON_ERROR_STOP=1 \
    -U gpoint_restore_admin -d gpoint -c 'SELECT version_num FROM agata.alembic_version')" = agata0012
  migration_parity=PASS

  table_rowcounts > "$plaintext_dir/table-counts-after.txt"
  diff -u "$plaintext_dir/table-counts-before.txt" "$plaintext_dir/table-counts-after.txt"
  profile_semantics > "$plaintext_dir/agata-profile-semantics-after.json"
  cmp -s "$plaintext_dir/agata-profile-semantics-before.json" "$plaintext_dir/agata-profile-semantics-after.json"
  migration_table_counts_sha="$(sha256sum "$plaintext_dir/table-counts-after.txt" | awk '{print $1}')"
  migration_profile_semantics_sha="$(sha256sum "$plaintext_dir/agata-profile-semantics-after.json" | awk '{print $1}')"
  migration_data=PASS

  if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" == "1" ]]; then
    runtime_role=gpoint
  else
    runtime_role="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$api_container_id" | \
      python3 -c 'import sys,urllib.parse; rows=[x.strip().split("=",1)[1] for x in sys.stdin if x.startswith("DATABASE_URL=")]; print(urllib.parse.urlparse(rows[0]).username if len(rows)==1 else "")')"
  fi
  [[ "$runtime_role" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]
  tenant_a="me52-${run_id}-a"
  tenant_b="me52-${run_id}-b"
  access_result="$(docker exec -i "$restore_container_id" psql -XqAt -v ON_ERROR_STOP=1 \
    -U gpoint_restore_admin -d gpoint -v role="$runtime_role" -v tenant_a="$tenant_a" -v tenant_b="$tenant_b" <<'SQL'
SELECT CASE WHEN EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname=:'role' AND (rolsuper OR rolbypassrls)
) THEN 'PRIVILEGED_RUNTIME_ROLE'
WHEN NOT has_schema_privilege(:'role','engine','USAGE')
  OR NOT has_table_privilege(:'role','engine.eros_inference_jobs','SELECT') THEN 'RUNTIME_GRANT_MISSING'
WHEN NOT EXISTS (
  SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='engine' AND c.relname='eros_inference_jobs'
    AND c.relrowsecurity AND c.relforcerowsecurity
) THEN 'RLS_NOT_FORCED'
WHEN NOT EXISTS (
  SELECT 1 FROM pg_policies WHERE schemaname='engine' AND tablename='eros_inference_jobs'
    AND policyname='eros_inference_jobs_tenant_isolation'
    AND qual LIKE '%app.tenant_id%' AND with_check LIKE '%app.tenant_id%'
) THEN 'TENANT_POLICY_MISSING'
ELSE 'GRANTS_AND_RLS_PASS' END;
BEGIN;
INSERT INTO engine.eros_inference_jobs
  (id,request_id,request_hash,tenant_id,user_id,session_id,instance_id,job_type,request,deadline_at)
VALUES
  (md5(:'tenant_a'||'-id')::uuid,md5(:'tenant_a'||'-request')::uuid,repeat('0',64),:'tenant_a',
   md5(:'tenant_a'||'-user')::uuid,md5(:'tenant_a'||'-session')::uuid,md5(:'tenant_a'||'-instance')::uuid,
   'SYSTEM_COMPLETION','{}'::jsonb,now()+interval '1 hour'),
  (md5(:'tenant_b'||'-id')::uuid,md5(:'tenant_b'||'-request')::uuid,repeat('0',64),:'tenant_b',
   md5(:'tenant_b'||'-user')::uuid,md5(:'tenant_b'||'-session')::uuid,md5(:'tenant_b'||'-instance')::uuid,
   'SYSTEM_COMPLETION','{}'::jsonb,now()+interval '1 hour');
SET ROLE :"role";
SET app.tenant_id = :'tenant_a';
SELECT (count(*)=1)::text AS tenant_a_visible FROM engine.eros_inference_jobs \gset
\if :tenant_a_visible
\echo TENANT_A_SCOPE_PASS
\else
\quit 1
\endif
SELECT (count(*)=0)::text AS tenant_b_hidden FROM engine.eros_inference_jobs WHERE tenant_id=:'tenant_b' \gset
\if :tenant_b_hidden
\echo TENANT_B_SCOPE_PASS
\else
\quit 1
\endif
SET app.tenant_id = :'tenant_b';
SELECT (count(*)=1)::text AS tenant_b_visible FROM engine.eros_inference_jobs \gset
\if :tenant_b_visible
\echo TENANT_B_OWN_SCOPE_PASS
\else
\quit 1
\endif
RESET ROLE;
ROLLBACK;
SQL
  )"
  grep -qx 'GRANTS_AND_RLS_PASS' <<< "$access_result"
  grep -qx 'TENANT_A_SCOPE_PASS' <<< "$access_result"
  grep -qx 'TENANT_B_SCOPE_PASS' <<< "$access_result"
  grep -qx 'TENANT_B_OWN_SCOPE_PASS' <<< "$access_result"
  migration_access=PASS
  echo GPOINT_MIGRATION_PARITY=PASS
  echo GPOINT_MIGRATION_DATA_PRESERVATION=PASS
  echo GPOINT_MIGRATION_SYNTHETIC_RLS=PASS
fi

if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" != "1" ]]; then
  media_restore_dir="$(mktemp -d "${docker_root%/}/.gpoint-kamal-media-restore-${run_id}-${attempt}.XXXXXX")"
  chmod 700 "$media_restore_dir"
  age -d -i "$age_identity" "$restore_media_backup_file" | \
    tar --extract --file=- --directory="$media_restore_dir" --numeric-owner \
      --same-owner --same-permissions --acls --xattrs --delay-directory-restore
  media_restore_manifest="$plaintext_dir/media-restored.json"
  media_restore_output="$(python3 "$media_tree_helper" manifest --root "$media_restore_dir" --output "$media_restore_manifest")"
  grep -qx 'GPOINT_MEDIA_MANIFEST=PASS' <<< "$media_restore_output"
  media_restore_manifest_sha="$(sed -n 's/^GPOINT_MEDIA_MANIFEST_SHA256=//p' <<< "$media_restore_output")"
  [[ "$media_restore_manifest_sha" =~ ^[0-9a-f]{64}$ ]]
  test "$media_restore_manifest_sha" = "$media_manifest_sha"
  cmp -s "${restore_media_source_manifest:-$media_source_manifest}" "$media_restore_manifest"
  python3 "$media_tree_helper" compare --manifest "${restore_media_source_manifest:-$media_source_manifest}" --root "$media_root" | \
    grep -qx 'GPOINT_MEDIA_TREE_MATCH=PASS'
fi
echo GPOINT_MEDIA_BACKUP=PASS
echo GPOINT_MEDIA_ISOLATED_RESTORE=PASS

evidence="/opt/gpoint/backups/scheduled/runs/${candidate}-${run_id}-${attempt}/restore.txt"
cat > "$evidence" <<EOF
BACKUP_RESTORE_SCHEMA=gpoint-kamal-backup-restore/v1
CANDIDATE_SHA=$candidate
RUN_ID=$run_id
ATTEMPT=$attempt
BACKUP_PATH=$backup_file
BACKUP_SHA256=$backup_sha
BACKUP_BYTES=$backup_bytes
BACKUP_INTEGRITY=PASS
RESTORED_TABLE_COUNT=$restored_tables
RESTORED_DATABASE_BYTES=$restored_bytes
RESTORED_PRISMA_HEAD=$restored_prisma
RESTORED_AGATA_HEAD=$restored_agata
ISOLATED_RESTORE=PASS
BACKUP_ENCRYPTION=AGE_X25519
MEDIA_BACKUP_PATH=$media_backup_file
MEDIA_BACKUP_SHA256=$media_backup_sha
MEDIA_BACKUP_BYTES=$media_backup_bytes
MEDIA_BACKUP_INTEGRITY=PASS
MEDIA_FILE_COUNT=$media_file_count
MEDIA_DIRECTORY_COUNT=$media_directory_count
MEDIA_TOTAL_BYTES=$media_total_bytes
MEDIA_SOURCE_MANIFEST_SHA256=$media_manifest_sha
MEDIA_RESTORE_MANIFEST_SHA256=$media_restore_manifest_sha
MEDIA_BACKUP_ENCRYPTION=AGE_X25519
MEDIA_ISOLATED_RESTORE=PASS
BUNNY_BACKUP_BUCKET=${bunny_remote#*:}
BUNNY_REMOTE_ROOT=$bunny_remote
BUNNY_SNAPSHOT_PREFIX=$bunny_snapshot_prefix
BUNNY_DATABASE_OBJECT=$bunny_database_object
BUNNY_DATABASE_READBACK=$bunny_database_readback
BUNNY_MEDIA_OBJECT=$bunny_media_object
BUNNY_MEDIA_READBACK=$bunny_media_readback
BUNNY_MEDIA_MANIFEST_OBJECT=$bunny_media_manifest_object
BUNNY_MEDIA_MANIFEST_SHA256=$([[ -n "$bunny_media_manifest_object" ]] && sha256sum "$bunny_media_manifest_encrypted" | awk '{print $1}' || echo NOT_REQUESTED)
BUNNY_MEDIA_MANIFEST_READBACK=$bunny_media_manifest_readback
OFFSITE_RESTORE=$([[ "$bunny_database_readback" == PASS ]] && echo PASS || echo NOT_REQUESTED)
MEDIA_OFFSITE_RESTORE=$([[ "$bunny_media_readback" == PASS && "$bunny_media_manifest_readback" == PASS ]] && echo PASS || echo NOT_REQUESTED)
MIGRATION_PARITY=$migration_parity
MIGRATION_DATA_PRESERVATION=$migration_data
MIGRATION_SYNTHETIC_RLS=$migration_access
MIGRATION_PRODUCTION_GRANTS_RLS=NOT_CHECKED
MIGRATION_CANDIDATE_SHA=${migration_candidate:-NOT_RUN}
MIGRATION_PRISMA_HEAD=$([[ "$migration_parity" == PASS ]] && echo 20261022110000_eros_gpoint_message_state || echo NOT_RUN)
MIGRATION_AGATA_HEAD=$([[ "$migration_parity" == PASS ]] && echo agata0012 || echo NOT_RUN)
MIGRATION_BASELINE_PRISMA_COUNT=${existing_prisma_count:-0}
MIGRATION_BASELINE_PRISMA_CHECKSUMS_SHA256=$migration_baseline_checksums_sha
MIGRATION_NEW_PRISMA_CHECKSUMS_SHA256=$([[ "$migration_parity" == PASS ]] && printf '%s\n' "$expected_prisma" | sha256sum | awk '{print $1}' || echo NOT_RUN)
MIGRATION_PRISMA_EROS_QUEUE_SHA256=${GPOINT_PRISMA_EROS_QUEUE_SHA256:-NOT_RUN}
MIGRATION_PRISMA_EROS_MAPPING_SHA256=${GPOINT_PRISMA_EROS_MAPPING_SHA256:-NOT_RUN}
MIGRATION_PRISMA_MESSAGE_STATE_SHA256=${GPOINT_PRISMA_MESSAGE_STATE_SHA256:-NOT_RUN}
MIGRATION_AGATA_0012_SHA256=${GPOINT_AGATA_0012_SHA256:-NOT_RUN}
MIGRATION_TABLE_ROW_COUNTS_SHA256=$migration_table_counts_sha
MIGRATION_AGATA_PROFILE_SEMANTICS_SHA256=$migration_profile_semantics_sha
EOF
chmod 600 "$evidence"
evidence_sha="$(sha256sum "$evidence" | awk '{print $1}')"
printf 'backup_path=%s\nbackup_sha256=%s\nbackup_bytes=%s\nrestore_evidence_path=%s\nrestore_evidence_sha=%s\nrestored_table_count=%s\nmedia_backup_path=%s\nmedia_backup_sha256=%s\nmedia_backup_bytes=%s\nmedia_file_count=%s\nmedia_directory_count=%s\nmedia_total_bytes=%s\nmedia_source_manifest_sha256=%s\nmedia_restore_manifest_sha256=%s\n' \
  "$backup_file" "$backup_sha" "$backup_bytes" "$evidence" "$evidence_sha" "$restored_tables" \
  "$media_backup_file" "$media_backup_sha" "$media_backup_bytes" "$media_file_count" \
  "$media_directory_count" "$media_total_bytes" "$media_manifest_sha" "$media_restore_manifest_sha"
echo GPOINT_ISOLATED_RESTORE=PASS
if [[ "$migration_parity" == PASS && "$migration_data" == PASS && "$migration_access" == PASS ]]; then
  echo GPOINT_MIGRATION_SYNTHETIC_RLS=PASS
  echo GPOINT_MIGRATION_PRODUCTION_GRANTS_RLS=NOT_CHECKED
  echo GPOINT_MIGRATION_REHEARSAL=PASS
fi
