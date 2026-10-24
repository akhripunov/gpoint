const SHA40 = /^[a-f0-9]{40}$/;
const DIGEST = /^sha256:[a-f0-9]{64}$/;
const EROS_REPOSITORY = 'ghcr.io/akhripunov/gpoint-eros-pilot';

export function validateErosProductionImage(bundle, sha) {
  const image = bundle?.pilotImages?.eros;
  if (!SHA40.test(sha ?? '') || bundle?.candidateSha !== sha || bundle?.gates?.erosPilotImage !== 'PASS' ||
      bundle?.gates?.erosPilotGhcrReadback !== 'PASS' || bundle?.gates?.erosStockMigrationApiE2E !== 'PASS' ||
      image?.repository !== EROS_REPOSITORY || image?.sourceSha !== sha || !DIGEST.test(image?.digest ?? '') ||
      !DIGEST.test(image?.configDigest ?? '') || image?.platform !== 'linux/amd64')
    throw new Error('exact stock Eros image receipt and Prepare gates are required');
  return `${EROS_REPOSITORY}@${image.digest}`;
}

export function erosProvisionCommand(sha, image) {
  if (!SHA40.test(sha ?? '') || !/^ghcr\.io\/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$/.test(image ?? ''))
    throw new Error('exact Eros source and immutable image are required');
  return `set -euo pipefail; umask 077; install -d -m 700 /opt/gpoint/backups/eros; if [ -f /opt/gpoint/env/eros.env ] && [ ! -f /opt/gpoint/backups/eros/eros-env-before-${sha}.env ]; then cp /opt/gpoint/env/eros.env /opt/gpoint/backups/eros/eros-env-before-${sha}.env; chmod 600 /opt/gpoint/backups/eros/eros-env-before-${sha}.env; fi; python3 /opt/gpoint/releases/${sha}/source/scripts/eros-production-secrets.py --source-sha ${sha} --image '${image}' --ensure-postgres-network --provision-database; test "$(stat -c '%a:%u' /opt/gpoint/env/eros.env)" = '600:0'; echo EROS_DATABASE_PROVISION=PASS`;
}

export function erosBackupCommand(sha) {
  if (!SHA40.test(sha ?? '')) throw new Error('exact Eros release SHA required');
  return `set -euo pipefail; umask 077; install -d -m 700 /opt/gpoint/backups/eros; stamp=$(date -u +%Y%m%dT%H%M%SZ); file=/opt/gpoint/backups/eros/eros-before-${sha}-$stamp.dump; docker exec shared_postgres_1 sh -ec 'exec pg_dump -U "$POSTGRES_USER" -d eros -Fc' > "$file"; test -s "$file"; docker exec -i shared_postgres_1 pg_restore --list < "$file" >/dev/null; sha256sum "$file" > "$file.sha256"; chmod 600 "$file" "$file.sha256"; echo EROS_BACKUP_STATUS=PASS`;
}

export function erosRollbackCommand(sha) {
  if (!SHA40.test(sha ?? '')) throw new Error('exact Eros release SHA required');
  return `set -euo pipefail; docker stop compose-eros-1 >/dev/null 2>&1 || true; backup=$(find /opt/gpoint/backups/eros -maxdepth 1 -type f -name 'eros-before-${sha}-*.dump' -print | sort | tail -1); test -n "$backup"; sha256sum -c "$backup.sha256"; docker exec -i shared_postgres_1 sh -ec 'exec pg_restore --exit-on-error --clean --if-exists --no-owner -U "$POSTGRES_USER" -d eros' < "$backup"; previousEnv=/opt/gpoint/backups/eros/eros-env-before-${sha}.env; if [ -f "$previousEnv" ]; then install -m 600 "$previousEnv" /opt/gpoint/env/eros.env; RELEASE_SHA=$(sed -n 's/^RELEASE_SHA=//p' /opt/gpoint/env/production.env | head -1) docker compose -p compose --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/eros.env -f /opt/gpoint/compose/docker-compose.yml up -d --no-build eros; fi; echo EROS_DATABASE_ROLLBACK=PASS`;
}

export function erosStartCommand(sha) {
  if (!SHA40.test(sha ?? '')) throw new Error('exact Eros release SHA required');
  const compose = `EROS_TEST_USER_IDS= EROS_TEST_CREATOR_IDS= docker compose -p compose --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/eros.env -f /tmp/gpoint-compose-${sha}.yml`;
  const rollback = erosRollbackCommand(sha);
  return `set -euo pipefail; available=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo); printf 'EROS_PRE_APPLY_MEMAVAILABLE_KIB=%s\\n' "$available"; test "$available" -ge 1048576; RELEASE_SHA=${sha} ${compose} config --quiet; RELEASE_SHA=${sha} ${compose} up -d --no-build eros-embedding-relay; RELEASE_SHA=${sha} ${compose} up --no-deps eros-model-verify; ${erosBackupCommand(sha)}; restore_on_error(){ code=$?; trap - ERR; if [ "$code" -ne 0 ]; then if ${rollback}; then echo EROS_AUTOMATIC_ROLLBACK=PASS; else echo EROS_AUTOMATIC_ROLLBACK=FAILED; fi; fi; exit "$code"; }; trap restore_on_error ERR; RELEASE_SHA=${sha} ${compose} stop eros; RELEASE_SHA=${sha} ${compose} up --no-deps eros-migrate; RELEASE_SHA=${sha} ${compose} up -d --no-build eros; ready=0; for attempt in $(seq 1 60); do health=$(docker inspect compose-eros-1 --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo missing); if [ "$health" = 'true healthy' ]; then ready=1; break; fi; sleep 2; done; test "$ready" = 1; expectedImage=$(sed -n 's/^EROS_IMAGE=//p' /opt/gpoint/env/eros.env | head -1); actualImage=$(docker inspect compose-eros-1 --format '{{.Config.Image}}'); test "$actualImage" = "$expectedImage"; imageId=$(docker inspect compose-eros-1 --format '{{.Image}}'); test "$imageId" = "$(docker image inspect "$expectedImage" --format '{{.Id}}')"; revision=$(docker image inspect "$imageId" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'); test "$revision" = 'd1a140083321687212fb7b17c09cc0c3c9639f5e'; integrationRevision=$(docker image inspect "$imageId" --format '{{index .Config.Labels "com.gpoint.integration.revision"}}'); test "$integrationRevision" = '${sha}'; echo EROS_IMAGE_IDENTITY=PASS; available=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo); printf 'EROS_POST_APPLY_MEMAVAILABLE_KIB=%s\\n' "$available"; test "$available" -ge 262144; docker stats --no-stream --format 'EROS_CONTAINER={{.Name}} MEM={{.MemUsage}}' compose-eros-1 compose-eros-embedding-relay-1; trap - ERR; echo EROS_PERMANENT_HEALTH=PASS`;
}

export function erosSyntheticRouteReadbackCommand() {
  return `set -euo pipefail; env=$(docker inspect compose-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}'); printf '%s\\n' "$env" | grep -Fx 'EROS_PRODUCTION_TEST_MODE=true'; printf '%s\\n' "$env" | grep -Fx 'EROS_DATA_MODE=synthetic'; printf '%s\\n' "$env" | grep -Fx 'EROS_PRODUCTION_TEST_USER_IDS='; printf '%s\\n' "$env" | grep -Fx 'EROS_PRODUCTION_TEST_CREATOR_IDS='; echo EROS_SYNTHETIC_ROUTE_ONLY=PASS`;
}

export function erosSyntheticComposeConfigCheckCommand(sha) {
  if (!SHA40.test(sha ?? '')) throw new Error('exact Eros release SHA required');
  const code = `import json,pathlib,sys
p=pathlib.Path('/opt/gpoint/release-logs/${sha}/effective-compose.json')
d=json.loads(p.read_text())
e=d['services']['api']['environment']
expected={'EROS_ENABLED':'true','EROS_PRODUCTION_TEST_MODE':'true','EROS_DATA_MODE':'synthetic','EROS_PRODUCTION_TEST_USER_IDS':'','EROS_PRODUCTION_TEST_CREATOR_IDS':''}
if any(e.get(k)!=v for k,v in expected.items()): raise SystemExit('synthetic-only Eros Compose gate failed')
print('EROS_SYNTHETIC_COMPOSE_CONFIG=PASS')`;
  return `printf '%s' '${Buffer.from(code).toString('base64')}' | base64 -d | python3 -`;
}

export function erosComposeLayerCommand(sha) {
  if (!SHA40.test(sha ?? '')) throw new Error('exact Eros release SHA required');
  return `set -euo pipefail; RELEASE_SHA=${sha} docker compose -p compose --profile agata --profile gsales --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/eros.env -f /opt/gpoint/releases/${sha}/source/infra/production/docker-compose.yml -f /opt/gpoint/releases/${sha}/source/infra/production/docker-compose.gsales.yml -f /opt/gpoint/releases/${sha}/source/infra/production/docker-compose.eros.yml config --quiet; RELEASE_SHA=${sha} docker compose -p compose --profile agata --profile gsales --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/eros.env -f /opt/gpoint/releases/${sha}/source/infra/production/docker-compose.yml -f /opt/gpoint/releases/${sha}/source/infra/production/docker-compose.gsales.yml -f /opt/gpoint/releases/${sha}/source/infra/production/docker-compose.eros.yml config --no-interpolate --no-env-resolution > /tmp/gpoint-compose-${sha}.yml;`;
}
