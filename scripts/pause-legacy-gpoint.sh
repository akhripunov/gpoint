#!/usr/bin/env bash
set -euo pipefail

candidate="${1:?candidate SHA required}"
run_id="${2:?GitHub run id required}"
attempt="${3:?GitHub run attempt required}"
baseline_file="${4:?cutover baseline required}"
baseline_sha="${5:?baseline checksum required}"

[[ "$candidate" =~ ^[0-9a-f]{40}$ ]]
[[ "$run_id" =~ ^[0-9]+$ && "$attempt" =~ ^[0-9]+$ ]]
[[ "$baseline_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$baseline_file" == "/opt/gpoint/release-logs/${candidate}/kamal-cutover-baseline-${run_id}-${attempt}.txt" ]]
test -s "$baseline_file"
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

inventory_file="$(baseline_value INVENTORY_FILE)"
inventory_sha="$(baseline_value INVENTORY_SHA256)"
[[ "$inventory_file" == "/opt/gpoint/release-logs/${candidate}/kamal-vps-inventory-${run_id}-${attempt}.json" ]]
[[ "$inventory_sha" =~ ^[0-9a-f]{64}$ ]]
test -s "$inventory_file"
test "$(sha256sum "$inventory_file" | awk '{print $1}')" = "$inventory_sha"

test "$(docker inspect --format '{{.Id}}' shared_postgres_1)" = "$(baseline_value POSTGRES_CONTAINER_ID)"
test "$(docker inspect --format '{{.Image}}' shared_postgres_1)" = "$(baseline_value POSTGRES_IMAGE_ID)"
test "$(docker inspect --format '{{.State.Running}}' shared_postgres_1)" = true
test "$(docker inspect --format '{{.Id}}' compose-eros-1)" = "$(baseline_value EROS_CONTAINER_ID)"
test "$(docker inspect --format '{{.Image}}' compose-eros-1)" = "$(baseline_value EROS_IMAGE_ID)"
test "$(docker inspect --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' compose-eros-1)" = 'true healthy'
test "$(docker inspect --format '{{.Id}}' compose-eros-embedding-relay-1)" = "$(baseline_value EROS_RELAY_CONTAINER_ID)"
test "$(docker inspect --format '{{.Image}}' compose-eros-embedding-relay-1)" = "$(baseline_value EROS_RELAY_IMAGE_ID)"
test "$(docker inspect --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' compose-eros-embedding-relay-1)" = 'true healthy'
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
  if [[ "$status" -ne 0 && "$cutover_complete" -ne 1 ]]; then
    restore_legacy_services
  fi
  exit "$status"
}
trap on_exit EXIT

for service in "${stop_order[@]}"; do
  id="${id_by_service[$service]}"
  was_running="${running_by_service[$service]}"
  if [[ "$was_running" == true ]]; then
    test "$(docker inspect --format '{{.State.Running}}' "$id")" = true
    docker stop --time 120 "$id" >/dev/null
    stopped_by_service[$service]="$id"
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

cutover_complete=1
echo 'GPOINT_LEGACY_CONSUMERS_PAUSED=PASS'
echo 'GPOINT_LEGACY_CUTOVER_GATE=PASS'
