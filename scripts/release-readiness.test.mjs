import test from 'node:test';
import assert from 'node:assert/strict';
import { observeDeploymentReadiness, dependencyRecheckState, collectWorkerDependencies } from './release-readiness.mjs';
import { checkAgataEffectiveConfig } from './release-gpoint.mjs';
const nowMs = Date.parse('2026-10-05T04:00:00Z');
const healthy = {enabled:true,queue_connectivity:'reachable',last_poll_at:'2026-10-05T03:59:59Z',last_heartbeat_at:'2026-10-05T03:59:58Z'};
const probe = () => ({GPOINT:healthy,GSALES:healthy});

test('verified server with healthy dependency is COMPLETE/READY without inferring an E2E reply', () => {
  const result=observeDeploymentReadiness('COMPLETE',probe,{nowMs});
  assert.equal(result.serverDeployStatus,'COMPLETE');
  assert.equal(result.workerOperationalStatus,'HEALTHY');
  assert.equal(result.overallServiceReadiness,'READY');
  assert.equal(result.inferenceAcceptance,'NOT_VERIFIED');
});
test('unavailable worker preserves COMPLETE and explicitly degrades inference readiness', () => {
  const result=observeDeploymentReadiness('COMPLETE',()=>({GPOINT:healthy,GSALES:{...healthy,queue_connectivity:'worker_target_unavailable'}}),{nowMs});
  assert.equal(result.serverDeployStatus,'COMPLETE');
  assert.equal(result.workerOperationalStatus,'UNAVAILABLE');
  assert.equal(result.overallServiceReadiness,'DEGRADED');
  assert.equal(result.workerDependencies.GSALES.status,'UNAVAILABLE');
});
test('postdeploy timeout cannot throw into the server failure or rollback path', () => {
  const result=observeDeploymentReadiness('COMPLETE',()=>{throw Object.assign(new Error('secret must not escape'),{code:'ETIMEDOUT'});},{nowMs});
  assert.equal(result.serverDeployStatus,'COMPLETE');
  assert.equal(result.workerOperationalStatus,'UNAVAILABLE');
  assert.equal(result.overallServiceReadiness,'DEGRADED');
  assert.ok(!JSON.stringify(result).includes('secret'));
});
test('unknown status or stale polling remains visible and never advertises service READY', () => {
  assert.equal(observeDeploymentReadiness('COMPLETE',()=>null,{nowMs}).workerOperationalStatus,'NOT_VERIFIED');
  const result=observeDeploymentReadiness('COMPLETE',()=>({GPOINT:healthy,GSALES:{...healthy,last_poll_at:'2026-10-05T03:00:00Z'}}),{nowMs});
  assert.equal(result.workerOperationalStatus,'DEGRADED');
  assert.equal(result.overallServiceReadiness,'DEGRADED');
});
test('later worker recovery updates only readiness, preserving release/ref/runtime/migration identity', () => {
  const current={currentProductionSha:'a'.repeat(40),lastReleaseStatus:'COMPLETE',lastReleaseId:'release-1',currentProductionRef:'production-ref',currentProductionDbHead:'migration-1',inferenceAcceptance:'OWNER_CONFIRMED',...observeDeploymentReadiness('COMPLETE',()=>null,{nowMs})};
  current.inferenceAcceptance='OWNER_CONFIRMED';
  const updated=dependencyRecheckState(current,'a'.repeat(40),observeDeploymentReadiness('COMPLETE',probe,{nowMs}));
  assert.equal(updated.overallServiceReadiness,'READY');
  for(const key of ['currentProductionSha','currentProductionRef','currentProductionDbHead','lastReleaseId','lastReleaseStatus','inferenceAcceptance']) assert.equal(updated[key],current[key]);
  assert.throws(()=>dependencyRecheckState(current,'b'.repeat(40),observeDeploymentReadiness('COMPLETE',probe,{nowMs})));
});
test('server failure/recovery never becomes COMPLETE or READY merely because worker is healthy', () => {
  for(const state of ['FAILED','RECOVERY_REQUIRED']) {
    const result=observeDeploymentReadiness(state,probe,{nowMs});
    assert.equal(result.serverDeployStatus,state);
    assert.equal(result.overallServiceReadiness,'DEGRADED');
    assert.throws(()=>dependencyRecheckState({currentProductionSha:'a'.repeat(40),lastReleaseStatus:state},'a'.repeat(40),result));
  }
});
test('server compatibility blocks invalid protocol/config but not temporary worker liveness', () => {
  const config={enabled:true,provider:'mac_worker',localEnabled:true,workerTokenPresent:true,agataApiHealthy:true};
  assert.deepEqual(checkAgataEffectiveConfig(config),[]);
  for(const invalid of [{provider:'unknown'},{localEnabled:false},{workerTokenPresent:false},{agataApiHealthy:false}]) assert.ok(checkAgataEffectiveConfig({...config,...invalid}).length>0);
});

// Exercise canonical CLI with disposable state and simulated external transports.
// A mistaken recheck -> apply/rollback dispatch or release identity rewrite breaks this test.
test('canonical dependency recheck persists unavailable -> healthy without server mutation', async t => {
  const fs=await import('node:fs'); const os=await import('node:os'); const path=await import('node:path');
  const {spawnSync}=await import('node:child_process');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'gpoint-dependency-cli-')); t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const candidate='a'.repeat(40); const image='sha256:'+'c'.repeat(64); const core='sha256:'+'d'.repeat(64);
  const runtime={buildSha:candidate,migrationHead:'001_gsales',migrationSha256:'b'.repeat(64),imageId:image,agataImageId:core,apiImage:image,coreApiImage:core,coreWorkerImage:core,ready:true,securityPassed:true,inferenceReady:true,provider:'mac_worker',codexLocalEnabled:true,outreachEnabled:false,autoReplyEnabled:false,managementEnabled:false,listenerEnabled:false};
  const stateFile=path.join(dir,'production.json'); const manifests=path.join(dir,'manifests');fs.mkdirSync(manifests);
  const initial={currentProductionSha:candidate,currentProductionRef:'keep-this-ref',lastReleaseId:'server-original',lastReleaseStatus:'COMPLETE',currentProductionDbHead:'keep-db-head',gsalesDbHead:'001_gsales',gsalesMigrationSha256:'b'.repeat(64),inferenceAcceptance:'OWNER_SMOKE_PENDING'};
  fs.writeFileSync(stateFile,JSON.stringify(initial));
  const runner=path.join(dir,'readonly-remote.sh'); const commandLog=path.join(dir,'remote-commands');
  fs.writeFileSync(runner,`#!/bin/sh\nprintf '%s\\n' "$REMOTE_CMD" >> "$DEPENDENCY_COMMAND_LOG"\nprintf '%s\\n' 'GSALES_RUNTIME=${JSON.stringify(runtime)}'\n`,{mode:0o700});
  const support=path.join(dir,'Downloads/Codex/Workers/AGATA Workers');
  const runtimePath=path.join(support,'runtime/current');
  fs.mkdirSync(runtimePath,{recursive:true});fs.mkdirSync(path.join(support,'status'),{recursive:true});
  const workerScript=path.join(runtimePath,'run_codex_worker.sh');
  for(const available of [false,true]) {
    const stamp=new Date().toISOString();const worker={enabled:true,queue_connectivity:available?'reachable':'worker_target_unavailable',last_poll_at:stamp,last_heartbeat_at:stamp};
    fs.writeFileSync(workerScript,`#!/bin/sh\nprintf '%s\\n' '${JSON.stringify(worker)}'\n`,{mode:0o700});
    fs.writeFileSync(path.join(support,'status/worker-status.json'),JSON.stringify({targets:{GSALES:{state:available?'idle':'unavailable',last_poll_at:stamp,last_heartbeat_at:stamp}}}),{mode:0o600});
    const result=spawnSync(process.execPath,[new URL('./release-gpoint.mjs',import.meta.url).pathname,'--recheck-worker','--candidate',candidate],{encoding:'utf8',env:{...process.env,HOME:dir,GPOINT_RELEASE_STATE_FILE:stateFile,GPOINT_RELEASE_MANIFEST_DIR:manifests,GPOINT_REMOTE_RUNNER:runner,DEPENDENCY_COMMAND_LOG:commandLog},timeout:15000});
    assert.equal(result.status,0,result.stderr);
    const updated=JSON.parse(fs.readFileSync(stateFile));
    assert.equal(updated.serverDeployStatus,'COMPLETE');
    assert.equal(updated.overallServiceReadiness,available?'READY':'DEGRADED');
    for(const key of Object.keys(initial)) assert.deepEqual(updated[key],initial[key]);
    const event=JSON.parse(result.stdout);assert.equal(event.eventType,'DEPENDENCY_RECHECK');
    assert.equal(event.runtimeSwitchOccurred,false); assert.equal(event.databaseApplyOccurred,false);
  }
  const commands=fs.readFileSync(commandLog,'utf8');
  assert.doesNotMatch(commands,/docker compose|--apply|rollback|restart|migrate deploy/);
  assert.equal(fs.readdirSync(manifests).length,2);
});

test('GPOINT timeout preserves independent healthy GSALES evidence', () => {
  const values=collectWorkerDependencies({GPOINT:()=>{throw Object.assign(new Error('private details'),{code:'ETIMEDOUT'});},GSALES:()=>healthy});
  const result=observeDeploymentReadiness('COMPLETE',()=>values,{nowMs});
  assert.equal(result.serverDeployStatus,'COMPLETE');
  assert.equal(result.workerDependencies.GPOINT.status,'UNAVAILABLE');
  assert.equal(result.workerDependencies.GSALES.status,'HEALTHY');
  assert.equal(result.overallServiceReadiness,'DEGRADED');
  assert.equal(JSON.stringify(result).includes('private details'),false);
});
