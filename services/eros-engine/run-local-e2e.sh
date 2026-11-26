#!/bin/sh
# LOCAL TEST ONLY: isolated API, Eros, deterministic Worker and tmpfs PostgreSQL.
set -eu
[ "$#" -eq 1 ] || [ "$#" -eq 2 ] || { echo 'usage: run-local-e2e.sh <exact-built-eros-image-tag> [immutable-core-image]' >&2; exit 2; }
repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image=$1
release_core=${2:-}
api_root=/workspace
if [ -n "$release_core" ]; then api_root=/app; fi
for cmd in docker python3 node; do command -v "$cmd" >/dev/null || { echo "required_tool_missing=$cmd" >&2; exit 2; }; done
local_context=$(node "$repo/services/eros-engine/local-test-policy.mjs" context) || exit 2
docker_config=${DOCKER_CONFIG:-$HOME/.docker}
docker_local(){ env -u DOCKER_HOST -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH DOCKER_CONFIG="$docker_config" DOCKER_CONTEXT="$local_context" docker "$@"; }
image_id=$(node "$repo/services/eros-engine/local-test-policy.mjs" image "$image") || exit 2
printf 'LOCAL_E2E_DOCKER_CONTEXT=LOCAL_UNIX:%s\n' "$local_context"
printf 'LOCAL_E2E_EROS_IMAGE_ID=%s\n' "$image_id"
printf 'LOCAL_E2E_STOCK_EROS_IMAGE=PASS source_sha=%s\n' "$(cat "$(dirname -- "$0")/upstream.sha")"
if [ -n "$release_core" ]; then
  [[ "$release_core" =~ ^ghcr\.io/akhripunov/gpoint-runtime@sha256:[0-9a-f]{64}$ ]] || { echo 'immutable_candidate_core_required' >&2; exit 2; }
  test "$(docker_local image inspect "$release_core" --format '{{.Os}}/{{.Architecture}}')" = linux/amd64
  test "$(docker_local image inspect "$release_core" --format '{{index .Config.Labels "service"}}')" = gpoint-core
  docker_local image inspect "$release_core" --format '{{json .RepoDigests}}' | grep -F "\"$release_core\"" >/dev/null
fi

tmp=$(mktemp -d "${TMPDIR:-/tmp}/me36-local-e2e.XXXXXX"); chmod 700 "$tmp"
project="me36_$(python3 -c 'import secrets;print(secrets.token_hex(10))')"
integration_container="${project}-worker-integration"
admin=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
provider=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
signing=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
tenant=$(python3 -c 'import uuid;print(uuid.uuid4())')
jwt=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
genome_write=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
telegram_bot_token=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
telegram_webhook_secret=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
worker_token=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
umask 077; envfile="$tmp/compose.env"
cat > "$envfile" <<EOF
EROS_IMAGE=$image_id
EROS_TEST_PROJECT=$project
EROS_TEST_ADMIN_PASSWORD=$admin
EROS_TEST_GPOINT_API_KEY=$provider
EROS_TEST_BRIDGE_SIGNING_SECRET=$signing
EROS_TEST_TENANT_ID=$tenant
EROS_TEST_SUPABASE_JWT_SECRET=$jwt
EROS_TEST_GENOME_WRITE_SECRET=$genome_write
EROS_TEST_TELEGRAM_BOT_TOKEN=$telegram_bot_token
EROS_TEST_TELEGRAM_WEBHOOK_SECRET=$telegram_webhook_secret
EROS_TEST_WORKER_TOKEN=$worker_token
EROS_TEST_OPENROUTER_API_KEY=
EOF
unset admin provider signing jwt genome_write telegram_bot_token telegram_webhook_secret
if [ -n "$release_core" ]; then
  printf 'GPOINT_E2E_CORE_IMAGE=%s\nGPOINT_E2E_WORKER_DEPS=%s\n' "$release_core" "$tmp/worker-deps" >> "$envfile"
fi
compose(){
  if [ -n "$release_core" ]; then
    env -i PATH="$PATH" HOME="$HOME" DOCKER_CONFIG="$docker_config" DOCKER_CONTEXT="$local_context" docker compose --env-file "$envfile" -p "$project" -f "$repo/services/eros-engine/compose.local-test.yml" -f "$repo/services/eros-engine/compose.release-test.yml" --profile test "$@"
  else
    env -i PATH="$PATH" HOME="$HOME" DOCKER_CONFIG="$docker_config" DOCKER_CONTEXT="$local_context" docker compose --env-file "$envfile" -p "$project" -f "$repo/services/eros-engine/compose.local-test.yml" --profile test "$@"
  fi
}
. "$repo/services/eros-engine/local-e2e-image-tools.sh"
run_manifest="$tmp/resources.manifest"
printf 'project=%s\n' "$project" >"$run_manifest"
worker_id=
compose_started=0
run_owned=0
cleanup(){
  rc=$?
  trap - EXIT HUP INT TERM
  cleanup_failed=0
  if [ "$rc" -ne 0 ] && [ "$compose_started" -eq 1 ]; then
    migrator_container=$(compose ps --all --quiet eros-migrate 2>/dev/null || true)
    if [ -n "$migrator_container" ]; then
      echo LOCAL_E2E_MIGRATOR_LOG_BEGIN >&2
      docker_local logs --tail 100 "$migrator_container" 2>&1 \
        | sed -E -e 's#(postgres(ql)?://)[^[:space:]@]+@#\1[REDACTED]@#g' \
          -e 's/((password|token|secret)[=: ]+)[^[:space:]]+/\1[REDACTED]/Ig' >&2 || true
      echo LOCAL_E2E_MIGRATOR_LOG_END >&2
    fi
    compose logs --no-color postgres gpoint-api-migrate gpoint-api eros gpoint-worker 2>/dev/null | grep -Ei -C 2 "error|exception|failed|fatal|panic|denied|not found|refused" | tail -100 >&2 || true
  fi
  if [ "$compose_started" -eq 1 ]; then
    compose down --volumes --remove-orphans >/dev/null 2>&1 || cleanup_failed=1
  fi
  if [ "$run_owned" -eq 1 ]; then
    if integration_identity=$(inspect_run_container "$integration_container"); then
      integration_owner=${integration_identity#*|}
      if [ "$integration_owner" = "$project" ]; then
        docker_local rm -f "$integration_container" >/dev/null || cleanup_failed=1
      else
        cleanup_failed=1
      fi
    else
      inspect_status=$?
      [ "$inspect_status" -eq 2 ] || cleanup_failed=1
    fi
    containers=$(docker_local ps -aq --filter "label=com.docker.compose.project=$project") || cleanup_failed=1
    for container in ${containers:-}; do
      container_owner=$(docker_local inspect "$container" --format '{{index .Config.Labels "com.docker.compose.project"}}') || cleanup_failed=1
      if [ "$container_owner" = "$project" ]; then
        docker_local rm -f "$container" >/dev/null || cleanup_failed=1
      else
        cleanup_failed=1
      fi
    done
    containers=$(docker_local ps -aq --filter "label=com.docker.compose.project=$project") || cleanup_failed=1
    networks=$(docker_local network ls -q --filter "label=com.docker.compose.project=$project") || cleanup_failed=1
    for network in ${networks:-}; do
      network_owner=$(docker_local network inspect "$network" --format '{{index .Labels "com.docker.compose.project"}}') || cleanup_failed=1
      network_name=$(docker_local network inspect "$network" --format '{{.Name}}') || cleanup_failed=1
      network_containers=$(docker_local network inspect "$network" --format '{{len .Containers}}') || cleanup_failed=1
      if [ "$network_owner" = "$project" ] && [ "$network_name" = "${project}_eros_test" ] && [ "$network_containers" = 0 ]; then
        docker_local network rm "$network" >/dev/null || cleanup_failed=1
      else
        cleanup_failed=1
      fi
    done
    volumes=$(docker_local volume ls -q --filter "label=com.docker.compose.project=$project") || cleanup_failed=1
    if [ -n "${containers:-}${networks:-}${volumes:-}" ]; then cleanup_failed=1; fi
  fi
  if [ -z "$release_core" ] && [ -f "$run_manifest" ]; then
    for service in gpoint-api gpoint-api-migrate gpoint-worker; do
      image_ref="${project}-${service}:latest"
      manifest_entry=$(awk -F'|' -v ref="$image_ref" '$1 == ref { print; exit }' "$run_manifest")
      if [ -z "$manifest_entry" ]; then
        # A tag left behind after a failed build/inspection has no verified
        # identity. Preserve it and make cleanup visibly fail closed.
        if image_identity=$(inspect_run_image "$image_ref"); then
          cleanup_failed=1
          echo "LOCAL_E2E_IMAGE_OWNERSHIP=UNVERIFIED tag=$image_ref reason=not_in_manifest" >&2
        else
          inspect_status=$?
          if [ "$inspect_status" -ne 2 ]; then cleanup_failed=1; fi
        fi
        continue
      fi
      IFS='|' read -r manifest_ref expected_image_id image_owner expected_service <<EOF
$manifest_entry
EOF
      case "$image_ref" in
        "${project}-gpoint-api:latest"|"${project}-gpoint-api-migrate:latest"|"${project}-gpoint-worker:latest") ;;
        *) cleanup_failed=1; echo "LOCAL_E2E_IMAGE_OWNERSHIP=FAIL" >&2; continue ;;
      esac
      if image_identity=$(inspect_run_image "$image_ref"); then
        :
      else
        inspect_status=$?
        if [ "$inspect_status" -eq 2 ]; then
          cleanup_failed=1
          echo "LOCAL_E2E_IMAGE_INSPECT=FAIL tag=$image_ref reason=manifest_image_missing" >&2
        else
          cleanup_failed=1
        fi
        continue
      fi
      current_image_id=${image_identity%%|*}
      image_labels=${image_identity#*|}
      if [ "$manifest_ref" != "$image_ref" ] || [ "$current_image_id" != "$expected_image_id" ] || [ "$image_labels" != "$project|$expected_service" ] || [ "$image_owner" != "$project" ] || [ "$expected_service" != "$service" ]; then
        cleanup_failed=1
        echo "LOCAL_E2E_IMAGE_OWNERSHIP=FAIL tag=$image_ref reason=identity_changed" >&2
        continue
      fi
      image_users=$(docker_local ps -aq --filter "ancestor=$expected_image_id") || { cleanup_failed=1; continue; }
      if [ -n "$image_users" ]; then
        echo "LOCAL_E2E_IMAGE_RETAINED=IN_USE service_tag=$image_ref" >&2
        cleanup_failed=1
      elif ! docker_local image rm "$image_ref" >/dev/null; then
        cleanup_failed=1
      else
        echo "LOCAL_E2E_IMAGE_CLEANUP=PASS tag=$image_ref"
      fi
    done
  fi
  rm -rf "$tmp" || cleanup_failed=1
  if [ "$cleanup_failed" -ne 0 ]; then
    echo "LOCAL_E2E_CLEANUP=FAIL project=$project" >&2
    [ "$rc" -ne 0 ] || rc=1
  else
    echo "LOCAL_E2E_CLEANUP=PASS project=$project"
  fi
  exit "$rc"
}
trap cleanup EXIT HUP INT TERM
for service in gpoint-api gpoint-api-migrate gpoint-worker; do
  image_ref="${project}-${service}:latest"
  if image_identity=$(inspect_run_image "$image_ref"); then
    echo "LOCAL_E2E_RESOURCE_COLLISION=FAIL image=$image_ref" >&2
    exit 2
  else
    inspect_status=$?
    [ "$inspect_status" -eq 2 ] || { echo "LOCAL_E2E_IMAGE_INSPECT=FAIL tag=$image_ref phase=preflight" >&2; exit 2; }
  fi
done
for resource_kind in container network volume; do
  case "$resource_kind" in
    container) existing=$(docker_local ps -aq --filter "label=com.docker.compose.project=$project") ;;
    network) existing=$(docker_local network ls -q --filter "label=com.docker.compose.project=$project") ;;
    volume) existing=$(docker_local volume ls -q --filter "label=com.docker.compose.project=$project") ;;
  esac
  [ -z "$existing" ] || { echo "LOCAL_E2E_RESOURCE_COLLISION=FAIL type=$resource_kind" >&2; exit 2; }
done
if integration_identity=$(inspect_run_container "$integration_container"); then
  echo LOCAL_E2E_RESOURCE_COLLISION=FAIL type=integration_container >&2
  exit 2
else
  inspect_status=$?
  [ "$inspect_status" -eq 2 ] || { echo LOCAL_E2E_CONTAINER_INSPECT=FAIL phase=preflight >&2; exit 2; }
fi
run_owned=1
mkdir -m 700 "$tmp/home"
if [ -n "$release_core" ]; then
  # Install existing pinned Worker dependencies into a disposable directory;
  # use the upstream Python image directly, not a Docker build.
  mkdir -m 755 "$tmp/worker-deps"
  worker_base=python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
  docker_local pull --platform linux/amd64 "$worker_base"
  docker_local run --rm --platform linux/amd64 --user "$(id -u):$(id -g)" -e HOME=/tmp \
    --mount "type=bind,src=$tmp/worker-deps,dst=/deps" "$worker_base" \
    python -m pip install --no-cache-dir --only-binary=:all: --target /deps \
    httpx==0.28.1 certifi==2026.7.22 anyio==4.15.1 httpcore==1.0.9 \
    idna==3.20 h11==0.16.0 typing_extensions==4.16.0
  # Validate merged Compose configuration before starting services.
  compose --profile worker config --format json | python3 -c '
import json, sys
services = json.load(sys.stdin)["services"]
if any(service.get("build") for service in services.values()):
    raise SystemExit("release E2E must never build an image")
for name in ("gpoint-api", "gpoint-api-migrate"):
    if services[name]["image"] != sys.argv[1]:
        raise SystemExit("release E2E Core digest mismatch")
if services["gpoint-worker"]["image"] != sys.argv[2]:
    raise SystemExit("release E2E Worker base mismatch")
' "$release_core" "$worker_base"
  echo "LOCAL_E2E_READY_IMAGES=PASS core=$release_core"
else
for service in gpoint-api gpoint-api-migrate gpoint-worker; do
  image_ref="${project}-${service}:latest"
  echo "LOCAL_E2E_IMAGE_BUILD=BEGIN service=$service"
  compose --profile worker build "$service"
  image_identity=$(docker_local image inspect "$image_ref" --format '{{.Id}}|{{index .Config.Labels "com.gpoint.me36.run"}}|{{index .Config.Labels "com.gpoint.me36.service"}}')
  expected_identity="$project|$service"
  [ "${image_identity#*|}" = "$expected_identity" ] || { echo "LOCAL_E2E_IMAGE_OWNERSHIP=FAIL tag=$image_ref reason=build_label_mismatch" >&2; exit 1; }
  # Record each image immediately after its own build and ownership validation,
  # so a later service build failure cannot orphan earlier verified images.
  printf '%s|%s|%s|%s\n' "$image_ref" "${image_identity%%|*}" "$project" "$service" >>"$run_manifest"
  echo "LOCAL_E2E_IMAGE_BUILD=PASS service=$service"
done
fi
echo LOCAL_E2E_COMPOSE_START=BEGIN
compose_started=1
compose up -d --wait --wait-timeout 240 --no-build postgres
compose up -d --no-deps --no-build eros-migrate
migrator_container=$(compose ps --all --quiet eros-migrate)
[ -n "$migrator_container" ] || { echo LOCAL_E2E_STOCK_MIGRATIONS=FAIL reason=container_missing >&2; exit 1; }
migrator_status=$(docker_local wait "$migrator_container")
[ "$migrator_status" = 0 ] || { echo "LOCAL_E2E_STOCK_MIGRATIONS=FAIL exit_code=$migrator_status" >&2; exit 1; }
echo LOCAL_E2E_STOCK_MIGRATIONS=PASS
# Same least-privilege grants, rehearsed on the actual stock schema.
sed -e "s/current_database() <> 'eros'/current_database() <> 'eros_test'/" \
  -e 's/GRANT CONNECT ON DATABASE eros TO/GRANT CONNECT ON DATABASE eros_test TO/' \
  "$repo/services/eros-engine/world-enrollment/writer-acl.sql" \
  | compose exec -T postgres psql -U eros_test_admin -d eros_test -v ON_ERROR_STOP=1
compose exec -T postgres psql -U eros_test_admin -d eros_test -v ON_ERROR_STOP=1 <<'SQL'
REVOKE CONNECT ON DATABASE gpoint_test,postgres,template1 FROM PUBLIC;
ALTER ROLE eros_world_writer LOGIN PASSWORD 'local-only-world-writer';
SQL
writer_acl_query=$(python3 - "$repo/scripts/eros-native-database.py" <<'PY'
import importlib.util,sys
spec=importlib.util.spec_from_file_location("eros_native_database",sys.argv[1])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.world_writer_acl_query()+';')
PY
)
writer_acl="$(printf '%s\n' "$writer_acl_query" | compose exec -T postgres psql -U eros_test_admin -d eros_test -XqAt -v ON_ERROR_STOP=1)"
[ "$writer_acl" = t ] || { echo LOCAL_E2E_WORLD_WRITER_ACL=FAIL >&2; exit 1; }
echo LOCAL_E2E_WORLD_WRITER_ACL=PASS
compose exec -T postgres psql -U eros_test_admin -d eros_test -v ON_ERROR_STOP=1 <<'SQL'
GRANT USAGE ON SCHEMA engine TO eros_runtime, eros_runtime_nobypass;
GRANT ALL ON ALL TABLES IN SCHEMA engine TO eros_runtime;
GRANT ALL ON ALL SEQUENCES IN SCHEMA engine TO eros_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA engine TO eros_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT ALL ON TABLES TO eros_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT ALL ON SEQUENCES TO eros_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT EXECUTE ON FUNCTIONS TO eros_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA engine TO eros_runtime_nobypass;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA engine TO eros_runtime_nobypass;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO eros_runtime_nobypass;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO eros_runtime_nobypass;
SQL
compose up -d --wait --wait-timeout 240 --no-build
echo LOCAL_E2E_COMPOSE_START=PASS
eros_api_privileges=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -At <<'SQL'
WITH relation_privileges AS (
  SELECT c.oid, c.relkind, p.privilege
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) AS p(privilege)
  WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
    AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    AND has_table_privilege('gpoint_api_test', c.oid, p.privilege)
), column_privileges AS (
  SELECT c.oid
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
  CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')) AS p(privilege)
  WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
    AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    AND has_column_privilege('gpoint_api_test', c.oid, a.attnum, p.privilege)
), sequence_privileges AS (
  SELECT c.oid
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN (VALUES ('USAGE'), ('SELECT'), ('UPDATE')) AS p(privilege)
  WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
    AND c.relkind = 'S' AND has_sequence_privilege('gpoint_api_test', c.oid, p.privilege)
)
SELECT
  has_database_privilege('gpoint_api_test', 'eros_test', 'CONNECT')::text || '|' ||
  has_schema_privilege('gpoint_api_test', 'engine', 'USAGE')::text || '|' ||
  (SELECT count(*) FROM relation_privileges)::text || '|' ||
  (SELECT count(*) FROM column_privileges)::text || '|' ||
  (SELECT count(*) FROM sequence_privileges)::text;
SQL
)
[ "$eros_api_privileges" = 'false|false|0|0|0' ] || {
  echo "LOCAL_E2E_EROS_DB_API_ISOLATION=FAIL checks=$eros_api_privileges expected=false|false|0|0|0" >&2
  exit 1
}
echo "LOCAL_E2E_EROS_DB_API_ISOLATION=PASS api_role_connect=false schema_usage=false table_privileges=0 column_privileges=0 sequence_privileges=0"
echo LOCAL_E2E_STOCK_GENOME_WRITE_ROUTE=READY
compose exec -T postgres psql -U eros_test_admin -d gpoint_test -v ON_ERROR_STOP=1 <<'SQL'
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA engine TO gpoint_worker_test;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA engine TO gpoint_worker_test;
SQL
compose exec -T postgres psql -U eros_test_admin -d gpoint_test -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO "User" (id,"telegramUserId","telegramUsername","firstName","createdAt","updatedAt") VALUES ('10000000-0000-4000-8000-000000000001',900000001,'eros_test_dialogue','Synthetic',now(),now());
SQL
compose exec -T postgres psql -U eros_test_admin -d gpoint_test -v ON_ERROR_STOP=1 <<SQL
INSERT INTO "GpointChatPolicyProfile"
  (id,"sourceTenantId",version,"termsVersion","privacyVersion",status,"effectiveFrom",rules)
VALUES
  ('eros_test_gpoint_policy','$tenant',1,'gpoint-terms-v1','gpoint-privacy-v1','active',now(),
   '{"minimum_age":18,"ai_disclosure_required":true,"self_attestation_allowed":true,"adult_only_features":[],"disclosure_repeat_hours":24}'::jsonb);
SQL
postgres_id=$(compose ps -q postgres)
api_image=$(compose images -q gpoint-api)
  docker_local run --name "$integration_container" --label "com.docker.compose.project=$project" --network "container:$postgres_id" \
  -e NODE_ENV=test \
  -e DATABASE_URL='postgresql://gpoint_worker_test:local-only-gpoint-worker-test@127.0.0.1:5432/gpoint_test?schema=public' \
  -e EROS_WORKER_DISPOSABLE_TEST_DB=1 \
  -v "$repo/apps/api/test:$api_root/apps/api/test:ro" \
  -v "$repo/packages/db/prisma/migrations:$api_root/packages/db/prisma/migrations:ro" \
  --workdir "$api_root" --entrypoint pnpm "$api_image" --filter @gpoint/api exec node --import tsx --test \
  test/eros-worker-contract.test.ts test/eros-worker-cancel.integration.test.ts
echo LOCAL_E2E_GPOINT_QUEUE_RLS_TENANT_ISOLATION=PASS fixture_role=gpoint_worker_test
compose --profile worker up -d --no-deps --no-build gpoint-worker
if ! compose exec -w "$api_root/apps/api" -T gpoint-api node --import tsx "$api_root/services/eros-engine/local-test-e2e.mjs" >"$tmp/e2e.out" 2>&1; then tail -40 "$tmp/e2e.out" >&2; exit 1; fi
cat "$tmp/e2e.out"
genome_id=$(sed -n 's/^LOCAL_E2E_GPOINT_GENOME_MAPPING=PASS stable_uuid=//p' "$tmp/e2e.out" | tail -1)
[[ "$genome_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] || { echo LOCAL_E2E_GPOINT_GENOME_MAPPING=FAIL reason=invalid_reported_uuid >&2; exit 1; }
eros_genome_count=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -qAtc "SELECT count(*) FROM engine.persona_genomes WHERE id='$genome_id'::uuid;")
[ "$eros_genome_count" = 1 ] || { echo LOCAL_E2E_EROS_GENOME_CREATE=FAIL reason=id_not_present >&2; exit 1; }
echo LOCAL_E2E_STOCK_PERSONA_PROVISIONING=PASS created_by_telegram_connect=YES
eros_rls_stats=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -Atc "SELECT count(*) || '|' || count(*) FILTER (WHERE c.relrowsecurity) || '|' || count(*) FILTER (WHERE c.relforcerowsecurity) || '|' || (SELECT count(*) FROM pg_policies WHERE schemaname='engine') FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='engine' AND c.relkind IN ('r','p');")
IFS='|' read -r eros_tables eros_rls_enabled eros_rls_forced eros_policy_count <<EOF
$eros_rls_stats
EOF
eros_runtime_bypass=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -Atc "SELECT rolbypassrls FROM pg_roles WHERE rolname='eros_runtime';")
eros_admin_personas=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -qAtc 'SET ROLE eros_runtime; SELECT count(*) FROM engine.persona_genomes;')
eros_strict_personas=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -qAtc 'SET ROLE eros_runtime_nobypass; SELECT count(*) FROM engine.persona_genomes;')
[ "$eros_runtime_bypass" = t ] && [ "$eros_admin_personas" -gt 0 ] && [ "$eros_strict_personas" = 0 ] && [ "$eros_tables" -gt 0 ] && [ "$eros_rls_enabled" = "$eros_tables" ] && [ "$eros_rls_forced" = 0 ] && [ "$eros_policy_count" = 0 ] || {
  echo "LOCAL_E2E_EROS_DATABASE_RLS_LIMITATION=UNEXPECTED tables=$eros_tables enabled=$eros_rls_enabled forced=$eros_rls_forced runtime_role_rows=$eros_admin_personas strict_role_rows=$eros_strict_personas policies=$eros_policy_count runtime_bypass=$eros_runtime_bypass" >&2
  exit 1
}
echo "LOCAL_E2E_EROS_DATABASE_RLS_LIMITATION=CONFIRMED tables=$eros_tables enabled=$eros_rls_enabled forced=$eros_rls_forced runtime_role_rows=$eros_admin_personas strict_role_rows=$eros_strict_personas policies=$eros_policy_count runtime_bypass=$eros_runtime_bypass"
relationship_mapping_count=$(compose exec -T postgres psql -U eros_test_admin -d gpoint_test -Atc 'SELECT count(*) FROM "ErosRelationshipMapping";')
[ "$relationship_mapping_count" = 1 ] || { echo "LOCAL_E2E_GPOINT_RELATIONSHIP_MAPPING=FAIL count=$relationship_mapping_count" >&2; exit 1; }
echo LOCAL_E2E_GPOINT_RELATIONSHIP_MAPPING=PASS
session_count=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -Atc "SELECT count(*) FROM engine.chat_sessions WHERE channel <> 'group';")
[ "$session_count" = 1 ] || { echo "LOCAL_E2E_MEMORY_FIXTURE_SESSIONS=FAIL count=$session_count" >&2; exit 1; }
session_id=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -Atc "SELECT id FROM engine.chat_sessions WHERE channel <> 'group' ORDER BY created_at DESC LIMIT 1;")
[ -n "$session_id" ] || { echo LOCAL_E2E_MEMORY_FIXTURE_SESSION=FAIL >&2; exit 1; }
session_id=$(python3 -c 'import sys,uuid; print(uuid.UUID(sys.argv[1]))' "$session_id") || { echo LOCAL_E2E_MEMORY_FIXTURE_SESSION=FAIL >&2; exit 1; }
# Existing synthetic relationship, real stock tables, exact application query.
world_owner=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -XqAtc "SELECT user_id FROM engine.chat_sessions WHERE id='$session_id'::uuid")
world_instance=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -XqAtc "SELECT instance_id FROM engine.chat_sessions WHERE id='$session_id'::uuid")
compose exec -w "$api_root/apps/api" -T gpoint-api node --import tsx --input-type=module -e '
import { PrismaClient } from "@prisma/client";
import { enrollErosWorld } from "./src/eros-world-enrollment.ts";
const db=new PrismaClient({datasourceUrl:"postgresql://eros_world_writer:local-only-world-writer@postgres:5432/eros_test",log:[]});
try { const [owner,session,instance]=process.argv.slice(1);await Promise.all([enrollErosWorld(db,owner,session,instance),enrollErosWorld(db,owner,session,instance)]);await enrollErosWorld(db,owner,session,instance); }
finally { await db.$disconnect(); }
' "$world_owner" "$session_id" "$world_instance"
world_ready=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -XqAtc "SELECT count(*) FROM engine.world_enrollments e JOIN engine.world_worldviews w USING(owner_uid) WHERE e.owner_uid='$world_owner'::uuid AND e.town_enabled AND e.stories_enabled AND w.content ~ '[^[:space:]]'")
[ "$world_ready" = 1 ] || { echo LOCAL_E2E_WORLD_ENROLLMENT=FAIL >&2; exit 1; }
echo LOCAL_E2E_WORLD_ENROLLMENT=PASS
scan_state_pending=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -v ON_ERROR_STOP=1 -Atc \
  "WITH aged_scan_state AS (UPDATE engine.relationship_scan_state SET last_turn_at = now() - interval '2 minutes' WHERE instance_id = (SELECT instance_id FROM engine.chat_sessions WHERE id = '$session_id'::uuid) AND pending_turns >= 3 RETURNING pending_turns) SELECT pending_turns FROM aged_scan_state;")
[[ "$scan_state_pending" =~ ^[0-9]+$ ]] && [ "$scan_state_pending" -ge 3 ] || {
  echo "LOCAL_E2E_MEMORY_FIXTURE_SCAN_STATE=FAIL pending_turns=$scan_state_pending" >&2
  exit 1
}
memory_state=
for attempt in $(seq 1 60); do
  memory_state=$(compose exec -T postgres psql -U eros_test_admin -d eros_test -Atc \
    "SELECT count(*) FROM engine.companion_memories WHERE session_id = '$session_id'::uuid AND category IS NOT NULL AND content LIKE 'SYNTH_%';")
  [ "$memory_state" = "2" ] && break
  sleep 1
done
if [ "$memory_state" != "2" ]; then
  echo "LOCAL_E2E_DURABLE_MEMORY=FAIL state=$memory_state" >&2
  compose exec -T postgres psql -U eros_test_admin -d gpoint_test -Atc \
    "SELECT 'EROS_QUEUE_DIAGNOSTIC=' || job_type || ':' || status || ':' || coalesce(error_code, 'none') || ':attempts=' || attempts FROM engine.eros_inference_jobs ORDER BY created_at;" >&2 || true
  diagnostic_worker=$(compose --profile worker ps -a -q gpoint-worker)
  if [ -n "$diagnostic_worker" ]; then
    echo LOCAL_E2E_WORKER_DIAGNOSTIC_BEGIN >&2
    docker_local logs --tail 80 "$diagnostic_worker" >&2 || true
    echo LOCAL_E2E_WORKER_DIAGNOSTIC_END >&2
  fi
  exit 1
fi
echo LOCAL_E2E_DREAMING_SCAN=PASS candidates=2
echo LOCAL_E2E_DURABLE_MEMORY=PASS candidates=2
if ! compose exec -w "$api_root/apps/api" -T gpoint-api node --import tsx "$api_root/services/eros-engine/local-test-e2e.mjs" --recall-only >"$tmp/recall.out" 2>&1; then tail -40 "$tmp/recall.out" >&2; exit 1; fi
cat "$tmp/recall.out"
worker_id=$(compose --profile worker ps -a -q gpoint-worker)
[ -n "$worker_id" ] || { echo LOCAL_E2E_WORKER_CONTAINER_MISSING=FAIL >&2; exit 1; }
docker_local kill --signal TERM "$worker_id" >/dev/null 2>&1 || true
worker_status=$(docker_local wait "$worker_id")
[ "$worker_status" = 0 ] || { echo LOCAL_E2E_WORKER_EXIT=FAIL >&2; docker_local logs "$worker_id" >&2; exit 1; }
worker_log=$(docker_local logs "$worker_id")
    completed_jobs=$(printf '%s\n' "$worker_log" | grep -c 'LOCAL_TEST_WORKER_COMPLETED=1' || true)
[ "$completed_jobs" -ge 16 ] || { echo "LOCAL_E2E_WORKER_JOBS=FAIL count=$completed_jobs" >&2; printf '%s\n' "$worker_log" >&2; exit 1; }
context_result=$(printf '%s\n' "$worker_log" | node "$repo/services/eros-engine/provider-context-check.mjs")
printf '%s\n' "$context_result"
    if printf '%s\n' "$context_result" | grep -q '^LOCAL_E2E_PROVIDER_CONTEXT=PASS '; then
  memory_recall_status=PASS
else
  memory_recall_status=BLOCKED
fi
    printf '%s\n' "$worker_log" | grep -E 'LOCAL_TEST_PROVIDER_CONTEXT|LOCAL_TEST_WORKER_COMPLETED='
echo LOCAL_E2E_EXISTING_GPOINT_WORKER_QUEUE=PASS
docker_local rm "$worker_id" >/dev/null; worker_id=
compose restart eros
compose up -d --wait --wait-timeout 90 --no-build eros
compose exec -w "$api_root/apps/api" -T gpoint-api node --import tsx "$api_root/services/eros-engine/local-test-e2e.mjs" --history-only
compose exec -T postgres psql -U eros_test_admin -d eros_test -Atc "SELECT 'EROS_CHAT_MESSAGES=' || count(*) FROM engine.chat_messages; SELECT 'EROS_DURABLE_MEMORY_ROWS=' || count(*) FROM engine.companion_memories WHERE category IS NOT NULL AND content LIKE 'SYNTH_%';"
mapping_constraints=$(compose exec -T postgres psql -U eros_test_admin -d gpoint_test -Atc \
  "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND confdeltype = 'r' AND conname IN ('ErosRelationshipMapping_userId_fkey', 'ErosRelationshipMapping_creatorId_fkey');")
[ "$mapping_constraints" = 2 ] || { echo "LOCAL_E2E_GPOINT_MAPPING_OWNERSHIP=FAIL restrictive_constraints=$mapping_constraints" >&2; exit 1; }
mapping_rows=$(compose exec -T postgres psql -U eros_test_admin -d gpoint_test -Atc \
  "SELECT count(*) FROM \"ErosRelationshipMapping\" WHERE \"userId\" = '10000000-0000-4000-8000-000000000001' AND \"creatorId\" = '20000000-0000-4000-8000-000000000002';")
[ "$mapping_rows" = 1 ] || { echo "LOCAL_E2E_GPOINT_MAPPING_OWNERSHIP=FAIL mapping_rows=$mapping_rows" >&2; exit 1; }
echo LOCAL_E2E_GPOINT_MAPPING_OWNERSHIP=PASS account_creator_references_preserved
compose exec -T postgres psql -U eros_test_admin -d gpoint_test -Atc "SELECT 'GPOINT_EROS_QUEUE_JOBS=' || count(*) FROM engine.eros_inference_jobs;"
if [ "$memory_recall_status" = PASS ]; then
  echo LOCAL_E2E_MEMORY_RECALL=PASS
else
  echo LOCAL_E2E_MEMORY_RECALL=BLOCKED reason=provider_context_missing
fi
echo LOCAL_E2E_REAL_INFERENCE=NOT_RUN
echo LOCAL_E2E_EROS_DATABASE_USER_RLS=NOT_AVAILABLE stock_api_ownership_test=PASS
echo LOCAL_E2E_DISPOSABLE_STACK_CLEANUP=ARMED
