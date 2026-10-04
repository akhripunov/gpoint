import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { gsalesReadinessCommand, gsalesMigrationStateCommand, gsalesMigrationCommand, combinedComposeCommand, gsalesRuntimeCommand, parseGsalesRuntime, gsalesProductionInspectionCommand } from './release-gsales.mjs';

const sha='a'.repeat(40);
const identity={migrationHead:'001_gsales',migrationSha256:'b'.repeat(64)};
function compileEmbedded(command) {
  const matches=[...command.matchAll(/printf '%s' '([A-Za-z0-9+/=]+)' \| base64 -d \| python3 -/g)];
  assert.ok(matches.length);
  for (const match of matches) {
    const source=Buffer.from(match[1],'base64').toString();
    const result=spawnSync('python3',['-c',"import sys; compile(sys.stdin.read(), '<remote>', 'exec')"],{input:source,encoding:'utf8'});
    assert.equal(result.status,0,result.stderr);
  }
}

test('all embedded scripts compile without running production commands',()=>{
  for(const command of [gsalesReadinessCommand(sha,'c'.repeat(40)),gsalesMigrationStateCommand(identity),gsalesMigrationCommand(sha,identity),gsalesRuntimeCommand(sha,identity),gsalesProductionInspectionCommand()]) compileEmbedded(command);
});
test('GS migrator consumes separate privileged env and exact immutable SHA',()=>{
  const command=gsalesMigrationCommand(sha,identity);
  assert.match(command,/--env-file \/opt\/gpoint\/env\/production.env --env-file \/opt\/gpoint\/env\/migration.env/);
  assert.match(command,new RegExp(`gpoint-g-sales-runtime:${sha} python -m app.db.migrate --apply --candidate ${sha} --owner-approved-sha ${sha}`));
  assert.doesNotMatch(command,/docker build|latest/);
  assert.throws(()=>gsalesMigrationCommand('HEAD',identity));
});
test('combined compose preserves runtime env references and saves protected effective configuration',()=>{
  const command=combinedComposeCommand(sha);
  assert.match(command,/docker-compose.gsales.yml/);
  assert.match(command,/--no-interpolate --no-env-resolution/);
  assert.match(command,/umask 077/);
  assert.match(command,/effective-compose.json/);
  assert.doesNotMatch(command,/migration.env/);
});
test('runtime proof enforces all lane images, actual readiness, checksums and sending OFF',()=>{
  const facts={buildSha:sha,migrationHead:identity.migrationHead,migrationSha256:identity.migrationSha256,imageId:'sha256:'+'c'.repeat(64),agataImageId:'sha256:'+'d'.repeat(64),ready:true,securityPassed:true,inferenceReady:true,provider:'mac_worker',codexLocalEnabled:true,outreachEnabled:false,autoReplyEnabled:false,managementEnabled:false,listenerEnabled:false};
  facts.apiImage=facts.imageId;facts.coreApiImage=facts.agataImageId;facts.coreWorkerImage=facts.agataImageId;
  const parse=(value)=>parseGsalesRuntime('GSALES_RUNTIME='+JSON.stringify(value),sha,identity);
  assert.deepEqual(parse(facts),facts);
  for(const change of [{coreWorkerImage:facts.imageId},{inferenceReady:false},{managementEnabled:true},{migrationSha256:'e'.repeat(64)},{buildSha:'f'.repeat(40)}]) assert.throws(()=>parse({...facts,...change}));
});

test('complete backup evidence includes GS checksum ledger with canonical schema order', async()=>{
  const {parseGuardedMigrationEvidence}=await import('./release-gpoint-vps-adapter.mjs');
  const backup={status:'COMPLETE_PROTECTED',database:'gpoint',candidate_sha:sha,schemas:['public','agata','gsales','gsales_meta','agata_identity'],extensions:['vector','pgcrypto'],sha256:'b'.repeat(64),path:'/opt/gpoint/backups/pre-release.dump'};
  assert.deepEqual(parseGuardedMigrationEvidence('GPOINT_COMPLETE_BACKUP='+JSON.stringify(backup),sha).backup,backup);
  assert.throws(()=>parseGuardedMigrationEvidence('GPOINT_COMPLETE_BACKUP='+JSON.stringify({...backup,schemas:[...backup.schemas,'maxibot']}),sha));
  assert.throws(()=>parseGuardedMigrationEvidence('GPOINT_COMPLETE_BACKUP='+JSON.stringify({...backup,extensions:['vector;bad']}),sha));
  assert.throws(()=>parseGuardedMigrationEvidence('GPOINT_COMPLETE_BACKUP='+JSON.stringify({...backup,extensions:['vector','vector']}),sha));
});

test('Docker Compose can persist a merged lane without embedding runtime credentials', async()=>{
  const fs=await import('node:fs'); const os=await import('node:os'); const path=await import('node:path');
  const scratch=fs.mkdtempSync(path.join(os.tmpdir(),'gsales-compose-contract-'));
  try {
    const envFile=path.join(scratch,'production.env'); fs.writeFileSync(envFile,'');
    const files=['docker-compose.yml','docker-compose.gsales.yml'].map(name=>{
      const target=path.join(scratch,name);
      fs.writeFileSync(target,fs.readFileSync(new URL('../infra/production/'+name,import.meta.url),'utf8').replaceAll('/opt/gpoint/env/production.env',envFile));
      return target;
    });
    const args=['compose','--env-file',envFile,'--profile','agata','--profile','gsales',...files.flatMap(file=>['-f',file]),'config','--no-interpolate','--no-env-resolution','--format','json'];
    const result=spawnSync('docker',args,{encoding:'utf8',env:{...process.env,RELEASE_SHA:sha}});
    assert.equal(result.status,0,result.stderr);
    const config=JSON.parse(result.stdout);
    assert.equal(config.services['gsales-api'].image,'gpoint-g-sales-runtime:${RELEASE_SHA}');
    assert.equal(config.services['gsales-agata-api'].environment.AGATA_CODEX_LOCAL_ENABLED,'true');
    assert.equal(config.services['gsales-agata-api'].environment.AGATA_LLM_API_KEY,'');
    assert.equal(config.services['gsales-api'].environment.GSALES_OUTREACH_ENABLED,'false');
    assert.equal(config.services['gsales-api'].environment.GSALES_TELEGRAM_LISTENER_ENABLED,'false');
    assert.ok(JSON.stringify(config.services['gsales-api'].env_file).includes(envFile));
    assert.equal(config.services['gsales-api'].ports[0].host_ip,'127.0.0.1');
  } finally {fs.rmSync(scratch,{recursive:true,force:true});}
});
