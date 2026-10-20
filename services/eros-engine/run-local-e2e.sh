#!/bin/sh
# LOCAL TEST ONLY: isolated API, Eros, deterministic Worker and tmpfs PostgreSQL.
set -eu
[ "$#" -eq 1 ] || { echo 'usage: run-local-e2e.sh <exact-built-eros-image-tag>' >&2; exit 2; }
repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image=$1
for cmd in docker python3.11 node; do command -v "$cmd" >/dev/null || { echo "required_tool_missing=$cmd" >&2; exit 2; }; done
local_context=$(node "$repo/services/eros-engine/local-test-policy.mjs" context) || exit 2
docker_config=${DOCKER_CONFIG:-$HOME/.docker}
docker_local(){ env -u DOCKER_HOST -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH DOCKER_CONFIG="$docker_config" DOCKER_CONTEXT="$local_context" docker "$@"; }
image_id=$(node "$repo/services/eros-engine/local-test-policy.mjs" image "$image") || exit 2
printf 'LOCAL_E2E_DOCKER_CONTEXT=LOCAL_UNIX:%s\n' "$local_context"
printf 'LOCAL_E2E_EROS_IMAGE_ID=%s\n' "$image_id"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/me36-local-e2e.XXXXXX"); chmod 700 "$tmp"
project="me36_$(python3.11 -c 'import secrets;print(secrets.token_hex(5))')"
integration_container="${project}-worker-integration"
admin=$(python3.11 -c 'import secrets;print(secrets.token_urlsafe(32))')
provider=$(python3.11 -c 'import secrets;print(secrets.token_urlsafe(32))')
signing=$(python3.11 -c 'import secrets;print(secrets.token_urlsafe(48))')
tenant=$(python3.11 -c 'import uuid;print(uuid.uuid4())')
jwt=$(python3.11 -c 'import secrets;print(secrets.token_urlsafe(48))')
worker_token=$(python3.11 -c 'import secrets;print(secrets.token_urlsafe(32))')
agata=$(python3.11 -c 'import secrets;print(secrets.token_urlsafe(32))')
umask 077; envfile="$tmp/compose.env"
cat > "$envfile" <<EOF
EROS_IMAGE=$image_id
EROS_TEST_ADMIN_PASSWORD=$admin
EROS_TEST_GPOINT_API_KEY=$provider
EROS_TEST_BRIDGE_SIGNING_SECRET=$signing
EROS_TEST_TENANT_ID=$tenant
EROS_TEST_SUPABASE_JWT_SECRET=$jwt
EROS_TEST_WORKER_TOKEN=$worker_token
EROS_TEST_AGATA_TOKEN=$agata
EOF
unset admin provider signing jwt agata
compose(){ env -i PATH="$PATH" HOME="$HOME" DOCKER_CONFIG="$docker_config" DOCKER_CONTEXT="$local_context" docker compose --env-file "$envfile" -p "$project" -f "$repo/services/eros-engine/compose.local-test.yml" --profile test "$@"; }
worker_id=
compose_started=0
cleanup(){
  rc=$?
  trap - EXIT HUP INT TERM
  cleanup_failed=0
  if [ "$rc" -ne 0 ] && [ "$compose_started" -eq 1 ]; then
    compose logs --no-color postgres gpoint-api-migrate gpoint-api eros gpoint-worker 2>/dev/null | rg -i -C 2 "error|exception|failed|fatal|panic|denied|not found|refused" | tail -100 >&2 || true
  fi
  if [ "$compose_started" -eq 1 ]; then
    compose down --volumes --remove-orphans >/dev/null 2>&1 || cleanup_failed=1
  fi
  if docker_local inspect "$integration_container" >/dev/null 2>&1; then
    integration_owner=$(docker_local inspect "$integration_container" --format '{{index .Config.Labels "com.docker.compose.project"}}') || cleanup_failed=1
    if [ "$integration_owner" = "$project" ]; then
      docker_local rm -f "$integration_container" >/dev/null || cleanup_failed=1
    else
      cleanup_failed=1
    fi
  fi
  containers=$(docker_local ps -aq --filter "label=com.docker.compose.project=$project") || cleanup_failed=1
  networks=$(docker_local network ls -q --filter "label=com.docker.compose.project=$project") || cleanup_failed=1
  volumes=$(docker_local volume ls -q --filter "label=com.docker.compose.project=$project") || cleanup_failed=1
  if [ -n "${containers:-}${networks:-}${volumes:-}" ]; then cleanup_failed=1; fi
  rm -rf "$tmp" || cleanup_failed=1
  if [ "$cleanup_failed" -ne 0 ]; then
    echo "LOCAL_E2E_CLEANUP=FAIL project=$project" >&2
    [ "$rc" -ne 0 ] || rc=1
  fi
  exit "$rc"
}
trap cleanup EXIT HUP INT TERM
mkdir -m 700 "$tmp/home"
echo LOCAL_E2E_COMPOSE_START=BEGIN
compose_started=1
compose up -d --build --wait --wait-timeout 240
echo LOCAL_E2E_COMPOSE_START=PASS
compose exec -T postgres psql -U eros_test_admin -d eros_test -v ON_ERROR_STOP=1 <<'SQL'
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA engine TO gpoint_worker_test;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA engine TO gpoint_worker_test;
SQL
compose exec -T postgres psql -U eros_test_admin -d eros_test -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO "User" (id,"telegramUserId","telegramUsername","firstName","createdAt","updatedAt") VALUES ('eros_test_user',900000001,'eros_test_dialogue','Synthetic',now(),now());
INSERT INTO "Creator" (id,"displayName",slug,"createdAt") VALUES ('eros_test_creator','Synthetic Eros Persona','eros-test-persona',now());
INSERT INTO "CreatorProfile" (id,"creatorId",status,bio,"tagsJson") VALUES ('eros_test_profile','eros_test_creator','APPROVED','Local synthetic test persona','[]');
SQL
postgres_id=$(compose ps -q postgres)
api_image=$(compose images -q gpoint-api)
  docker_local run --name "$integration_container" --label "com.docker.compose.project=$project" --network "container:$postgres_id" \
  -e DATABASE_URL='postgresql://gpoint_worker_test:local-only-gpoint-worker-test@127.0.0.1:5432/eros_test?schema=public' \
  -e EROS_WORKER_DISPOSABLE_TEST_DB=1 \
  -v "$repo/apps/api/test:/workspace/apps/api/test:ro" \
  -v "$repo/packages/db/prisma/migrations:/workspace/packages/db/prisma/migrations:ro" \
  --entrypoint pnpm "$api_image" --filter @gpoint/api exec node --import tsx --test test/eros-worker-cancel.integration.test.ts
compose --profile worker up -d --no-deps gpoint-worker
if ! compose exec -T gpoint-api node services/eros-engine/local-test-e2e.mjs >"$tmp/e2e.out" 2>&1; then tail -40 "$tmp/e2e.out" >&2; exit 1; fi
cat "$tmp/e2e.out"
worker_id=$(compose --profile worker ps -a -q gpoint-worker)
[ -n "$worker_id" ] || { echo LOCAL_E2E_WORKER_CONTAINER_MISSING=FAIL >&2; exit 1; }
worker_status=$(docker_local wait "$worker_id")
[ "$worker_status" = 0 ] || { echo LOCAL_E2E_WORKER_EXIT=FAIL >&2; docker_local logs "$worker_id" >&2; exit 1; }
worker_log=$(docker_local logs "$worker_id")
completed_jobs=$(printf '%s\n' "$worker_log" | rg -c 'LOCAL_TEST_WORKER_COMPLETED=1' || true)
[ "$completed_jobs" -ge 16 ] || { echo "LOCAL_E2E_WORKER_JOBS=FAIL count=$completed_jobs" >&2; printf '%s\n' "$worker_log" >&2; exit 1; }
printf '%s\n' "$worker_log" | rg 'LOCAL_TEST_PROVIDER_CONTEXT|LOCAL_TEST_WORKER_COMPLETED='
echo LOCAL_E2E_EXISTING_GPOINT_WORKER_QUEUE=PASS
docker_local rm "$worker_id" >/dev/null; worker_id=
compose restart eros
compose up -d --wait --wait-timeout 90 eros
compose exec -T gpoint-api node services/eros-engine/local-test-e2e.mjs --history-only
compose exec -T postgres psql -U eros_test_admin -d eros_test -Atc "SELECT 'EROS_CHAT_MESSAGES=' || count(*) FROM engine.chat_messages; SELECT 'EROS_QUEUE_JOBS=' || count(*) FROM engine.eros_inference_jobs;"
echo LOCAL_E2E_MEMORY_EXTRACTION=NOT_CONFIGURED
echo LOCAL_E2E_REAL_INFERENCE=NOT_RUN
echo LOCAL_E2E_RLS_TENANT_ISOLATION=NOT_TESTED
echo LOCAL_E2E_DISPOSABLE_STACK_CLEANUP=ARMED
