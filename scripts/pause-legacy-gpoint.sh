#!/usr/bin/env bash
set -euo pipefail

candidate="${1:?candidate SHA required}"
run_id="${2:?GitHub run id required}"
attempt="${3:?GitHub run attempt required}"
baseline_file="${4:?cutover baseline required}"
baseline_sha="${5:?baseline checksum required}"
media_manifest_sha="${6:?source media manifest SHA-256 required}"
media_tree_helper="${7:?media tree manifest helper required}"
eros_collector="${8:?no-Eros state collector required}"
eros_state_helper="${9:?no-Eros snapshot validator required}"
eros_collector_sha="${10:?no-Eros collector SHA-256 required}"
eros_state_helper_sha="${11:?no-Eros validator SHA-256 required}"
eros_inventory_sanitizer_sha="${12:?inventory sanitizer SHA-256 required}"
eros_resource_sanitizer_sha="${13:?resource sanitizer SHA-256 required}"

[[ "$candidate" =~ ^[0-9a-f]{40}$ ]]
[[ "$run_id" =~ ^[0-9]+$ && "$attempt" =~ ^[0-9]+$ ]]
[[ "$baseline_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$media_manifest_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$eros_collector_sha" =~ ^[0-9a-f]{64}$ && "$eros_state_helper_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$eros_inventory_sanitizer_sha" =~ ^[0-9a-f]{64}$ && "$eros_resource_sanitizer_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$baseline_file" == "/opt/gpoint/release-logs/${candidate}/kamal-cutover-baseline-${run_id}-${attempt}.txt" ]]
[[ "$media_tree_helper" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/media-tree.py" ]]
[[ "$eros_collector" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/eros-state-collector.py" ]]
[[ "$eros_state_helper" == "/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/kamal_eros_state.py" ]]
test -s "$baseline_file"
test -s "$media_tree_helper" && test -s "$eros_collector" && test -s "$eros_state_helper"
test -s "$(dirname "$eros_collector")/sanitize-kamal-inventory.py"
test -s "$(dirname "$eros_collector")/sanitize-kamal-resources.py"
test ! -L "$(dirname "$eros_collector")"
test ! -L "$eros_collector" && test ! -L "$eros_state_helper"
test "$(sha256sum "$eros_collector" | awk '{print $1}')" = "$eros_collector_sha"
test "$(sha256sum "$eros_state_helper" | awk '{print $1}')" = "$eros_state_helper_sha"
test "$(sha256sum "$(dirname "$eros_collector")/sanitize-kamal-inventory.py" | awk '{print $1}')" = "$eros_inventory_sanitizer_sha"
test "$(sha256sum "$(dirname "$eros_collector")/sanitize-kamal-resources.py" | awk '{print $1}')" = "$eros_resource_sanitizer_sha"
test "$(sha256sum "$baseline_file" | awk '{print $1}')" = "$baseline_sha"

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
test "$(baseline_value KAMAL_PROXY_BASELINE)" = ABSENT
test -z "$(docker ps -aq --filter 'name=^/kamal-proxy$')"
! docker network inspect kamal >/dev/null 2>&1
! docker volume inspect kamal-proxy-config >/dev/null 2>&1
eros_state_baseline="$(baseline_value NO_EROS_STATE_FILE)"
eros_state_baseline_sha="$(baseline_value NO_EROS_STATE_SHA256)"
[[ "$eros_state_baseline" == "/opt/gpoint/release-logs/${candidate}/kamal-no-eros-state-${run_id}-${attempt}.json" ]]
[[ "$eros_state_baseline_sha" =~ ^[0-9a-f]{64}$ ]]
test -s "$eros_state_baseline"
test "$(sha256sum "$eros_state_baseline" | awk '{print $1}')" = "$eros_state_baseline_sha"
eros_state_pause="/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/pause-no-eros-state.json"
test ! -e "$eros_state_pause"
eros_state_collect_result="$(python3 "$eros_collector" --output "$eros_state_pause")"
grep -qx 'EROS_STATE_COLLECT=PASS' <<< "$eros_state_collect_result"
eros_state_pause_sha="$(sed -n 's/^EROS_STATE_SHA256=//p' <<< "$eros_state_collect_result")"
[[ "$eros_state_pause_sha" =~ ^[0-9a-f]{64}$ ]]
python3 "$eros_state_helper" compare --before "$eros_state_baseline" --after "$eros_state_pause"

inventory_file="$(baseline_value INVENTORY_FILE)"
inventory_sha="$(baseline_value INVENTORY_SHA256)"
[[ "$inventory_file" == "/opt/gpoint/release-logs/${candidate}/kamal-vps-inventory-${run_id}-${attempt}.json" ]]
[[ "$inventory_sha" =~ ^[0-9a-f]{64}$ ]]
test -s "$inventory_file"
test "$(sha256sum "$inventory_file" | awk '{print $1}')" = "$inventory_sha"

test "$(docker inspect --format '{{.Id}}' shared_postgres_1)" = "$(baseline_value POSTGRES_CONTAINER_ID)"
test "$(docker inspect --format '{{.Image}}' shared_postgres_1)" = "$(baseline_value POSTGRES_IMAGE_ID)"
test "$(docker inspect --format '{{.State.Running}}' shared_postgres_1)" = true
test "$(sha256sum /etc/caddy/Caddyfile | awk '{print $1}')" = "$(baseline_value CADDYFILE_SHA256)"
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl is-active --quiet caddy

service_rows_output="$(python3 - "$inventory_file" <<'PY'
import json
import re
import sys

required = {'api', 'worker', 'bot', 'agata-api', 'agata-memory-worker'}
gsales = {'gsales-api', 'gsales-agata-api', 'gsales-agata-memory-worker'}
with open(sys.argv[1], encoding='utf-8') as source:
    inventory = json.load(source)
if inventory.get('schema') != 'gpoint-vps-inventory/v1':
    raise SystemExit('fresh inventory schema is invalid')
containers = inventory.get('containers')
if not isinstance(containers, list) or not containers:
    raise SystemExit('fresh inventory has no containers')
selected = {}
stopped_gsales = set()
for row in containers:
    service = row.get('composeService')
    if service in required:
        if service in selected or row.get('composeProject') != 'compose':
            raise SystemExit('legacy GPoint service identity is ambiguous')
        if not re.fullmatch(r'[0-9a-f]{64}', str(row.get('id', ''))):
            raise SystemExit('legacy GPoint container ID is invalid')
        selected[service] = row
    if service in gsales:
        if row.get('running') is not False:
            raise SystemExit('GSales must remain stopped')
        stopped_gsales.add(service)
if set(selected) != required or stopped_gsales != gsales:
    raise SystemExit('fresh inventory lacks required GPoint or stopped GSales services')
for service in sorted(required):
    row = selected[service]
    print(f"{service}|{row['id']}|{'true' if row.get('running') else 'false'}")
PY
)"
mapfile -t service_rows <<< "$service_rows_output"
test "${#service_rows[@]}" -eq 5

stop_order=(worker bot agata-memory-worker api agata-api)
restart_order=(agata-api api agata-memory-worker bot worker)
declare -A id_by_service=()
declare -A running_by_service=()
declare -A stopped_by_service=()
for row in "${service_rows[@]}"; do
  IFS='|' read -r service id was_running <<< "$row"
  test "$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$id")" = "$service"
  test "$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$id")" = compose
  live_running="$(docker inspect --format '{{.State.Running}}' "$id")"
  test "$live_running" = "$was_running"
  id_by_service[$service]="$id"
  running_by_service[$service]="$was_running"
done

cutover_complete=0
media_manifest_file=""
restore_legacy_services() {
  local service id
  for service in "${restart_order[@]}"; do
    id="${stopped_by_service[$service]:-}"
    [[ -n "$id" ]] || continue
    docker start "$id" >/dev/null || echo "Could not restart legacy container $id during pre-deploy recovery." >&2
  done
}
on_exit() {
  local status=$?
  trap - EXIT
  if [[ -n "$media_manifest_file" ]]; then rm -f -- "$media_manifest_file"; fi
  rm -f -- "$media_tree_helper"
  if [[ "$status" -ne 0 || "$cutover_complete" -ne 1 ]]; then
    rm -f -- "$eros_state_pause"
  fi
  if [[ "$status" -ne 0 && "$cutover_complete" -ne 1 ]]; then
    restore_legacy_services
  fi
  exit "$status"
}
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for service in "${stop_order[@]}"; do
  id="${id_by_service[$service]}"
  was_running="${running_by_service[$service]}"
  if [[ "$was_running" == true ]]; then
    test "$(docker inspect --format '{{.State.Running}}' "$id")" = true
    # A failed or interrupted stop may already have stopped this exact ID.
    stopped_by_service[$service]="$id"
    docker stop --time 120 "$id" >/dev/null
  fi
  test "$(docker inspect --format '{{.State.Running}}' "$id")" = false
done

for service in gsales-api gsales-agata-api gsales-agata-memory-worker; do
  ids="$(docker ps -aq --filter "label=com.docker.compose.service=$service")"
  test -n "$ids"
  while IFS= read -r id; do
    test "$(docker inspect --format '{{.State.Running}}' "$id")" = false
  done <<< "$ids"
done

# Recheck the backed-up tree after legacy API consumers are quiesced. If media
# changed during the offsite backup/restore window, the EXIT trap restarts every
# legacy consumer this helper stopped and the cutover fails before Caddy stops.
media_manifest_file="/run/gpoint-kamal/${candidate}-${run_id}-${attempt}/media-quiesced.json"
test ! -e "$media_manifest_file"
media_manifest_output="$(python3 "$media_tree_helper" manifest --root /opt/gpoint/media --output "$media_manifest_file")"
grep -qx 'GPOINT_MEDIA_MANIFEST=PASS' <<< "$media_manifest_output"
test "$(sed -n 's/^GPOINT_MEDIA_MANIFEST_SHA256=//p' <<< "$media_manifest_output")" = "$media_manifest_sha"
echo 'GPOINT_MEDIA_QUIESCED_TREE_MATCH=PASS'

cutover_complete=1
echo 'GPOINT_LEGACY_CONSUMERS_PAUSED=PASS'
echo 'GPOINT_LEGACY_CUTOVER_GATE=PASS'
