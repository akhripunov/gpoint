#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
[[ "$action" == apply || "$action" == cleanup ]] || { echo 'usage: eros-native-pilot-lifecycle.sh apply|cleanup' >&2; exit 2; }
: "${CANDIDATE_SHA:?exact workflow candidate SHA is required}"
: "${GPOINT_PRODUCTION_HOST:?GPoint production host is required}"
: "${GPOINT_SSH_KEY:?verified SSH identity file is required}"
: "${GPOINT_KNOWN_HOSTS_FILE:?verified known-hosts file is required}"
: "${EROS_PILOT_TEST_USER_ID:?exact synthetic user UUID is required}"
: "${EROS_PILOT_TEST_CREATOR_ID:?exact synthetic creator UUID is required}"
: "${EROS_PILOT_POLICY_JURISDICTION:?exact approved policy jurisdiction is required}"
: "${EROS_PILOT_POLICY_MARKET:?exact approved policy market is required}"
[[ "$CANDIDATE_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$EROS_PILOT_TEST_USER_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
[[ "$EROS_PILOT_TEST_CREATOR_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
[[ "$EROS_PILOT_POLICY_JURISDICTION" =~ ^[A-Z0-9_-]{2,32}$ ]]
[[ "$EROS_PILOT_POLICY_MARKET" =~ ^[a-z0-9_-]{2,32}$ ]]
target="root@${GPOINT_PRODUCTION_HOST}"
marker='/opt/gpoint/env/eros-pilot-state.json'
backup_root='/opt/gpoint/backups/eros-pilot'
ssh=(ssh -F /dev/null -T -i "$GPOINT_SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$GPOINT_KNOWN_HOSTS_FILE")
scp=(scp -F /dev/null -i "$GPOINT_SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$GPOINT_KNOWN_HOSTS_FILE")

kamal() {
  local kamal_workspace="${GPOINT_KAMAL_WORKTREE:-$GITHUB_WORKSPACE}"
  docker run --rm --volume "$kamal_workspace:/workdir" \
    --volume "$GPOINT_SSH_KEY:/root/.ssh/id_ed25519:ro" \
    --volume "$GPOINT_KNOWN_HOSTS_FILE:/root/.ssh/known_hosts:ro" --workdir /workdir \
    --env GPOINT_COMMIT_SHA --env GPOINT_APP_SOURCE_SHA --env GPOINT_PRODUCTION_HOST \
    --env KAMAL_REGISTRY_PASSWORD --env GPOINT_KAMAL_EXPECTED_DIGEST --env GPOINT_KAMAL_SERVICE --env KAMAL_ROLES \
    --env GPOINT_SSH_KEY=/root/.ssh/id_ed25519 --env GPOINT_KNOWN_HOSTS_FILE=/root/.ssh/known_hosts \
    ghcr.io/basecamp/kamal:v2.12.0@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625 "$@"
}

remote_py() { "${ssh[@]}" "$target" python3 - "$@"; }
run_ops() {
  local operation="$1" sha="$2" user="$3" creator="$4"; shift 4
  "${ssh[@]}" "$target" python3 "/opt/gpoint/releases/$sha/source/scripts/eros_native_pilot_ops.py" \
    "$operation" --source-sha "$sha" --test-user-id "$user" --test-creator-id "$creator" \
    --environment /opt/gpoint/env/eros.env --backup-root "$backup_root" "$@"
}

check_remote_baseline_raw() {
  "${ssh[@]}" "$target" python3 - < "$GITHUB_WORKSPACE/scripts/verify-kamal-production-baseline.py"
}

check_remote_baseline() {
  local raw
  raw="$(check_remote_baseline_raw)"
  python3 -c 'import json,re,sys; x=json.loads(sys.argv[1]); roles=set(x["roles"]); legacy={"gpoint-core-api","gpoint-core-worker","gpoint-core-bot","gpoint-agata-api","gpoint-agata-memory-worker","gpoint-miniapp-web"}; agata_free=legacy-{"gpoint-agata-api","gpoint-agata-memory-worker"}; assert roles in (legacy,agata_free), "incomplete or mixed Kamal role set"; assert re.fullmatch(r"[0-9a-f]{40}-[0-9]+-[0-9]+",x["version"]); assert re.fullmatch(r"[0-9a-f]{40}",x["source_sha"]); print("\n".join((x["version"],x["source_sha"],x["roles"]["gpoint-core-api"]["repo_digest"])))' "$raw"
}

stage_sources() {
  local archive="$RUNNER_TEMP/eros-pilot-source-${CANDIDATE_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.tar.gz"
  local stage="/run/eros-pilot-stage-${CANDIDATE_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
  cleanup_stage() {
    [[ "$stage" == "/run/eros-pilot-stage-${CANDIDATE_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" ]]
    "${ssh[@]}" "$target" bash -s -- "$stage" <<'REMOTE_CLEAN_STAGE'
set -euo pipefail
path="$1"
[[ "$path" =~ ^/run/eros-pilot-stage-[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]
if [[ -e "$path" || -L "$path" ]]; then
  test -d "$path" && test ! -L "$path"
  find "$path" -mindepth 1 -delete
  rmdir "$path"
fi
REMOTE_CLEAN_STAGE
  }
  trap 'cleanup_stage; rm -f "$archive"' EXIT
  tar -czf "$archive" -C "$GITHUB_WORKSPACE" \
    config/deploy.eros.yml config/deploy.eros-relay.yml config/deploy.eros-api.yml \
    services/eros-engine/eros-production-model-config.toml \
    services/eros-engine/eros-pilot-embedding-relay.mjs \
    services/eros-engine/test-personas/synthetic-eros-persona.toml \
    scripts/eros-production-secrets.py scripts/eros_native_pilot_ops.py \
    scripts/collect_kamal_eros_state.py scripts/kamal_eros_state.py \
    scripts/sanitize-kamal-inventory.py scripts/sanitize-kamal-resources.py \
    scripts/verify-kamal-production-baseline.py
  "${ssh[@]}" "$target" mkdir -m 700 -- "$stage"
  "${scp[@]}" "$archive" "$target:$stage/payload.tar.gz"
  "${ssh[@]}" "$target" bash -s -- "$stage" "$CANDIDATE_SHA" <<'REMOTE_STAGE'
set -euo pipefail
stage="$1"; sha="$2"
[[ "$stage" == "/run/eros-pilot-stage-${sha}-"* && "$sha" =~ ^[0-9a-f]{40}$ ]]
mkdir -m 700 "$stage/source"
tar -xzf "$stage/payload.tar.gz" -C "$stage/source" --no-same-owner
release="/opt/gpoint/releases/$sha/source"
install -d -m 755 "$release"
while IFS= read -r -d '' path; do
  target="$release/$path"
  install -d -m 755 "$(dirname "$target")"
  if [[ -e "$target" ]]; then
    cmp -s "$stage/source/$path" "$target" || { echo 'exact candidate source path already contains different content' >&2; exit 1; }
  else
    install -m 644 "$stage/source/$path" "$target"
  fi
done < <(find "$stage/source" -type f -print0)
REMOTE_STAGE
  rm -f "$archive"
  cleanup_stage
  trap - EXIT
}

write_pilot_marker() {
  local baseline_version="$1" baseline_source="$2" baseline_digest="$3"
  "${ssh[@]}" "$target" python3 - "$marker" "$CANDIDATE_SHA" "$IMAGE_VERSION" \
    "$baseline_version" "$baseline_source" "$baseline_digest" "$APP_DIGEST" "$EROS_PILOT_IMAGE" \
    "$EROS_PILOT_TEST_USER_ID" "$EROS_PILOT_TEST_CREATOR_ID" "$backup_root/eros-before-$CANDIDATE_SHA.dump" <<'REMOTE_MARKER'
import json, os, pathlib, re, sys
path=pathlib.Path(sys.argv[1]); source,version,baseline,base_source,base_digest,app_digest,eros_image,user,creator,backup=sys.argv[2:]
if path.exists() or path.is_symlink(): raise SystemExit('pilot marker already exists')
if not re.fullmatch(r'[0-9a-f]{40}',source) or not re.fullmatch(r'[0-9a-f]{40}-[0-9]+-[0-9]+',version): raise SystemExit('pilot candidate identity invalid')
if not re.fullmatch(r'[0-9a-f]{40}-[0-9]+-[0-9]+',baseline) or not re.fullmatch(r'[0-9a-f]{40}',base_source): raise SystemExit('baseline identity invalid')
if not re.fullmatch(r'ghcr\.io/akhripunov/gpoint-runtime@sha256:[0-9a-f]{64}',base_digest): raise SystemExit('baseline app digest invalid')
if not re.fullmatch(r'sha256:[0-9a-f]{64}',app_digest) or not re.fullmatch(r'ghcr\.io/akhripunov/gpoint-eros-pilot@sha256:[0-9a-f]{64}',eros_image): raise SystemExit('pilot images invalid')
for value in (user,creator):
    if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',value,re.I): raise SystemExit('pilot UUID invalid')
path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
facts={'schema':'gpoint-eros-native-pilot-state/v1','status':'apply-in-progress','sourceSha':source,'imageVersion':version,'baselineVersion':baseline,'baselineSourceSha':base_source,'baselineAppDigest':base_digest,'pilotAppDigest':app_digest,'erosImage':eros_image,'testUserId':user,'testCreatorId':creator,'backup':backup}
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f: json.dump(facts,f,sort_keys=True,separators=(',',':')); f.write('\n'); f.flush(); os.fsync(f.fileno())
REMOTE_MARKER
}

read_marker() {
  "${ssh[@]}" "$target" python3 - "$marker" "$EROS_PILOT_TEST_USER_ID" "$EROS_PILOT_TEST_CREATOR_ID" <<'REMOTE_READ_MARKER'
import json,os,pathlib,re,sys
p=pathlib.Path(sys.argv[1]); user,creator=sys.argv[2:]
if p.is_symlink() or not p.is_file() or p.stat().st_mode&0o077 or p.stat().st_uid!=0: raise SystemExit('pilot marker missing or unsafe')
x=json.loads(p.read_text())
if x.get('schema')!='gpoint-eros-native-pilot-state/v1' or x.get('testUserId')!=user or x.get('testCreatorId')!=creator: raise SystemExit('pilot marker identity mismatch')
required=('sourceSha','imageVersion','baselineVersion','baselineSourceSha','baselineAppDigest','pilotAppDigest','erosImage','backup')
if any(not isinstance(x.get(k),str) for k in required): raise SystemExit('pilot marker is incomplete')
if not re.fullmatch(r'[0-9a-f]{40}',x['sourceSha']) or not re.fullmatch(r'[0-9a-f]{40}-[0-9]+-[0-9]+',x['imageVersion']): raise SystemExit('pilot marker source identity invalid')
if not re.fullmatch(r'[0-9a-f]{40}-[0-9]+-[0-9]+',x['baselineVersion']) or not re.fullmatch(r'[0-9a-f]{40}',x['baselineSourceSha']): raise SystemExit('pilot marker baseline identity invalid')
if not re.fullmatch(r'ghcr\.io/akhripunov/gpoint-runtime@sha256:[0-9a-f]{64}',x['baselineAppDigest']) or not re.fullmatch(r'sha256:[0-9a-f]{64}',x['pilotAppDigest']): raise SystemExit('pilot marker digest invalid')
if not re.fullmatch(r'ghcr\.io/akhripunov/gpoint-eros-pilot@sha256:[0-9a-f]{64}',x['erosImage']): raise SystemExit('pilot marker stock image invalid')
for k in required: print(x[k])
genome=x.get('genomeId','')
if genome and not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',genome,re.I): raise SystemExit('pilot marker genome identity invalid')
print(genome)
REMOTE_READ_MARKER
}

write_pilot_genome_marker() {
  local genome_id="$1"
  "${ssh[@]}" "$target" python3 - "$marker" "$CANDIDATE_SHA" "$genome_id" <<'REMOTE_GENOME_MARKER'
import json,os,pathlib,sys,uuid
p=pathlib.Path(sys.argv[1]); sha,genome=sys.argv[2:]
if p.is_symlink() or not p.is_file() or p.stat().st_mode&0o077 or p.stat().st_uid!=0: raise SystemExit('pilot marker missing or unsafe')
x=json.loads(p.read_text())
if x.get('schema')!='gpoint-eros-native-pilot-state/v1' or x.get('sourceSha')!=sha: raise SystemExit('pilot marker source mismatch')
try: parsed=uuid.UUID(genome)
except ValueError: raise SystemExit('persona genome UUID invalid')
if parsed.int==0: raise SystemExit('persona genome UUID invalid')
x['genomeId']=str(parsed); t=p.with_name('.eros-pilot-state.tmp'); fd=os.open(t,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f: json.dump(x,f,sort_keys=True,separators=(',',':')); f.write('\n'); f.flush(); os.fsync(f.fileno())
os.replace(t,p)
REMOTE_GENOME_MARKER
}

set_creator_genome() {
  local creator="$1" genome="$2" count
  [[ "$creator" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
  [[ "$genome" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
  count="$("${ssh[@]}" "$target" docker exec -i shared_postgres_1 sh -ec 'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint' <<SQL
WITH changed AS (
  UPDATE "CreatorProfile" SET "erosGenomeId"='$genome'::uuid
  WHERE "creatorId"='$creator' AND status='APPROVED' AND "erosGenomeId" IS NULL
  RETURNING 1
)
SELECT count(*) FROM changed;
SQL
  )"
  test "$count" = 1 || { echo 'synthetic creator genome assignment did not update exactly one approved profile' >&2; return 1; }
  echo EROS_SYNTHETIC_CREATOR_GENOME=PASS
}

clear_creator_genome() {
  local creator="$1" genome="$2"
  [[ -n "$genome" ]] || return 0
  [[ "$creator" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
  [[ "$genome" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
  "${ssh[@]}" "$target" bash -s -- "$creator" "$genome" <<'REMOTE_CLEAR_GENOME'
set -euo pipefail
creator="$1"; genome="$2"
state="$(docker exec -i shared_postgres_1 sh -ec 'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint' <<SQL
SELECT coalesce((SELECT "erosGenomeId"::text FROM "CreatorProfile" WHERE "creatorId"='$creator'), 'MISSING');
SQL
)"
if [[ "$state" == "$genome" ]]; then
  changed="$(docker exec -i shared_postgres_1 sh -ec 'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint' <<SQL
WITH changed AS (UPDATE "CreatorProfile" SET "erosGenomeId"=NULL WHERE "creatorId"='$creator' AND "erosGenomeId"='$genome'::uuid RETURNING 1)
SELECT count(*) FROM changed;
SQL
)"
  test "$changed" = 1
elif [[ "$state" != NULL && "$state" != MISSING ]]; then
  echo 'synthetic creator Eros genome changed outside this pilot; refusing to clear' >&2; exit 1
fi
REMOTE_CLEAR_GENOME
}

remove_exact_pilot_containers() {
  local pilot_sha="$1" pilot_version="$2" eros_image="$3" app_digest="$4"
  local current_version="$5" current_source="$6" current_digest="$7"
  "${ssh[@]}" "$target" bash -s -- "$pilot_sha" "$pilot_version" "$eros_image" "$app_digest" \
    "$current_version" "$current_source" "$current_digest" <<'REMOTE_REMOVE'
set -euo pipefail
sha="$1"; version="$2"; eros_image="$3"; app_digest="$4"; current_version="$5"; current_source="$6"; current_digest="$7"
python3 - "$sha" "$version" "$eros_image" "$app_digest" "$current_version" "$current_source" "$current_digest" <<'PY'
import json,re,subprocess,sys
sha,version,eros_image,app_digest,current_version,current_source,current_digest=sys.argv[1:]
if not re.fullmatch(r'[0-9a-f]{40}',sha) or not re.fullmatch(r'[0-9a-f]{40}-[0-9]+-[0-9]+',version): raise SystemExit('pilot container version invalid')
if not re.fullmatch(r'[0-9a-f]{40}-[0-9]+-[0-9]+',current_version) or not re.fullmatch(r'[0-9a-f]{40}',current_source) or not re.fullmatch(r'ghcr\.io/akhripunov/gpoint-runtime@sha256:[0-9a-f]{64}',current_digest): raise SystemExit('current GPoint API identity invalid')
specs=(('gpoint-eros-pilot-engine-'+version,'d1a140083321687212fb7b17c09cc0c3c9639f5e',eros_image,True),('gpoint-eros-relay-relay-'+version,sha,'ghcr.io/akhripunov/gpoint-runtime@'+app_digest,False))
for name,revision,repo_digest,stock in specs:
    p=subprocess.run(['docker','inspect',name],text=True,capture_output=True)
    if p.returncode:
        if 'no such object' in (p.stdout+p.stderr).lower(): continue
        raise SystemExit('pilot container inspection failed')
    try: row=json.loads(p.stdout)[0]
    except Exception: raise SystemExit('pilot container identity unavailable')
    labels=row.get('Config',{}).get('Labels') or {}
    if row.get('Name')!='/'+name or labels.get('org.opencontainers.image.revision')!=revision: raise SystemExit('container name/source identity mismatch')
    if stock and (labels.get('com.gpoint.integration.revision')!=sha or labels.get('com.gpoint.eros.patch-set.sha256')!='none'): raise SystemExit('stock Eros label mismatch')
    image_id=row.get('Image')
    image=json.loads(subprocess.run(['docker','image','inspect',image_id],check=True,text=True,capture_output=True).stdout)[0]
    if repo_digest not in (image.get('RepoDigests') or []): raise SystemExit('pilot service image digest mismatch')
    if row.get('State',{}).get('Running'): subprocess.run(['docker','stop',name],check=True,capture_output=True)
    final=json.loads(subprocess.run(['docker','inspect',name],check=True,text=True,capture_output=True).stdout)[0]
    if final.get('State',{}).get('Running'): raise SystemExit('pilot container did not stop')
    subprocess.run(['docker','rm',name],check=True,capture_output=True)
spec=importlib.util.spec_from_file_location('ops',pathlib.Path('/opt/gpoint/releases')/sha/'source/scripts/eros_native_pilot_ops.py')
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.remove_pilot_api_containers(sha,version,'ghcr.io/akhripunov/gpoint-runtime@'+app_digest,
                                  current_version,current_source,current_digest)
PY
REMOTE_REMOVE
}

rollback_and_cleanup() {
  local marker_values=()
  mapfile -t marker_values < <(read_marker)
  [[ "${#marker_values[@]}" -eq 9 ]] || { echo 'pilot marker value count invalid' >&2; return 1; }
  local pilot_sha="${marker_values[0]}" pilot_version="${marker_values[1]}" baseline_version="${marker_values[2]}"
  local baseline_source="${marker_values[3]}" baseline_digest="${marker_values[4]}" app_digest="${marker_values[5]}"
  local eros_image="${marker_values[6]}" backup="${marker_values[7]}" genome_id="${marker_values[8]}"
  local worktree_root="$RUNNER_TEMP/eros-pilot-kamal-source-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
  local pilot_worktree="$worktree_root/pilot" current_worktree="$worktree_root/current"
  local current_raw current_version current_source current_digest cleanup_target
  current_raw="$(check_remote_baseline_raw)"
  cleanup_target="$(python3 - "$current_raw" "$pilot_version" <<'CLEANUP_TARGET'
import importlib.util,json,pathlib,sys
raw,pilot_version=sys.argv[1:]
baseline=json.loads(raw)
api=baseline['roles']['gpoint-core-api']
spec=importlib.util.spec_from_file_location('ops',pathlib.Path('scripts/eros_native_pilot_ops.py'))
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
target=module.cleanup_api_release_target(baseline['version'],baseline['source_sha'],api['repo_digest'],pilot_version)
print('\n'.join((target['version'],target['sourceSha'],target['repoDigest'])))
CLEANUP_TARGET
  )"
  mapfile -t cleanup_values <<<"$cleanup_target"
  [[ "${#cleanup_values[@]}" -eq 3 ]]
  current_version="${cleanup_values[0]}" current_source="${cleanup_values[1]}" current_digest="${cleanup_values[2]}"
  local current_image_version="$current_version"
  mkdir -p "$worktree_root"
  git -C "$GITHUB_WORKSPACE" worktree add --detach "$pilot_worktree" "$pilot_sha"
  git -C "$GITHUB_WORKSPACE" cat-file -e "$current_source^{commit}"
  git -C "$GITHUB_WORKSPACE" worktree add --detach "$current_worktree" "$current_source"
  export GPOINT_KAMAL_WORKTREE="$current_worktree"
  GPOINT_COMMIT_SHA="$current_source" GPOINT_APP_SOURCE_SHA="$current_source" \
    GPOINT_KAMAL_EXPECTED_DIGEST="${current_digest##*@}" GPOINT_KAMAL_SERVICE=gpoint-core KAMAL_ROLES=api \
    kamal deploy --skip-push --version "$current_image_version" --roles api --config-file config/deploy.yml
  "${ssh[@]}" "$target" python3 - "$current_image_version" "$current_source" "$current_digest" <<'REMOTE_CLEAN_API'
import json,re,subprocess,sys
version,source,digest=sys.argv[1:]
if not re.fullmatch(r'[0-9a-f]{40}-[0-9]+-[0-9]+',version) or not re.fullmatch(r'[0-9a-f]{40}',source) or not re.fullmatch(r'ghcr\.io/akhripunov/gpoint-runtime@sha256:[0-9a-f]{64}',digest): raise SystemExit('cleanup API identity invalid')
name='gpoint-core-api-'+version
row=json.loads(subprocess.run(['docker','inspect',name],check=True,text=True,capture_output=True).stdout)[0]
if row.get('Name')!='/'+name or row.get('State',{}).get('Running') is not True: raise SystemExit('latest GPoint API did not return healthy')
labels=row.get('Config',{}).get('Labels') or {}
if labels.get('org.opencontainers.image.revision')!=source: raise SystemExit('cleanup API source SHA changed')
env=dict(x.split('=',1) for x in row.get('Config',{}).get('Env',[]) if '=' in x)
if env.get('EROS_ENABLED')=='true' or env.get('EROS_PRODUCTION_TEST_MODE')=='true' or any(m.get('Destination')=='/opt/gpoint/env/eros-api.env' for m in row.get('Mounts') or []): raise SystemExit('standard API still contains pilot configuration')
image=json.loads(subprocess.run(['docker','image','inspect',row['Image']],check=True,text=True,capture_output=True).stdout)[0]
if digest not in (image.get('RepoDigests') or []): raise SystemExit('cleanup API digest changed')
networks=row.get('NetworkSettings',{}).get('Networks') or {}
print('EROS_CLEANUP_LATEST_GPOINT_API=PASS source_sha='+source)
REMOTE_CLEAN_API
  export GPOINT_KAMAL_WORKTREE="$pilot_worktree"
  GPOINT_COMMIT_SHA="$pilot_sha" GPOINT_APP_SOURCE_SHA="$pilot_sha" \
    GPOINT_KAMAL_SERVICE=gpoint-eros-pilot KAMAL_ROLES=engine \
    kamal app stop --roles engine --config-file config/deploy.eros.yml || true
  GPOINT_COMMIT_SHA="$pilot_sha" GPOINT_APP_SOURCE_SHA="$pilot_sha" \
    GPOINT_KAMAL_SERVICE=gpoint-eros-relay KAMAL_ROLES=relay \
    kamal app stop --roles relay --config-file config/deploy.eros-relay.yml || true
  unset GPOINT_KAMAL_WORKTREE
  export GPOINT_KAMAL_WORKTREE="$current_worktree"
  "${ssh[@]}" "$target" python3 - "$pilot_sha" "$current_version" "$current_source" "$current_digest" <<'REMOTE_DETACH'
import importlib.util,pathlib,sys
pilot_sha,version,source,digest=sys.argv[1:]
spec=importlib.util.spec_from_file_location('ops',pathlib.Path('/opt/gpoint/releases')/pilot_sha/'source/scripts/eros_native_pilot_ops.py')
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.detach_pilot_api_network(version,source,digest)
REMOTE_DETACH
  "${ssh[@]}" "$target" python3 - "$current_version" <<'REMOTE_CLEAN_NETWORK'
import json,subprocess,sys
name='gpoint-core-api-'+sys.argv[1]
row=json.loads(subprocess.run(['docker','inspect',name],check=True,text=True,capture_output=True).stdout)[0]
if 'gpoint_eros_embeddings' in (row.get('NetworkSettings',{}).get('Networks') or {}): raise SystemExit('standard API remains attached to Eros network')
print('EROS_CLEANUP_API_NETWORK=PASS')
REMOTE_CLEAN_NETWORK
  remove_exact_pilot_containers "$pilot_sha" "$pilot_version" "$eros_image" "$app_digest" \
    "$current_version" "$current_source" "$current_digest"
  clear_creator_genome "$EROS_PILOT_TEST_CREATOR_ID" "$genome_id"
  "${ssh[@]}" "$target" rm -f -- "/run/eros-persona-$pilot_sha.json"
  if "${ssh[@]}" "$target" test -f "$backup"; then
    run_ops restore "$pilot_sha" "$EROS_PILOT_TEST_USER_ID" "$EROS_PILOT_TEST_CREATOR_ID" --backup "$backup"
  fi
  if "${ssh[@]}" "$target" test -f /opt/gpoint/env/eros.env; then
    run_ops cleanup "$pilot_sha" "$EROS_PILOT_TEST_USER_ID" "$EROS_PILOT_TEST_CREATOR_ID"
  fi
  "${ssh[@]}" "$target" python3 "/opt/gpoint/releases/$pilot_sha/source/scripts/collect_kamal_eros_state.py" \
    --output "/run/eros-pilot-clean-state-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT.json"
  "${ssh[@]}" "$target" rm -f -- "/run/eros-pilot-clean-state-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT.json"
  "${ssh[@]}" "$target" bash -s -- "$EROS_PILOT_TEST_USER_ID" "$EROS_PILOT_TEST_CREATOR_ID" <<'REMOTE_METADATA'
set -euo pipefail
user="$1"; creator="$2"
[[ "$user" =~ ^[0-9a-f-]{36}$ && "$creator" =~ ^[0-9a-f-]{36}$ ]]
docker exec -i shared_postgres_1 sh -ec 'exec psql -Xq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint' <<SQL
BEGIN;
DELETE FROM "ErosAssistantMessageState" WHERE "userId"='$user' AND "creatorId"='$creator';
DELETE FROM "ErosRelationshipMapping" WHERE "userId"='$user' AND "creatorId"='$creator';
COMMIT;
SQL
REMOTE_METADATA
  "${ssh[@]}" "$target" rm -f -- "$marker"
  unset GPOINT_KAMAL_WORKTREE
  git -C "$GITHUB_WORKSPACE" worktree remove --force "$pilot_worktree"
  git -C "$GITHUB_WORKSPACE" worktree remove --force "$current_worktree"
  rmdir "$worktree_root"
  echo 'EROS_PILOT_ROLLBACK_AND_CLEANUP=PASS'
}

if [[ "$action" == cleanup ]]; then
  rollback_and_cleanup
  exit 0
fi

: "${IMAGE_VERSION:?candidate-scoped Kamal version is required}"
: "${EROS_PILOT_IMAGE:?immutable stock Eros image is required}"
: "${APP_DIGEST:?candidate GPoint image digest is required}"
: "${KAMAL_REGISTRY_PASSWORD:?GHCR read token is required}"
: "${GITHUB_WORKSPACE:?runner workspace is required}"
: "${RUNNER_TEMP:?runner temp directory is required}"
[[ "$IMAGE_VERSION" == "$CANDIDATE_SHA-"* ]]
[[ "$EROS_PILOT_IMAGE" =~ ^ghcr\.io/akhripunov/gpoint-eros-pilot@sha256:[0-9a-f]{64}$ ]]
[[ "$APP_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
test "$(git -C "$GITHUB_WORKSPACE" rev-parse HEAD)" = "$CANDIDATE_SHA"
test "$(git -C "$GITHUB_WORKSPACE" status --porcelain)" = ''

mapfile -t baseline < <(check_remote_baseline)
[[ "${#baseline[@]}" -eq 3 ]]
BASELINE_VERSION="${baseline[0]}" BASELINE_SOURCE_SHA="${baseline[1]}" BASELINE_CORE_DIGEST="${baseline[2]}"
[[ "$BASELINE_CORE_DIGEST" =~ ^ghcr\.io/akhripunov/gpoint-runtime@sha256:[0-9a-f]{64}$ ]]
stage_sources
"${ssh[@]}" "$target" python3 "/opt/gpoint/releases/$CANDIDATE_SHA/source/scripts/collect_kamal_eros_state.py" \
  --output "/run/eros-pilot-preflight-$CANDIDATE_SHA.json"
"${ssh[@]}" "$target" rm -f -- "/run/eros-pilot-preflight-$CANDIDATE_SHA.json"
"${ssh[@]}" "$target" python3 - "/opt/gpoint/releases/$CANDIDATE_SHA/source/scripts/eros_native_pilot_ops.py" <<'REMOTE_RESOURCES'
import importlib.util,sys
spec=importlib.util.spec_from_file_location('ops',sys.argv[1]); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
facts=module.check_resources()
print('EROS_VPS_RESOURCES=PASS mem_available_kib=%s free_disk_bytes=%s'%(facts['memAvailableKiB'],facts['freeDiskBytes']))
REMOTE_RESOURCES
remote_api="$(${ssh[@]} "$target" docker inspect --format '{{.Config.Image}}' gpoint-core-api-"$BASELINE_VERSION")"
"${ssh[@]}" "$target" bash -s -- "$remote_api" "$EROS_PILOT_TEST_USER_ID" "$EROS_PILOT_TEST_CREATOR_ID" <<'REMOTE_TEST_PAIR'
set -euo pipefail
image="$1"; user="$2"; creator="$3"
docker run --rm --network shared_internal --env-file /opt/gpoint/env/production.env --workdir /app/apps/api \
  --entrypoint node "$image" -e 'const {PrismaClient}=require("@prisma/client");const db=new PrismaClient();(async()=>{try{const uid=process.argv[1],cid=process.argv[2];const u=await db.user.findUnique({where:{id:uid},select:{id:true,telegramUsername:true}});const c=await db.creator.findUnique({where:{id:cid},include:{profile:true,owners:{select:{userId:true}}}});if(!u||!/^eros_test_[a-z0-9_]{2,32}$/i.test(u.telegramUsername||""))throw Error("synthetic user mismatch");if(!c||!/^eros-test-[a-z0-9-]{2,48}$/i.test(c.slug)||c.profile?.status!=="APPROVED"||c.profile.erosGenomeId!==null||!c.owners.some(x=>x.userId===uid))throw Error("synthetic creator mismatch");if(await db.erosRelationshipMapping.count({where:{userId:uid,creatorId:cid}})!==0)throw Error("prior Eros mapping exists");if(await db.erosAssistantMessageState.count({where:{userId:uid,creatorId:cid}})!==0)throw Error("prior Eros message state exists");console.log("EROS_TEST_PAIR_VALID=PASS")}catch(e){console.error("EROS_TEST_PAIR_VALID=BLOCKED");process.exitCode=1}finally{await db.$disconnect()}})()' "$user" "$creator"
REMOTE_TEST_PAIR

pilot_marker_present=0
rollback_on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ "$rc" -ne 0 && "$pilot_marker_present" -eq 1 ]]; then
    echo 'EROS_PILOT_APPLY=FAILED; starting exact-scope rollback' >&2
    rollback_and_cleanup || echo 'EROS_PILOT_AUTOMATIC_CLEANUP=FAILED; retain marker and backup for owner-reviewed recovery' >&2
  fi
  exit "$rc"
}
trap rollback_on_exit EXIT INT TERM
write_pilot_marker "$BASELINE_VERSION" "$BASELINE_SOURCE_SHA" "$BASELINE_CORE_DIGEST"
pilot_marker_present=1
"${ssh[@]}" "$target" python3 "/opt/gpoint/releases/$CANDIDATE_SHA/source/scripts/eros-production-secrets.py" \
  --source-sha "$CANDIDATE_SHA" --image "$EROS_PILOT_IMAGE" \
  --pilot-test-user-id "$EROS_PILOT_TEST_USER_ID" --pilot-test-creator-id "$EROS_PILOT_TEST_CREATOR_ID" \
  --policy-jurisdiction "$EROS_PILOT_POLICY_JURISDICTION" --policy-market "$EROS_PILOT_POLICY_MARKET" \
  --ensure-postgres-network --ensure-embeddings-network --provision-database
backup="$backup_root/eros-before-$CANDIDATE_SHA.dump"
run_ops backup "$CANDIDATE_SHA" "$EROS_PILOT_TEST_USER_ID" "$EROS_PILOT_TEST_CREATOR_ID" --backup "$backup"
run_ops restore-probe "$CANDIDATE_SHA" "$EROS_PILOT_TEST_USER_ID" "$EROS_PILOT_TEST_CREATOR_ID" \
  --backup "$backup" --probe-database "eros_restore_probe_${CANDIDATE_SHA:0:16}"
run_ops migrate "$CANDIDATE_SHA" "$EROS_PILOT_TEST_USER_ID" "$EROS_PILOT_TEST_CREATOR_ID" --image "$EROS_PILOT_IMAGE"
"${ssh[@]}" "$target" python3 - "/opt/gpoint/releases/$CANDIDATE_SHA/source/scripts/eros_native_pilot_ops.py" <<'REMOTE_SQLX'
import importlib.util,sys
spec=importlib.util.spec_from_file_location('ops',sys.argv[1]); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
count=module.verify_sqlx_migrations()
print('EROS_STOCK_SQLX_LEDGER=PASS applied_count=%s'%count)
REMOTE_SQLX
run_ops seed-persona "$CANDIDATE_SHA" "$EROS_PILOT_TEST_USER_ID" "$EROS_PILOT_TEST_CREATOR_ID" --image "$EROS_PILOT_IMAGE"
genome_id="$("${ssh[@]}" "$target" python3 - "$CANDIDATE_SHA" <<'REMOTE_GENOME_RESULT'
import json,pathlib,re,sys
sha=sys.argv[1]
if not re.fullmatch(r'[0-9a-f]{40}',sha): raise SystemExit('source SHA invalid')
p=pathlib.Path('/run')/f'eros-persona-{sha}.json'
if p.is_symlink() or not p.is_file() or p.stat().st_mode&0o077 or p.stat().st_uid!=0: raise SystemExit('persona seed result missing or unsafe')
x=json.loads(p.read_text())
if x.get('schema')!='gpoint-eros-persona-seed/v1' or x.get('sourceSha')!=sha or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',x.get('genomeId',''),re.I): raise SystemExit('persona seed result identity mismatch')
print(x['genomeId'])
REMOTE_GENOME_RESULT
)"
write_pilot_genome_marker "$genome_id"
set_creator_genome "$EROS_PILOT_TEST_CREATOR_ID" "$genome_id"
"${ssh[@]}" "$target" rm -f -- "/run/eros-persona-$CANDIDATE_SHA.json"

GPOINT_COMMIT_SHA="$CANDIDATE_SHA" GPOINT_APP_SOURCE_SHA="$CANDIDATE_SHA" \
  GPOINT_KAMAL_EXPECTED_DIGEST="$APP_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-eros-relay KAMAL_ROLES=relay \
  kamal deploy --skip-push --version "$IMAGE_VERSION" --roles relay --config-file config/deploy.eros-relay.yml
GPOINT_COMMIT_SHA="$CANDIDATE_SHA" GPOINT_APP_SOURCE_SHA="$CANDIDATE_SHA" \
  GPOINT_KAMAL_EXPECTED_DIGEST="${EROS_PILOT_IMAGE##*@}" GPOINT_KAMAL_SERVICE=gpoint-eros-pilot KAMAL_ROLES=engine \
  kamal deploy --skip-push --version "$IMAGE_VERSION" --roles engine --config-file config/deploy.eros.yml
GPOINT_COMMIT_SHA="$CANDIDATE_SHA" GPOINT_APP_SOURCE_SHA="$CANDIDATE_SHA" \
  GPOINT_KAMAL_EXPECTED_DIGEST="$APP_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-core KAMAL_ROLES=api \
  kamal deploy --skip-push --version "$IMAGE_VERSION" --roles api --config-file config/deploy.eros-api.yml

"${ssh[@]}" "$target" bash -s -- "$IMAGE_VERSION" "$CANDIDATE_SHA" "$APP_DIGEST" "$EROS_PILOT_IMAGE" \
  "$EROS_PILOT_TEST_USER_ID" "$EROS_PILOT_TEST_CREATOR_ID" <<'REMOTE_VERIFY'
set -euo pipefail
version="$1"; sha="$2"; app_digest="$3"; eros_image="$4"; user="$5"; creator="$6"
engine="gpoint-eros-pilot-engine-$version"; api="gpoint-core-api-$version"
test "$(docker inspect --format '{{.State.Running}}' "$engine")" = true
test "$(docker inspect --format '{{.State.Running}}' "$api")" = true
test "$(docker inspect --format '{{index .Config.Labels "com.gpoint.integration.revision"}}' "$engine")" = "$sha"
test "$(docker image inspect --format '{{index .Config.Labels "com.gpoint.eros.patch-set.sha256"}}' "$(docker inspect --format '{{.Image}}' "$engine")")" = none
python3 - "$engine" "$api" "$user" "$creator" <<'PY'
import json,subprocess,sys
engine,api,user,creator=sys.argv[1:]
def env(name):
    row=json.loads(subprocess.run(['docker','inspect',name],check=True,text=True,capture_output=True).stdout)[0]
    values=dict(x.split('=',1) for x in row['Config']['Env'] if '=' in x)
    return values,row
values,_=env(engine); api_values,api_row=env(api)
for key,expected in [('EROS_PRODUCTION_TEST_USER_IDS',user),('EROS_PRODUCTION_TEST_CREATOR_IDS',creator)]:
    if api_values.get(key)!=expected: raise SystemExit('runtime synthetic allowlist mismatch')
if api_values.get('EROS_ENABLED')!='true' or api_values.get('EROS_PRODUCTION_TEST_MODE')!='true' or api_values.get('EROS_DATA_MODE')!='synthetic': raise SystemExit('runtime synthetic mode mismatch')
engine_networks=json.loads(subprocess.run(['docker','inspect',engine,'--format','{{json .NetworkSettings.Networks}}'],check=True,text=True,capture_output=True).stdout)
if set(engine_networks)!={'gpoint_app','gpoint_eros_data'}: raise SystemExit('Eros engine network boundary mismatch')
networks=json.loads(subprocess.run(['docker','inspect',api,'--format','{{json .NetworkSettings.Networks}}'],check=True,text=True,capture_output=True).stdout)
if not {'gpoint_eros_embeddings','shared_internal','gpoint_app'}.issubset(networks) or 'gpoint_eros_data' in networks: raise SystemExit('GPoint API network boundary mismatch')
print('EROS_RUNTIME_ALLOWLIST=PASS exact_pair=true')
print('EROS_RUNTIME_NETWORKS=PASS database_private=true embeddings_internal=true')
PY
relay="gpoint-eros-relay-relay-$version"
test "$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$relay")" = host
docker image inspect "$(docker inspect --format '{{.Image}}' "$engine")" --format '{{json .RepoDigests}}' | grep -F "$eros_image" >/dev/null
docker image inspect "$(docker inspect --format '{{.Image}}' "$api")" --format '{{json .RepoDigests}}' | grep -F "ghcr.io/akhripunov/gpoint-runtime@$app_digest" >/dev/null
docker image inspect "$(docker inspect --format '{{.Image}}' "$relay")" --format '{{json .RepoDigests}}' | grep -F "ghcr.io/akhripunov/gpoint-runtime@$app_digest" >/dev/null
ss -ltn | grep -F '172.31.254.1:11434' >/dev/null
! ss -ltn | grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\[::\]):11434([[:space:]]|$)'
docker exec "$api" node -e "fetch('http://172.31.254.1:11434/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null
docker run --rm --network gpoint_eros_embeddings \
  --entrypoint node "ghcr.io/akhripunov/gpoint-runtime@$app_digest" -e \
  "fetch('http://172.31.254.1:11434/api/tags').then(async r=>{if(!r.ok)throw Error('relay model verification denied');const x=await r.json();if(!x.models?.some(m=>m.name==='qwen3-embedding:0.6b'&&m.digest==='ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d'))throw Error('locked Qwen model digest missing')}).catch(()=>process.exit(1))" >/dev/null
api_ip="$(docker inspect --format '{{with index .NetworkSettings.Networks "gpoint_eros_embeddings"}}{{.IPAddress}}{{end}}' "$api")"
python3 - "$api_ip" <<'PY'
import ipaddress,sys
address=ipaddress.ip_address(sys.argv[1])
network=ipaddress.ip_network('172.31.254.0/29')
if address not in network or address in (network.network_address,network.broadcast_address,ipaddress.ip_address('172.31.254.1')):
    raise SystemExit('API did not receive a usable embeddings-network address')
PY
echo "EROS_QWEN3_RELAY=PASS api_ip=$api_ip verifier_ip=dynamic digest=locked"
REMOTE_VERIFY

"${ssh[@]}" "$target" python3 - "/opt/gpoint/releases/$CANDIDATE_SHA/source/scripts/eros_native_pilot_ops.py" <<'REMOTE_FINAL_RESOURCES'
import importlib.util,sys
spec=importlib.util.spec_from_file_location('ops',sys.argv[1]); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
facts=module.check_resources()
print('EROS_VPS_POST_APPLY_RESOURCES=PASS mem_available_kib=%s free_disk_bytes=%s'%(facts['memAvailableKiB'],facts['freeDiskBytes']))
REMOTE_FINAL_RESOURCES
"${ssh[@]}" "$target" python3 - "$marker" <<'REMOTE_MARKER_READY'
import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); x=json.loads(p.read_text())
if x.get('schema')!='gpoint-eros-native-pilot-state/v1' or p.stat().st_mode&0o077: raise SystemExit('pilot marker identity changed')
x['status']='synthetic-pilot-ready'; t=p.with_name('.eros-pilot-state.tmp'); t.write_text(json.dumps(x,sort_keys=True,separators=(',',':'))+'\n'); t.chmod(0o600); os.replace(t,p)
REMOTE_MARKER_READY
trap - EXIT INT TERM
pilot_marker_present=0
echo 'SYNTHETIC_PILOT_APPLY=PASS paid_inference=not_invoked real_chat=not_invoked'
