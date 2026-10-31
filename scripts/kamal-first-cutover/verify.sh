#!/usr/bin/env bash
# One-time server-owned phase; never invoke outside kamal_cutover_owner.
set -euo pipefail
curl --retry 12 --retry-connrefused --retry-delay 5 --fail --silent --show-error https://gpoint.amiora.one/api/health/live | grep -Fq '"status":"ok"'
for path in / /creators; do
  curl --retry 6 --retry-connrefused --retry-delay 2 --fail --silent --show-error "https://gpoint.amiora.one$path" | grep -Fq '<div id="root">'
done
remote_manifest_writer="/run/gpoint-kamal-manifest-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.py"
remote_manifest_writer_sha="$(sha256sum scripts/write-kamal-production-manifest.py | awk '{print $1}')"
remote_eros_dir="/run/gpoint-kamal/${CANDIDATE_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
eros_collector_sha="$(sha256sum scripts/collect_kamal_eros_state.py | awk '{print $1}')"
eros_state_helper_sha="$(sha256sum scripts/kamal_eros_state.py | awk '{print $1}')"
eros_inventory_sanitizer_sha="$(sha256sum scripts/sanitize-kamal-inventory.py | awk '{print $1}')"
eros_resource_sanitizer_sha="$(sha256sum scripts/sanitize-kamal-resources.py | awk '{print $1}')"
ssh -F "$HOME/.ssh/config" -T "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$remote_eros_dir" "$eros_collector_sha" "$eros_state_helper_sha" "$eros_inventory_sanitizer_sha" "$eros_resource_sanitizer_sha" <<'REMOTE_EROS_HELPERS'
set -euo pipefail
dir="$1"
[[ "$dir" =~ ^/run/gpoint-kamal/[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]
[[ "$2" =~ ^[0-9a-f]{64}$ && "$3" =~ ^[0-9a-f]{64}$ && "$4" =~ ^[0-9a-f]{64}$ && "$5" =~ ^[0-9a-f]{64}$ ]]
test -d "$dir" && test ! -L "$dir"
for pair in \
  "$dir/eros-state-collector.py:$2" \
  "$dir/kamal_eros_state.py:$3" \
  "$dir/sanitize-kamal-inventory.py:$4" \
  "$dir/sanitize-kamal-resources.py:$5"; do
  file="${pair%%:*}"; expected="${pair#*:}"
  test -f "$file" && test ! -L "$file"
  test "$(sha256sum "$file" | awk '{print $1}')" = "$expected"
done
REMOTE_EROS_HELPERS
ssh -F "$HOME/.ssh/config" -T "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$remote_manifest_writer" <<'REMOTE_CHECK'
set -euo pipefail
[[ "$1" =~ ^/run/gpoint-kamal-manifest-[0-9]+-[0-9]+\.py$ ]]
test ! -e "$1"
REMOTE_CHECK
scp -F "$HOME/.ssh/config" scripts/write-kamal-production-manifest.py "root@${GPOINT_PRODUCTION_HOST}:${remote_manifest_writer}"
verify_log="$RUNNER_TEMP/kamal-verify-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.log"
ssh -F "$HOME/.ssh/config" -T "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$CANDIDATE_SHA" "$IMAGE_VERSION" "$APP_DIGEST" "$AGATA_DIGEST" "$MINIAPP_DIGEST" "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$BASELINE_PATH" "$BASELINE_SHA" "$BACKUP_PATH" "$BACKUP_SHA" "$RESTORE_EVIDENCE_PATH" "$RESTORE_EVIDENCE_SHA" "$DEPLOYMENT_ID" "$remote_manifest_writer" "$remote_manifest_writer_sha" "$MEDIA_BACKUP_PATH" "$MEDIA_BACKUP_SHA" "$MEDIA_SOURCE_MANIFEST_SHA" "$MEDIA_RESTORE_MANIFEST_SHA" "$MEDIA_FILE_COUNT" "$MEDIA_DIRECTORY_COUNT" "$MEDIA_TOTAL_BYTES" "$MEDIA_BACKUP_BYTES" "$APP_SOURCE_SHA" "$eros_collector_sha" "$eros_state_helper_sha" "$NO_EROS_STATE_PATH" "$NO_EROS_STATE_SHA" "$eros_inventory_sanitizer_sha" "$eros_resource_sanitizer_sha" "$PROXY_DIGEST" <<'REMOTE' | tee "$verify_log"
set -euo pipefail
sha="$1"; version="$2"; app="$3"; agata="$4"; miniapp="$5"; run_id="$6"; attempt="$7"; baseline_file="$8"; expected_baseline_sha="$9"; backup_file="${10}"; expected_backup_sha="${11}"; restore_file="${12}"; expected_restore_sha="${13}"
manifest_writer="${15}"; expected_manifest_writer_sha="${16}"
media_backup_file="${17}"; expected_media_backup_sha="${18}"; media_source_manifest_sha="${19}"; media_restore_manifest_sha="${20}"
media_file_count="${21}"; media_directory_count="${22}"; media_total_bytes="${23}"; media_backup_bytes="${24}"; app_source_sha="${25}"
eros_collector_sha="${26}"; eros_state_helper_sha="${27}"; expected_no_eros_file="${28}"; expected_no_eros_file_sha="${29}"
eros_inventory_sanitizer_sha="${30}"; eros_resource_sanitizer_sha="${31}"
proxy_digest="${32}"
[[ "$manifest_writer" =~ ^/run/gpoint-kamal-manifest-[0-9]+-[0-9]+\.py$ && "$expected_manifest_writer_sha" =~ ^[0-9a-f]{64}$ ]]
trap 'rm -f -- "$manifest_writer"' EXIT
test "$(sha256sum "$manifest_writer" | awk '{print $1}')" = "$expected_manifest_writer_sha"
chmod 0700 "$manifest_writer"
[[ "$sha" =~ ^[0-9a-f]{40}$ && "$app_source_sha" =~ ^[0-9a-f]{40}$ && "$expected_baseline_sha" =~ ^[0-9a-f]{64}$ && "$expected_backup_sha" =~ ^[0-9a-f]{64}$ && "$expected_restore_sha" =~ ^[0-9a-f]{64}$ && "$proxy_digest" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$expected_media_backup_sha" =~ ^[0-9a-f]{64}$ && "$media_source_manifest_sha" =~ ^[0-9a-f]{64}$ && "$media_restore_manifest_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$media_file_count" =~ ^[0-9]+$ && "$media_directory_count" =~ ^[0-9]+$ && "$media_total_bytes" =~ ^[0-9]+$ && "$media_backup_bytes" =~ ^[0-9]+$ && "$media_backup_bytes" -gt 0 ]]
[[ "$run_id" =~ ^[0-9]+$ && "$attempt" =~ ^[0-9]+$ ]]
[[ "$eros_collector_sha" =~ ^[0-9a-f]{64}$ && "$eros_state_helper_sha" =~ ^[0-9a-f]{64}$ && "$expected_no_eros_file_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$eros_inventory_sanitizer_sha" =~ ^[0-9a-f]{64}$ && "$eros_resource_sanitizer_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$baseline_file" == "/opt/gpoint/release-logs/${sha}/kamal-cutover-baseline-${run_id}-${attempt}.txt" ]]
test -s "$baseline_file"
test "$(sha256sum "$baseline_file" | awk '{print $1}')" = "$expected_baseline_sha"
[[ "$backup_file" == "/opt/gpoint/backups/gpoint-pre-release-${sha}-${run_id}-${attempt}.dump.age" ]]
[[ "$media_backup_file" == "/opt/gpoint/backups/gpoint-pre-release-${sha}-${run_id}-${attempt}.media.tar.age" ]]
[[ "$restore_file" == "/opt/gpoint/release-logs/${sha}/kamal-backup-restore-${run_id}-${attempt}.txt" ]]
test -s "$backup_file" && test -s "$media_backup_file" && test -s "$restore_file"
test "$(sha256sum "$backup_file" | awk '{print $1}')" = "$expected_backup_sha"
test "$(sha256sum "$media_backup_file" | awk '{print $1}')" = "$expected_media_backup_sha"
test "$(stat -c '%s' "$media_backup_file")" = "$media_backup_bytes"
test "$(sha256sum "$restore_file" | awk '{print $1}')" = "$expected_restore_sha"
grep -qx "CANDIDATE_SHA=$sha" "$restore_file"
grep -qx "RUN_ID=$run_id" "$restore_file"
grep -qx "ATTEMPT=$attempt" "$restore_file"
grep -qx "BACKUP_PATH=$backup_file" "$restore_file"
grep -qx "BACKUP_SHA256=$expected_backup_sha" "$restore_file"
grep -qx 'ISOLATED_RESTORE=PASS' "$restore_file"
grep -qx 'BACKUP_ENCRYPTION=AGE_X25519' "$restore_file"
grep -qx 'OFFSITE_RESTORE=PASS' "$restore_file"
grep -qx "MEDIA_BACKUP_PATH=$media_backup_file" "$restore_file"
grep -qx "MEDIA_BACKUP_SHA256=$expected_media_backup_sha" "$restore_file"
grep -qx "MEDIA_BACKUP_BYTES=$media_backup_bytes" "$restore_file"
grep -qx "MEDIA_FILE_COUNT=$media_file_count" "$restore_file"
grep -qx "MEDIA_DIRECTORY_COUNT=$media_directory_count" "$restore_file"
grep -qx "MEDIA_TOTAL_BYTES=$media_total_bytes" "$restore_file"
grep -qx "MEDIA_SOURCE_MANIFEST_SHA256=$media_source_manifest_sha" "$restore_file"
grep -qx "MEDIA_RESTORE_MANIFEST_SHA256=$media_restore_manifest_sha" "$restore_file"
grep -qx 'MEDIA_BACKUP_ENCRYPTION=AGE_X25519' "$restore_file"
grep -qx "MEDIA_OFFSITE_COPY_SHA256=$expected_media_backup_sha" "$restore_file"
grep -qx 'MEDIA_OFFSITE_RESTORE=PASS' "$restore_file"
grep -qx 'MEDIA_ISOLATED_RESTORE=PASS' "$restore_file"
baseline_value() {
  local result
  result="$(sed -n "s/^$1=//p" "$baseline_file")"
  test -n "$result"
  printf '%s' "$result"
}
test "$(baseline_value BASELINE_SCHEMA)" = gpoint-kamal-cutover-baseline/v1
test "$(baseline_value CANDIDATE_SHA)" = "$sha"
test "$(baseline_value RUN_ID)" = "$run_id"
test "$(baseline_value ATTEMPT)" = "$attempt"
test "$(sha256sum "$(baseline_value INVENTORY_FILE)" | awk '{print $1}')" = "$(baseline_value INVENTORY_SHA256)"
inventory_sha="$(baseline_value INVENTORY_SHA256)"
inventory_file="$(baseline_value INVENTORY_FILE)"
python3 - "$inventory_file" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as source:
    inventory = json.load(source)
if inventory.get('schema') != 'gpoint-vps-inventory/v1' or not inventory.get('containers'):
    raise SystemExit('fresh sanitized VPS inventory is missing or invalid')
running = {row['name'] for row in inventory['containers'] if row['running']}
resource_rows = inventory.get('resourceUsage')
if not isinstance(resource_rows, list) or not resource_rows:
    raise SystemExit('fresh per-container resource sample is missing')
sampled = {row.get('containerName') for row in resource_rows}
if sampled != running or len(sampled) != len(resource_rows):
    raise SystemExit('resource sample does not match the fresh running-container inventory')
for row in resource_rows:
    if any(not isinstance(row.get(key), (int, float)) or row[key] < 0 for key in
           ('cpuPercent', 'memoryUsedBytes', 'memoryLimitBytes', 'memoryPercent', 'pids')):
        raise SystemExit('resource sample contains invalid CPU or memory facts')
PY
test "$(docker inspect --format '{{.Id}}' shared_postgres_1)" = "$(baseline_value POSTGRES_CONTAINER_ID)"
test "$(docker inspect --format '{{.Image}}' shared_postgres_1)" = "$(baseline_value POSTGRES_IMAGE_ID)"
test "$(docker inspect --format '{{.State.Running}}' shared_postgres_1)" = true
database_facts="$(docker exec -i shared_postgres_1 sh -ec 'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint' <<'SQL'
SELECT json_build_object(
  'systemIdentifier',(SELECT system_identifier::text FROM pg_control_system()),
  'serverVersionNum',current_setting('server_version_num')::integer,
  'databases',(SELECT json_agg(json_build_object('name',datname,'owner',pg_get_userbyid(datdba),'allowConnections',datallowconn,'acl',datacl::text) ORDER BY datname) FROM pg_database WHERE datname IN ('gpoint','postgres','template1','eros')),
  'erosRoles',(SELECT json_agg(json_build_object('name',rolname,'login',rolcanlogin,'superuser',rolsuper,'createDatabase',rolcreatedb,'createRole',rolcreaterole,'bypassRls',rolbypassrls) ORDER BY rolname) FROM pg_roles WHERE rolname IN ('eros_ddl','eros_runtime','eros_migrator')),
  'erosMemberships',(SELECT json_agg(json_build_object('role',pg_get_userbyid(roleid),'member',pg_get_userbyid(member)) ORDER BY roleid,member) FROM pg_auth_members WHERE roleid IN (SELECT oid FROM pg_roles WHERE rolname IN ('eros_ddl','eros_runtime','eros_migrator')) OR member IN (SELECT oid FROM pg_roles WHERE rolname IN ('eros_ddl','eros_runtime','eros_migrator')))
)::text;
SQL
)"
postgres_system_identifier="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["systemIdentifier"])' "$database_facts")"
postgres_server_version_num="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["serverVersionNum"])' "$database_facts")"
test "$postgres_system_identifier" = "$(baseline_value POSTGRES_SYSTEM_IDENTIFIER)"
test "$postgres_server_version_num" = "$(baseline_value POSTGRES_SERVER_VERSION_NUM)"
test "$(printf '%s' "$database_facts" | sha256sum | awk '{print $1}')" = "$(baseline_value POSTGRES_ACL_FINGERPRINT)"
no_eros_baseline_file="$(baseline_value NO_EROS_STATE_FILE)"
no_eros_baseline_sha="$(baseline_value NO_EROS_STATE_SHA256)"
[[ "$expected_no_eros_file" == "/opt/gpoint/release-logs/${sha}/kamal-no-eros-state-${run_id}-${attempt}.json" ]]
[[ "$no_eros_baseline_file" == "$expected_no_eros_file" && "$no_eros_baseline_sha" == "$expected_no_eros_file_sha" ]]
test "$(sha256sum "$no_eros_baseline_file" | awk '{print $1}')" = "$no_eros_baseline_sha"
no_eros_dir="/run/gpoint-kamal/${sha}-${run_id}-${attempt}"
eros_collector="$no_eros_dir/eros-state-collector.py"
eros_state_helper="$no_eros_dir/kamal_eros_state.py"
[[ "$eros_collector" == "/run/gpoint-kamal/${sha}-${run_id}-${attempt}/eros-state-collector.py" ]]
[[ "$eros_state_helper" == "/run/gpoint-kamal/${sha}-${run_id}-${attempt}/kamal_eros_state.py" ]]
test -s "$eros_collector" && test -s "$eros_state_helper"
test "$(sha256sum "$eros_collector" | awk '{print $1}')" = "$eros_collector_sha"
test "$(sha256sum "$eros_state_helper" | awk '{print $1}')" = "$eros_state_helper_sha"
test "$(sha256sum "$no_eros_dir/sanitize-kamal-inventory.py" | awk '{print $1}')" = "$eros_inventory_sanitizer_sha"
test "$(sha256sum "$no_eros_dir/sanitize-kamal-resources.py" | awk '{print $1}')" = "$eros_resource_sanitizer_sha"
no_eros_postflight_file="/opt/gpoint/release-logs/${sha}/kamal-no-eros-state-${run_id}-${attempt}-postflight.json"
test ! -e "$no_eros_postflight_file"
no_eros_collect_result="$(python3 "$eros_collector" --output "$no_eros_postflight_file")"
grep -qx 'EROS_STATE_COLLECT=PASS' <<< "$no_eros_collect_result"
no_eros_postflight_content_sha="$(sed -n 's/^EROS_STATE_SHA256=//p' <<< "$no_eros_collect_result")"
[[ "$no_eros_postflight_content_sha" =~ ^[0-9a-f]{64}$ ]]
no_eros_postflight_sha="$(sha256sum "$no_eros_postflight_file" | awk '{print $1}')"
[[ "$no_eros_postflight_sha" =~ ^[0-9a-f]{64}$ ]]
python3 "$eros_state_helper" compare --before "$no_eros_baseline_file" --after "$no_eros_postflight_file"
echo 'GPOINT_NO_EROS_BASELINE_POSTFLIGHT=PASS'
caddy_backup="/opt/gpoint/backups/caddy-before-kamal-${sha}-${run_id}-${attempt}"
sha256sum --check --status "${caddy_backup}.sha256"
test "$(awk '{print $1}' "${caddy_backup}.sha256")" = "$(baseline_value CADDYFILE_SHA256)"
! systemctl is-active --quiet caddy
test "$(docker inspect --format '{{.State.Running}}' kamal-proxy)" = true
proxy_image_id="$(docker inspect kamal-proxy --format '{{.Image}}')"
docker image inspect "$proxy_image_id" --format '{{json .RepoDigests}}' | grep -Fq "basecamp/kamal-proxy@$proxy_digest"
proxy_ports="$(docker inspect --format '{{json .NetworkSettings.Ports}}' kamal-proxy)"
grep -Fq '"HostPort":"80"' <<< "$proxy_ports"
grep -Fq '"HostPort":"443"' <<< "$proxy_ports"
echo 'KAMAL_PROXY_PUBLIC_TLS_AND_CADDY_RETIRED=PASS'
for item in \
  "gpoint-core|api|ghcr.io/akhripunov/gpoint-runtime|$app" \
  "gpoint-core|worker|ghcr.io/akhripunov/gpoint-runtime|$app" \
  "gpoint-core|bot|ghcr.io/akhripunov/gpoint-runtime|$app" \
  "gpoint-agata|api|ghcr.io/akhripunov/gpoint-agata-runtime|$agata" \
  "gpoint-agata|memory-worker|ghcr.io/akhripunov/gpoint-agata-runtime|$agata" \
  "gpoint-miniapp|web|ghcr.io/akhripunov/gpoint-miniapp|$miniapp"; do
  IFS='|' read -r service role repository digest <<< "$item"
  container="$service-$role-$version"
  docker inspect "$container" >/dev/null
  test "$(docker inspect --format '{{.State.Running}}' "$container")" = true
  test "$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$container")" = "$app_source_sha"
  image_id="$(docker inspect --format '{{.Image}}' "$container")"
  docker image inspect "$image_id" --format '{{json .RepoDigests}}' | grep -Fq "$repository@$digest"
  case "$service:$role" in
    gpoint-core:api)
      mounts="$(docker inspect --format '{{json .Mounts}}' "$container")"
      grep -Fq '/opt/gpoint/media' <<< "$mounts"
      echo 'GPOINT_KAMAL_MOUNT_CHECK=PASS'
      api_aliases="$(docker inspect --format '{{json (index .NetworkSettings.Networks "gpoint_app").Aliases}}' "$container")"
      networks="$(docker inspect --format '{{json .NetworkSettings.Networks}}' "$container")"
      grep -Fq 'shared_internal' <<< "$networks"
      grep -Eq '"api"' <<< "$api_aliases"
      echo 'GPOINT_KAMAL_NETWORK_ALIAS_CHECK=PASS'
      docker exec "$container" node -e "fetch('http://127.0.0.1:3000/health/live').then(async r => { if (!r.ok || (await r.json()).status !== 'ok') process.exit(1) }).catch(() => process.exit(1))"
      docker exec "$container" node -e "const fs=require('node:fs');const p='/data/media/.gpoint-kamal-verify-'+process.argv[1];const value='gpoint-media-read-write-'+process.argv[1];try{fs.writeFileSync(p,value,{flag:'wx'});if(fs.readFileSync(p,'utf8')!==value)process.exitCode=1;else console.log('GPOINT_MEDIA_READ_WRITE=PASS')}finally{try{fs.unlinkSync(p)}catch{}}" "$sha-$run_id-$attempt"
      ;;
    gpoint-core:worker) docker top "$container" | grep -Fq 'apps/worker/dist/main.js' ;;
    gpoint-core:bot) docker top "$container" | grep -Fq 'apps/bot/dist/main.js' ;;
    gpoint-agata:api)
      aliases="$(docker inspect --format '{{json (index .NetworkSettings.Networks "shared_internal").Aliases}}' "$container")"
      grep -Eq '"agata-api"' <<< "$aliases"
      docker exec "$container" python -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3); assert r.status == 200" ;;
    gpoint-agata:memory-worker) docker exec "$container" python -m app.memory.worker --healthcheck ;;
    gpoint-miniapp:web) docker exec "$container" wget --spider --quiet http://127.0.0.1/ ;;
  esac
done
prisma_status="$(docker exec "gpoint-core-api-$version" pnpm --filter @gpoint/db exec prisma migrate status 2>&1)"
if printf '%s\n' "$prisma_status" | grep -Eiq 'Database schema is up to date|No pending migrations to apply'; then
  ! printf '%s\n' "$prisma_status" | grep -Eiq 'Following migrations have not yet been applied|The following migration(s) have not yet been applied'
else
  echo 'Prisma migration status is pending or unrecognized' >&2
  exit 1
fi
echo 'GPOINT_PRISMA_MIGRATION_HEAD=PASS'
agata_container="gpoint-agata-api-$version"
agata_heads="$(docker exec "$agata_container" alembic heads)"
[[ "$(printf '%s\n' "$agata_heads" | awk 'NF { count++ } END { print count+0 }')" = 1 ]]
expected_agata_head="$(printf '%s\n' "$agata_heads" | awk 'NF { print $1 }')"
actual_agata_head="$(docker exec "$agata_container" python -c 'import os,psycopg; from sqlalchemy.engine import make_url; u=os.environ.get("AGATA_MIGRATOR_DATABASE_URL") or os.environ["AGATA_DATABASE_URL"]; u=u.replace("postgresql+asyncpg://","postgresql+psycopg://",1); url=make_url(u); url=url.set(query={k:v for k,v in url.query.items() if k in {"sslmode","sslrootcert","sslcert","sslkey","connect_timeout","application_name"}}); c=psycopg.connect(url.render_as_string(hide_password=False)); row=c.execute("SELECT version_num FROM agata.alembic_version").fetchall(); assert len(row)==1; print(row[0][0]); c.close()')"
test "$actual_agata_head" = "$expected_agata_head"
echo 'GPOINT_AGATA_MIGRATION_HEAD=PASS'
for role in worker bot memory-worker; do
  case "$role" in
    worker) prefix=gpoint-core-worker ;;
    bot) prefix=gpoint-core-bot ;;
    memory-worker) prefix=gpoint-agata-memory-worker ;;
  esac
  count="$(docker ps -q --filter "name=^/${prefix}-" | wc -l | tr -d ' ')"
  test "$count" = 1 || { echo "Expected one running consumer for $role, found $count" >&2; exit 1; }
done
for service in gsales-api gsales-agata-api gsales-agata-memory-worker; do
  ids="$(docker ps -aq --filter "label=com.docker.compose.service=$service")"
  test -n "$ids" || { echo "Expected stopped GSales service is missing: $service" >&2; exit 1; }
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    test "$(docker inspect --format '{{.State.Running}}' "$id")" = false
  done <<< "$ids"
done
echo 'GPOINT_DUPLICATE_CONSUMER_CHECK=PASS'
echo 'GPOINT_POSTGRES_MIGRATIONS=PASS'
echo 'GPOINT_GSALES_STOPPED=PASS'
deployment_id="${14}"
[[ "$deployment_id" =~ ^[0-9]+$ ]]
manifest_dir="/opt/gpoint/release-logs/${sha}"
manifest_file="${manifest_dir}/kamal-production-${run_id}-${attempt}.json"
install -d -o root -g root -m 0700 "$manifest_dir"
test ! -e "$manifest_file"
python3 "$manifest_writer" \
  --output "$manifest_file" --sha "$sha" --app-source-sha "$app_source_sha" --version "$version" \
  --no-eros-baseline-path "$no_eros_baseline_file" --no-eros-baseline-sha "$no_eros_baseline_sha" \
  --no-eros-postflight-path "$no_eros_postflight_file" --no-eros-postflight-sha "$no_eros_postflight_sha" \
  --app-digest "$app" --agata-digest "$agata" --miniapp-digest "$miniapp" \
  --run-id "$run_id" --attempt "$attempt" --deployment-id "$deployment_id" \
  --baseline-path "$baseline_file" --baseline-sha "$expected_baseline_sha" \
  --inventory-path "$inventory_file" --inventory-sha "$inventory_sha" \
  --backup-path "$backup_file" --backup-sha "$expected_backup_sha" \
  --restore-path "$restore_file" --restore-sha "$expected_restore_sha" \
  --media-backup-path "$media_backup_file" --media-backup-sha "$expected_media_backup_sha" \
  --media-source-manifest-sha "$media_source_manifest_sha" \
  --media-restore-manifest-sha "$media_restore_manifest_sha" \
  --media-file-count "$media_file_count" --media-directory-count "$media_directory_count" \
  --media-total-bytes "$media_total_bytes" --media-backup-bytes "$media_backup_bytes" \
  --agata-candidate-head "$expected_agata_head" --agata-live-head "$actual_agata_head" \
  --postgres-system-identifier "$postgres_system_identifier" \
  --postgres-acl-fingerprint "$(baseline_value POSTGRES_ACL_FINGERPRINT)"
manifest_sha="$(sha256sum "$manifest_file" | awk '{print $1}')"
[[ "$manifest_sha" =~ ^[0-9a-f]{64}$ ]]
echo "GPOINT_KAMAL_MANIFEST_SHA256=$manifest_sha PATH=$manifest_file"
rm -f -- "$eros_collector" "$eros_state_helper" \
  "$no_eros_dir/sanitize-kamal-inventory.py" "$no_eros_dir/sanitize-kamal-resources.py" \
  "$no_eros_dir/pause-no-eros-state.json" "$no_eros_dir/pause-legacy-gpoint.sh"
# The attempt-scoped directory can also hold verified backup and
# deployment helpers. Cleanup must not turn successful Runtime Verify
# into a false failure after the live traffic switch.
test -s "$manifest_file"
test "$(sha256sum "$manifest_file" | awk '{print $1}')" = "$manifest_sha"
REMOTE
mapfile -t manifest_sha_rows < <(sed -n 's/^GPOINT_KAMAL_MANIFEST_SHA256=\([0-9a-f]*\) PATH=.*/\1/p' "$verify_log")
test "${#manifest_sha_rows[@]}" -eq 1
manifest_sha="${manifest_sha_rows[0]}"
[[ "$manifest_sha" =~ ^[0-9a-f]{64}$ ]]
echo "manifest_sha=$manifest_sha" >> "$GITHUB_OUTPUT"
echo "Verified production manifest SHA-256: \`$manifest_sha\`" >> "$GITHUB_STEP_SUMMARY"
echo "GPOINT_KAMAL_VERIFY=PASS SHA=$CANDIDATE_SHA BASELINE_SHA=$BASELINE_SHA INVENTORY_SHA=$inventory_sha BACKUP_SHA=$BACKUP_SHA RESTORE_EVIDENCE_SHA=$RESTORE_EVIDENCE_SHA"
