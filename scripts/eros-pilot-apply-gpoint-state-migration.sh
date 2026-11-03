#!/usr/bin/env bash
set -euo pipefail

: "${CANDIDATE_SHA:?exact approved candidate SHA is required}"
: "${APP_DIGEST:?exact candidate app digest is required}"
: "${GPOINT_PRODUCTION_HOST:?production host is required}"
: "${GPOINT_SSH_KEY:?verified SSH identity file is required}"
: "${GPOINT_KNOWN_HOSTS_FILE:?verified known-hosts file is required}"
: "${KAMAL_REGISTRY_PASSWORD:?temporary GHCR read token is required}"
: "${GITHUB_ACTOR:?GitHub actor is required}"
: "${GITHUB_RUN_ID:?workflow run ID is required}"
: "${GITHUB_RUN_ATTEMPT:?workflow run attempt is required}"

[[ "$CANDIDATE_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$APP_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$GITHUB_ACTOR" =~ ^[A-Za-z0-9-]{1,39}$ ]]
image="ghcr.io/akhripunov/gpoint-runtime@$APP_DIGEST"
migration='20261103140000_gpoint_chat_policy_and_agata_state_copy'
migration_file="packages/db/prisma/migrations/$migration/migration.sql"
expected_checksum="$(sha256sum "$GITHUB_WORKSPACE/$migration_file" | awk '{print $1}')"
[[ "$expected_checksum" =~ ^[0-9a-f]{64}$ ]]
target="root@$GPOINT_PRODUCTION_HOST"
stage="/run/gpoint-eros-state-migration-$CANDIDATE_SHA-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
[[ "$stage" =~ ^/run/gpoint-eros-state-migration-[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]
ssh=(ssh -F /dev/null -T -i "$GPOINT_SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$GPOINT_KNOWN_HOSTS_FILE")
cleanup_remote_stage() {
  local status=$?
  trap - EXIT
  if ! "${ssh[@]}" "$target" bash -s -- "$stage" <<'REMOTE_STAGE_CLEANUP'
set -euo pipefail
path="$1"
[[ "$path" =~ ^/run/gpoint-eros-state-migration-[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]
if [[ -e "$path" || -L "$path" ]]; then
  test -d "$path" && test ! -L "$path" && test "$(stat -c '%a:%u' "$path")" = 700:0
  rm -rf -- "$path"
fi
REMOTE_STAGE_CLEANUP
  then
    [[ "$status" -ne 0 ]] || status=1
    echo 'GPOINT_PILOT_MIGRATION_TEMP_CLEANUP=FAILED' >&2
  fi
  exit "$status"
}
trap cleanup_remote_stage EXIT

backup_output="$("${ssh[@]}" "$target" bash -s <<'REMOTE_BACKUP'
set -euo pipefail
current=/opt/gpoint/gpoint-backup-current/scripts/gpoint-scheduled-backup.py
resolved="$(readlink -f -- "$current")"
[[ "$resolved" =~ ^/opt/gpoint/backup-releases/[0-9a-f]{40}/scripts/gpoint-scheduled-backup\.py$ ]]
test -x "$resolved"
policy="$(dirname "$resolved")/gpoint-backup-policy.py"
test -x "$policy"
python3 "$resolved"
python3 "$policy" freshness --manifest /opt/gpoint/backups/scheduled/latest.json --max-age-hours 1
REMOTE_BACKUP
)"
printf '%s\n' "$backup_output"
grep -Fx 'GPOINT_BACKUP_SCHEDULED=PASS' <<<"$backup_output" >/dev/null
grep -Fx 'GPOINT_BACKUP_MANIFEST=/opt/gpoint/backups/scheduled/latest.json' <<<"$backup_output" >/dev/null
grep -Fx 'GPOINT_BACKUP_FRESHNESS=PASS' <<<"$backup_output" >/dev/null
"${ssh[@]}" "$target" python3 - /opt/gpoint/backups/scheduled/latest.json <<'REMOTE_BACKUP_VERIFY'
import hashlib,json,pathlib,re,stat,sys
p=pathlib.Path(sys.argv[1]); info=p.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600: raise SystemExit('GPoint backup manifest identity invalid')
x=json.loads(p.read_text())
if x.get('schema')!='gpoint-scheduled-backup/v1' or x.get('status')!='PASS': raise SystemExit('GPoint encrypted backup did not pass')
for section in ('database','media'):
 row=x.get(section,{})
 if row.get('encrypted') is not True or row.get('integrity')!='PASS' or row.get('isolated_restore')!='PASS' or not re.fullmatch(r'[0-9a-f]{64}',row.get('sha256','')): raise SystemExit('GPoint backup/isolated restore evidence incomplete')
 if not isinstance(row.get('bytes'),int) or row['bytes']<1: raise SystemExit('GPoint backup artifact is empty')
print('GPOINT_ENCRYPTED_BACKUP_AND_ISOLATED_RESTORE=PASS manifest_sha256='+hashlib.sha256(p.read_bytes()).hexdigest())
REMOTE_BACKUP_VERIFY

"${ssh[@]}" "$target" bash -s -- "$stage" <<'REMOTE_STAGE_CREATE'
set -euo pipefail
path="$1"
[[ "$path" =~ ^/run/gpoint-eros-state-migration-[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]
test ! -e "$path" && test ! -L "$path"
mkdir -m 700 -- "$path"
REMOTE_STAGE_CREATE
printf '%s' "$KAMAL_REGISTRY_PASSWORD" | "${ssh[@]}" "$target" "umask 077; cat > '$stage/registry-token'"
"${ssh[@]}" "$target" bash -s -- "$stage" "$CANDIDATE_SHA" "$image" "$migration" "$expected_checksum" "$GITHUB_ACTOR" <<'REMOTE_MIGRATE'
set -euo pipefail
stage="$1"; candidate="$2"; image="$3"; migration="$4"; checksum="$5"; actor="$6"
[[ "$stage" =~ ^/run/gpoint-eros-state-migration-${candidate}-[0-9]+-[0-9]+$ ]]
[[ "$candidate" =~ ^[0-9a-f]{40}$ && "$image" =~ ^ghcr\.io/akhripunov/gpoint-runtime@sha256:[0-9a-f]{64}$ ]]
[[ "$migration" == 20261103140000_gpoint_chat_policy_and_agata_state_copy && "$checksum" =~ ^[0-9a-f]{64}$ ]]
[[ "$actor" =~ ^[A-Za-z0-9-]{1,39}$ ]]
test -d "$stage" && test ! -L "$stage" && test "$(stat -c '%a:%u' "$stage")" = 700:0
cleanup() { rm -rf -- "$stage"; }
trap cleanup EXIT
install -d -m 700 "$stage/docker"
DOCKER_CONFIG="$stage/docker" docker login ghcr.io --username "$actor" --password-stdin < "$stage/registry-token" >/dev/null
rm -f -- "$stage/registry-token"
DOCKER_CONFIG="$stage/docker" docker pull --platform linux/amd64 "$image" >/dev/null
test "$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$candidate"
docker image inspect "$image" --format '{{json .RepoDigests}}' | grep -F "$image" >/dev/null

verify_inventory() {
  docker run --rm --network shared_internal --env-file /opt/gpoint/env/production.env \
    --workdir /app/apps/api --entrypoint node "$image" -e '
const fs=require("node:fs"),path=require("node:path"),crypto=require("node:crypto");
const {PrismaClient}=require("@prisma/client"); const db=new PrismaClient();
(async()=>{try{
 const [migration,expected]=process.argv.slice(1),root="/app/packages/db/prisma/migrations";
 const files=fs.readdirSync(root).filter(n=>fs.existsSync(path.join(root,n,"migration.sql"))).sort();
 const rows=await db.$queryRawUnsafe("SELECT migration_name,checksum,finished_at,rolled_back_at FROM public._prisma_migrations ORDER BY migration_name");
 const applied=new Map();
 for(const row of rows){if(row.rolled_back_at)continue;if(!row.finished_at)throw Error("unfinished Prisma migration");if(applied.has(row.migration_name))throw Error("duplicate Prisma migration");applied.set(row.migration_name,row.checksum)}
 for(const [name,hash] of applied){if(!files.includes(name))throw Error("database migration absent from exact candidate image");const actual=crypto.createHash("sha256").update(fs.readFileSync(path.join(root,name,"migration.sql"))).digest("hex");if(hash!==actual)throw Error("applied migration checksum mismatch")}
 const pending=files.filter(name=>!applied.has(name));if(pending.some(name=>name!==migration)||pending.length>1)throw Error("unexpected pending Prisma migration");
 const actual=crypto.createHash("sha256").update(fs.readFileSync(path.join(root,migration,"migration.sql"))).digest("hex");if(actual!==expected)throw Error("candidate migration file checksum mismatch");
 if(applied.has(migration)&&applied.get(migration)!==expected)throw Error("pilot migration ledger checksum mismatch");
 console.log("GPOINT_PRISMA_PREFLIGHT=PASS pending="+(pending[0]||"NONE"));
}catch(e){console.error("GPOINT_PRISMA_PREFLIGHT=BLOCKED reason="+e.message);process.exitCode=1}finally{await db.$disconnect()}})();' "$migration" "$checksum"
}

verify_inventory
docker run --rm --network shared_internal --env-file /opt/gpoint/env/production.env \
  --workdir /app --entrypoint pnpm "$image" --filter @gpoint/db exec prisma migrate deploy
verify_inventory
docker run --rm --network shared_internal --env-file /opt/gpoint/env/production.env \
  --workdir /app/apps/api --entrypoint node "$image" -e '
const {PrismaClient}=require("@prisma/client");const db=new PrismaClient();
(async()=>{try{const rows=await db.$queryRawUnsafe("SELECT checksum FROM public._prisma_migrations WHERE migration_name=$1 AND finished_at IS NOT NULL AND rolled_back_at IS NULL",process.argv[1]);if(rows.length!==1||rows[0].checksum!==process.argv[2])throw Error("migration head readback mismatch");console.log("GPOINT_PILOT_STATE_MIGRATION=PASS exact_sha=true checksum_match=true")}catch(e){console.error("GPOINT_PILOT_STATE_MIGRATION=BLOCKED reason="+e.message);process.exitCode=1}finally{await db.$disconnect()}})();' "$migration" "$checksum"
REMOTE_MIGRATE
