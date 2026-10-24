import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const SHA = /^[0-9a-f]{40}$/;
const HASH = /^[0-9a-f]{64}$/;
function exact(sha) { if (!SHA.test(sha ?? '')) throw new Error('exact GSales candidate SHA required'); }
function python(source) { return `printf '%s' '${Buffer.from(source).toString('base64')}' | base64 -d | python3 -`; }
const ADMIN = `import json, subprocess, pathlib, os
from urllib.parse import urlsplit
from uuid import UUID
def query(sql):
    p=subprocess.run(['docker','exec','-i','shared_postgres_1','sh','-ec','exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint'],input="BEGIN READ ONLY; SET LOCAL statement_timeout='15s'; "+sql+"; ROLLBACK;",text=True,capture_output=True,timeout=30)
    if p.returncode: raise RuntimeError('GSales administrator inspection failed')
    return json.loads(p.stdout.strip())
def read_env(name):
    return dict(line.split('=',1) for line in pathlib.Path(name).read_text().splitlines() if line and not line.startswith('#') and '=' in line)
def state():
    exists=query("SELECT json_build_object('schema',to_regnamespace('gsales') IS NOT NULL,'ledger',to_regclass('gsales_meta.migrations') IS NOT NULL)")
    rows=query("SELECT coalesce(json_agg(json_build_object('version',version,'sha256',sha256)),'[]') FROM gsales_meta.migrations") if exists['ledger'] else []
    if len(rows)>1: raise RuntimeError('unexpected GSales migration inventory')
    if exists['schema'] != bool(rows): raise RuntimeError('GSales schema and migration ledger disagree')
    return {'migrationHead':rows[0]['version'] if rows else 'ABSENT','migrationSha256':rows[0]['sha256'] if rows else None,'schemaPresent':exists['schema']}
`;

export function gsalesMigrationIdentity(checkout, bundle) {
  const file = path.join(checkout, 'services/gsales/migrations/001_gsales.sql');
  const digest = crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  if (bundle.gsalesMigrationHead !== '001_gsales' || bundle.gsalesMigrationSha256 !== digest) throw new Error('GSales receipt migration differs from exact candidate source');
  return { migrationHead: '001_gsales', migrationSha256: digest };
}

export function gsalesReadinessCommand(candidate, baselineSha) {
  exact(candidate); exact(baselineSha);
  return python(ADMIN + `
e=read_env('/opt/gpoint/env/production.env')
m=read_env('/opt/gpoint/env/migration.env')
if e.get('RELEASE_SHA')!='${baselineSha}': raise RuntimeError('GSales production baseline changed')
if pathlib.Path('/opt/gpoint/env/migration.env').stat().st_mode & 0o077: raise RuntimeError('migration secrets permissions are unsafe')
for key in ('GSALES_DATABASE_URL','GSALES_TENANT_ID','GSALES_OWNER_API_TOKEN','GSALES_WORKER_TOKEN','GSALES_AGATA_DATABASE_URL','GSALES_AGATA_TENANT_ID','GSALES_AGATA_INTERNAL_API_TOKEN','GSALES_AGATA_MESSAGE_AEAD_KEY','GSALES_AGATA_REQUEST_FINGERPRINT_HMAC_KEY','GSALES_AGATA_RETRIEVAL_QUERY_HMAC_KEY','GSALES_AGATA_RESPONSE_FINGERPRINT_HMAC_KEY','GSALES_AGATA_SHADOW_CANDIDATE_HMAC_KEY'):
    if not e.get(key): raise RuntimeError('GSales config missing: '+key)
for key in ('GSALES_OUTREACH_ENABLED','GSALES_AUTO_REPLY_ENABLED','GSALES_MANAGEMENT_ENABLED','GSALES_TELEGRAM_LISTENER_ENABLED'):
    if e.get(key)!='false': raise RuntimeError('GSales sending must remain disabled: '+key)
if e.get('GSALES_PRODUCTION_MODE')!='true' or e.get('GSALES_AGATA_LLM_PROVIDER')!='mac_worker': raise RuntimeError('approved GSales Mac provider required')
if len({e['GSALES_WORKER_TOKEN'],e['GSALES_AGATA_INTERNAL_API_TOKEN'],e['GSALES_OWNER_API_TOKEN'],e.get('GPOINT_AGATA_WORKER_TOKEN','')})!=4: raise RuntimeError('GSales credentials must be distinct')
tenant=str(UUID(e['GSALES_TENANT_ID']))
if tenant!=e['GSALES_AGATA_TENANT_ID']: raise RuntimeError('GSales Core tenant mismatch')
for key,role in (('GSALES_DATABASE_URL','gsales_runtime'),('GSALES_AGATA_DATABASE_URL','gsales_agata_runtime')):
    u=urlsplit(e[key])
    if u.path!='/gpoint' or u.username!=role: raise RuntimeError('GSales runtime database scope mismatch')
for key in ('GSALES_MIGRATION_DATABASE_URL','AGATA_MIGRATOR_DATABASE_URL'):
    if not m.get(key) or urlsplit(m[key]).path!='/gpoint': raise RuntimeError('scoped privileged migrator missing')
    if e.get(key): raise RuntimeError('migration DSN must stay outside runtime env')
inventory=query("SELECT json_build_object('tenants',(SELECT coalesce(json_agg(id::text),'[]') FROM agata.tenants),'roles',(SELECT coalesce(json_agg(rolname),'[]') FROM pg_roles WHERE rolname IN ('gsales_runtime','gsales_agata_runtime') AND rolcanlogin AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole AND NOT rolcreatedb AND NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid)))")
if tenant not in inventory['tenants'] or set(inventory['roles'])!={'gsales_runtime','gsales_agata_runtime'}: raise RuntimeError('GSales tenant/role provisioning unproven')
bindings=json.loads(m.get('AGATA_ROLE_TENANT_BINDINGS',e.get('AGATA_ROLE_TENANT_BINDINGS','{}')))
if set(str(UUID(v)) for v in bindings.values())!=set(inventory['tenants']) or any(bindings.get(r)!=tenant for r in ('gsales_runtime','gsales_agata_runtime')): raise RuntimeError('GSales complete role binding inventory mismatch')
facts=state()
facts.update(readiness='PASS',candidateSha='${candidate}',baselineSha='${baselineSha}',tenantId=tenant,provider='mac_worker',sendingEnabled=False)
print('GSALES_READINESS='+json.dumps(facts,sort_keys=True))
`);
}

export function parseGsalesReadiness(output, candidate, baselineSha) {
  const line = output.split(/\r?\n/).find(line => line.startsWith('GSALES_READINESS='));
  let result; try { result = JSON.parse(line?.slice('GSALES_READINESS='.length)); } catch { throw new Error('GSales readiness evidence absent'); }
  if (result.readiness !== 'PASS' || result.candidateSha !== candidate || result.baselineSha !== baselineSha || result.provider !== 'mac_worker' || result.sendingEnabled !== false) throw new Error('GSales readiness evidence invalid');
  return result;
}

export function gsalesMigrationStateCommand(identity) {
  if (identity.migrationHead !== '001_gsales' || !HASH.test(identity.migrationSha256)) throw new Error('invalid GSales migration identity');
  return python(ADMIN + `
facts=state()
if facts['migrationHead']!='ABSENT' and (facts['migrationHead']!='001_gsales' or facts['migrationSha256']!='${identity.migrationSha256}'): raise RuntimeError('GSales migration checksum differs from candidate')
print('GSALES_PENDING='+('1' if facts['migrationHead']=='ABSENT' else '0'))
`);
}

export function gsalesMigrationCommand(sha, identity) {
  exact(sha); gsalesMigrationStateCommand(identity);
  return `set -e; docker run --rm --network shared_internal --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/migration.env gpoint-g-sales-runtime:${sha} python -m app.db.migrate --apply --candidate ${sha} --owner-approved-sha ${sha} >/dev/null 2>&1; ${python(ADMIN + `
s=state()
if s['migrationHead']!='001_gsales' or s['migrationSha256']!='${identity.migrationSha256}': raise RuntimeError('GSales post-migration checksum/head unproven')
print('GSALES_MIGRATION_VERIFIED='+json.dumps(s,sort_keys=True))
`)}`;
}

export function combinedComposeCommand(sha) {
  exact(sha);
  const base = `/opt/gpoint/releases/${sha}/source/infra/production`;
  // Validate effective configuration without emitting secrets. Preserve env_file
  // and interpolation in the durable combined compose instead of embedding DSNs.
  return `umask 077; EROS_TEST_USER_IDS= EROS_TEST_CREATOR_IDS= RELEASE_SHA=${sha} docker compose -p compose --profile agata --profile gsales --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/eros.env -f ${base}/docker-compose.yml -f ${base}/docker-compose.gsales.yml -f ${base}/docker-compose.eros.yml config --quiet; EROS_TEST_USER_IDS= EROS_TEST_CREATOR_IDS= RELEASE_SHA=${sha} docker compose -p compose --profile agata --profile gsales --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/eros.env -f ${base}/docker-compose.yml -f ${base}/docker-compose.gsales.yml -f ${base}/docker-compose.eros.yml config --no-interpolate --no-env-resolution > /tmp/gpoint-compose-${sha}.yml; mkdir -p /opt/gpoint/release-logs/${sha}; EROS_TEST_USER_IDS= EROS_TEST_CREATOR_IDS= RELEASE_SHA=${sha} docker compose -p compose --profile agata --profile gsales --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/eros.env -f /tmp/gpoint-compose-${sha}.yml config --format json > /opt/gpoint/release-logs/${sha}/effective-compose.json; chmod 600 /opt/gpoint/release-logs/${sha}/effective-compose.json /tmp/gpoint-compose-${sha}.yml; sha256sum /opt/gpoint/release-logs/${sha}/effective-compose.json | awk '{print "GPOINT_EFFECTIVE_CONFIG_SHA256=" $1}'`;
}

export function gsalesStartCommand(sha) {
  exact(sha);
  return `set -e; EROS_TEST_USER_IDS= EROS_TEST_CREATOR_IDS= RELEASE_SHA=${sha} docker compose -p compose --profile gsales --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/eros.env -f /tmp/gpoint-compose-${sha}.yml up -d --no-build gsales-api gsales-agata-api gsales-agata-memory-worker; ready=0; for attempt in $(seq 1 60); do ready=1; for container in compose-gsales-api-1 compose-gsales-agata-api-1 compose-gsales-agata-memory-worker-1; do test "$(docker inspect "$container" --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')" = 'true healthy' || ready=0; done; if [ "$ready" = 1 ]; then break; fi; sleep 2; done; test "$ready" = 1; echo GSALES_STARTED`;
}

export function gsalesRuntimeCommand(sha, identity, { attempts = 60 } = {}) {
  exact(sha); gsalesMigrationStateCommand(identity);
  if (![1, 60].includes(attempts)) throw new Error('invalid bounded inspection attempts');
  const inside = `import asyncio,json,httpx\nfrom app.config import Settings\nfrom app.db.session import Database\nfrom app.core import AgataClient\ns=Settings()\nasync def verify():\n    db=Database(s)\n    async with db.transaction() as conn: await db.assert_secure(conn)\n    await AgataClient(s).assert_secure(require_inference_ready=False)\n    async with httpx.AsyncClient() as c:\n        r=await c.get('http://127.0.0.1:8010/health/live'); r.raise_for_status()\n    assert s.production_mode and not any((s.outreach_enabled,s.auto_reply_enabled,s.management_enabled,s.telegram_listener_enabled))\n    try: operational=await asyncio.wait_for(AgataClient(s).request('GET','/internal/v1/sales/runtime-security'),timeout=2)\n    except Exception: operational={}\n    print(json.dumps(dict(ready=True,securityPassed=True,inferenceReady=operational.get('production_inference_ready') is True,tenantId=s.tenant_id,outreachEnabled=False,autoReplyEnabled=False,managementEnabled=False,listenerEnabled=False)))\nasyncio.run(verify())`;
  const core = `import json\nfrom app.config import get_settings\ns=get_settings()\nassert s.source_system=='gsales' and s.sales_production_mode and s.sales_drafts_enabled and s.llm_provider=='mac_worker' and s.codex_local_enabled\nprint(json.dumps(dict(provider=s.llm_provider,codexLocalEnabled=s.codex_local_enabled)))`;
  return python(ADMIN + `
import time
s=state()
if s['migrationHead']!='001_gsales' or s['migrationSha256']!='${identity.migrationSha256}': raise RuntimeError('GSales migration identity mismatch')
def docker(*args):
    p=subprocess.run(['docker',*args],text=True,capture_output=True,timeout=30)
    if p.returncode: raise RuntimeError('GSales runtime inspection failed')
    return p.stdout.strip()
facts={}
for attempt in range(${attempts}):
    p=subprocess.run(['docker','exec','compose-gsales-api-1','python','-c',${JSON.stringify(inside)}],text=True,capture_output=True,timeout=30)
    if p.returncode==0:
        facts=json.loads(p.stdout); break
    time.sleep(2)
else: raise RuntimeError('GSales DB/Core server readiness failed')
facts.update(json.loads(docker('exec','compose-gsales-agata-api-1','python','-c',${JSON.stringify(core)})))
facts.update(s)
facts.update(buildSha='${sha}',imageId=docker('image','inspect','gpoint-g-sales-runtime:${sha}','--format','{{.Id}}'),agataImageId=docker('image','inspect','gpoint-agata-runtime:${sha}','--format','{{.Id}}'))
for name,key,expected in (('gsales-api','apiImage',facts['imageId']),('gsales-agata-api','coreApiImage',facts['agataImageId']),('gsales-agata-memory-worker','coreWorkerImage',facts['agataImageId'])):
    item=json.loads(docker('inspect','compose-'+name+'-1'))[0]
    if item['Image']!=expected or not item['State']['Running'] or item['State'].get('Health',{}).get('Status')!='healthy': raise RuntimeError('GSales runtime artifact parity failed')
    revision=docker('image','inspect',expected,'--format','{{index .Config.Labels "org.opencontainers.image.revision"}}')
    if revision!='${sha}': raise RuntimeError('GSales runtime source identity failed')
    facts[key]=item['Image']
print('GSALES_RUNTIME='+json.dumps(facts,sort_keys=True))
`);
}

export function parseGsalesRuntime(output, sha, identity) {
  const line = output.split(/\r?\n/).find(line => line.startsWith('GSALES_RUNTIME='));
  let facts; try { facts = JSON.parse(line?.slice('GSALES_RUNTIME='.length)); } catch { throw new Error('GSales runtime evidence absent'); }
  if (facts.buildSha !== sha || facts.migrationHead !== identity.migrationHead || facts.migrationSha256 !== identity.migrationSha256 || facts.ready !== true || facts.securityPassed !== true || typeof facts.inferenceReady !== 'boolean' || facts.provider !== 'mac_worker' || facts.codexLocalEnabled !== true || !/^sha256:[0-9a-f]{64}$/.test(facts.imageId ?? '') || !/^sha256:[0-9a-f]{64}$/.test(facts.agataImageId ?? '') || facts.apiImage !== facts.imageId || facts.coreApiImage !== facts.agataImageId || facts.coreWorkerImage !== facts.agataImageId || ['outreachEnabled','autoReplyEnabled','managementEnabled','listenerEnabled'].some(key => facts[key] !== false)) throw new Error('GSales runtime proof failed');
  return facts;
}

export function gsalesProductionInspectionCommand() {
  return python(ADMIN + `
e=read_env('/opt/gpoint/env/production.env')
s=state()
print('LIVE_GSALES_HEAD='+s['migrationHead'])
print('LIVE_GSALES_MIGRATION_SHA256='+str(s['migrationSha256'] or 'ABSENT'))
print('LIVE_GSALES_SCHEMA_PRESENT='+str(s['schemaPresent']).lower())
print('LIVE_GSALES_TENANT_ID='+e.get('GSALES_TENANT_ID','ABSENT'))
for name in ('gsales-api','gsales-agata-api','gsales-agata-memory-worker'):
    p=subprocess.run(['docker','inspect','compose-'+name+'-1'],text=True,capture_output=True,timeout=15)
    if p.returncode: continue
    v=json.loads(p.stdout)[0]; prefix='LIVE_'+name.upper().replace('-','_')
    print(prefix+'_IMAGE_ID='+v['Image'])
    print(prefix+'_RUNNING='+str(v['State']['Running']).lower())
    print(prefix+'_HEALTH='+v['State'].get('Health',{}).get('Status','none'))
    p=subprocess.run(['docker','image','inspect',v['Image'],'--format','{{index .Config.Labels "org.opencontainers.image.revision"}}'],text=True,capture_output=True,timeout=15)
    if p.returncode: raise RuntimeError('GSales runtime revision missing')
    print(prefix+'_SHA='+p.stdout.strip())
`);
}
