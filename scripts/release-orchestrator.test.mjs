import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {dispatchPrepare, findTaskAuthorization, validateEvent, OrchestrationError, taskAutoClaimRef} from './release-orchestrator.mjs';
import {ownerTaskCommitmentSha256, candidateScopeDiff, validateTaskAuthorization} from './release-authorization.mjs';

const sha = 'a'.repeat(40);
const event = (change = {}) => ({repository:{full_name:'akhripunov/gpoint'}, workflow_run:{id:123,run_attempt:2,
  head_sha:sha,head_branch:'main',status:'completed',conclusion:'success',event:'push',path:'.github/workflows/gpoint-prepare.yml',...change}});

function apiFixture() {
  const calls=[]; let claimed=false;
  const api=async(method,path,body)=>{
    calls.push({method,path,body});
    if(path==='git/ref/heads/main') return {object:{sha}};
    if(path==='actions/runs/123') return event().workflow_run;
    if(path==='actions/runs/123/attempts/2') return {run_attempt:2,run_started_at:'2026-10-14T04:51:39Z'};
    if(path==='actions/runs/123/attempts/1') return {run_attempt:1,run_started_at:'2026-10-14T04:38:31Z'};
    if(path==='actions/runs/123/attempts/2/jobs?per_page=100') return {total_count:1,jobs:[{name:'prepare',status:'completed',conclusion:'success',head_sha:sha,started_at:'2026-10-14T04:51:40Z',completed_at:'2026-10-14T04:52:20Z'}]};
    if(path==='actions/runs/123/attempts/1/jobs?per_page=100') return {total_count:1,jobs:[{name:'prepare',status:'completed',conclusion:'success',head_sha:sha,started_at:'2026-10-14T04:38:34Z',completed_at:'2026-10-14T04:46:48Z'}]};
    if(path.startsWith('releases/tags/')) return {tag_name:path.split('/').at(-1),draft:false,immutable:true,assets:[{id:456,name:'bundle.zip',state:'uploaded',size:100,digest:`sha256:${'b'.repeat(64)}`}]};
    if(path.startsWith('git/ref/tags/gpoint-prepare-')) return {object:{type:'commit',sha}};
    if(path.includes('actions/workflows/gpoint-deploy.yml/runs')) return {workflow_runs:[]};
    if(path.startsWith('git/ref/tags/gpoint-task-auth/')) { if(claimed)return {object:{type:'commit',sha}}; const e=new OrchestrationError('missing');e.status=404;throw e; }
    if(path==='git/refs') {if(claimed){const e=new OrchestrationError('duplicate');e.status=422;throw e;} claimed=true;return {};}
    if(path.endsWith('/dispatches')) return {};
    throw new Error(`Unexpected ${method} ${path}`);
  };
  return {api,calls};
}

test('trusted success event is exact',()=>assert.equal(validateEvent(event()).id,123));
test('failed, stale or untrusted Prepare never reaches the API',async()=>{
  for(const change of [{conclusion:'failure'},{status:'in_progress'},{head_branch:'feature'},{head_sha:'c'.repeat(40)},{path:'.github/workflows/other.yml'}]) {
    const {api,calls}=apiFixture();
    await assert.rejects(dispatchPrepare(event(change),api),OrchestrationError);
    assert.equal(calls.some(item=>item.path.endsWith('/dispatches')),false);
  }
});
test('Prepare without durable full-cycle authorization leaves exact-SHA path and does not dispatch',async()=>{
  const {api,calls}=apiFixture();
  const result=await dispatchPrepare(event(),api,'987');
  assert.equal(result.state,'OWNER_EXACT_SHA_APPROVAL_REQUIRED');
  assert.equal(calls.some(item=>item.path==='git/refs'),false);
  assert.equal(calls.some(item=>item.path.endsWith('/dispatches')),false);
});
test('ambiguous authorization never dispatches production',async()=>{
  const {api,calls}=apiFixture();
  const result=await dispatchPrepare(event(),api,'987',process.cwd(),async()=>({authorizationId:'',ambiguous:true}));
  assert.equal(result.state,'TASK_AUTHORIZATION_AMBIGUOUS');
  assert.equal(calls.some(item=>item.path==='git/refs'),false);
  assert.equal(calls.some(item=>item.path.endsWith('/dispatches')),false);
});
test('Prepare with one full-cycle authorization claims once and dispatches the existing Deploy workflow',async()=>{
  const {api,calls}=apiFixture(); const authorizationId='gpoint-task-2026-10-16-001';
  const result=await dispatchPrepare(event(),api,'987',process.cwd(),async()=>({authorizationId,ambiguous:false}));
  assert.equal(result.state,'TASK_SCOPED_AUTO_DISPATCHED');
  assert.equal(taskAutoClaimRef(authorizationId),`refs/tags/gpoint-task-auth/${authorizationId}/claimed`);
  const dispatch=calls.find(item=>item.path.endsWith('/dispatches'));
  assert.deepEqual(dispatch.body,{ref:'main',inputs:{candidate:sha,mode:'task-scoped-auto',approve_production:'false',
    prepare_run_id:'123',prepare_run_attempt:'2',orchestrator_run_id:'987',task_authorization_id:authorizationId}});
});
test('duplicate Prepare events cannot dispatch twice under one authorization',async()=>{
  const {api,calls}=apiFixture(); const authorizationId='gpoint-task-2026-10-16-001';
  const lookup=async()=>({authorizationId,ambiguous:false});
  const first=await dispatchPrepare(event(),api,'987',process.cwd(),lookup);
  const second=await dispatchPrepare(event(),api,'988',process.cwd(),lookup);
  assert.equal(first.state,'TASK_SCOPED_AUTO_DISPATCHED');
  assert.equal(second.state,'TASK_SCOPED_AUTO_ALREADY_CLAIMED');
  assert.equal(calls.filter(item=>item.path.endsWith('/dispatches')).length,1);
});
test('an existing authorization claim for a different SHA blocks instead of counting as a duplicate',async()=>{
  const {api}=apiFixture(); const authorizationId='gpoint-task-2026-10-16-001'; let claimed=true;
  const wrapped=async(method,route,body)=>{
    if(method==='POST'&&route==='git/refs'){const error=new OrchestrationError('already claimed');error.status=422;throw error;}
    if(method==='GET'&&route.endsWith('/claimed'))return {object:{type:'commit',sha:'c'.repeat(40)}};
    return api(method,route,body);
  };
  await assert.rejects(dispatchPrepare(event(),wrapped,'987',process.cwd(),async()=>({authorizationId,ambiguous:false})),OrchestrationError);
});
test('publish-only rerun uses earlier real Prepare job and exact immutable bundle',async()=>{
  const {api,calls}=apiFixture();
  const wrapped=async(method,path,body)=>{
    if(path==='releases/tags/gpoint-prepare-'+sha+'-123-2'){
      const e=new OrchestrationError('missing');e.status=404;throw e;
    }
    if(path==='actions/runs/123/attempts/2/jobs?per_page=100')
      return {total_count:1,jobs:[{name:'prepare',status:'completed',conclusion:'success',head_sha:sha,
        started_at:'2026-10-14T04:38:34Z',completed_at:'2026-10-14T04:46:48Z'}]};
    return api(method,path,body);
  };
  const result=await dispatchPrepare(event(),wrapped,'987');
  assert.equal(result.prepareRunAttempt,'1');
  assert.equal(result.tag,`gpoint-prepare-${sha}-123-1`);
  assert.equal(calls.some(x=>x.path.endsWith('/dispatches')),false);
});
test('new Prepare execution without immutable evidence blocks preflight',async()=>{
  const {api}=apiFixture();
  const wrapped=async(method,path,body)=>{
    if(path==='releases/tags/gpoint-prepare-'+sha+'-123-2'){
      const e=new OrchestrationError('missing');e.status=404;throw e;
    }
    return api(method,path,body);
  };
  await assert.rejects(dispatchPrepare(event(),wrapped),/Executed Prepare has no immutable bundle/);
});
test('immutable Prepare release accepts the exact bundle plus archive-fallback asset set',async()=>{
  const {api}=apiFixture();
  const wrapped=async(method,path,body)=>{
    const result=await api(method,path,body);
    if(path.startsWith('releases/tags/')) result.assets.push({id:457,name:'fallback-images.zip',state:'uploaded',
      size:200,digest:`sha256:${'c'.repeat(64)}`});
    return result;
  };
  const result=await dispatchPrepare(event(),wrapped);
  assert.equal(result.state,'OWNER_EXACT_SHA_APPROVAL_REQUIRED');
});
test('immutable Prepare release rejects missing, duplicate, invalid or unexpected assets',async()=>{
  const bundle={id:456,name:'bundle.zip',state:'uploaded',size:100,digest:`sha256:${'b'.repeat(64)}`};
  const fallback={id:457,name:'fallback-images.zip',state:'uploaded',size:200,digest:`sha256:${'c'.repeat(64)}`};
  const invalidSets=[
    [fallback],
    [bundle,bundle,fallback],
    [bundle,fallback,fallback],
    [bundle,{...fallback,name:'unexpected.zip'}],
    [bundle,{...fallback,digest:'sha256:invalid'}],
  ];
  for (const assets of invalidSets) {
    const {api}=apiFixture();
    const wrapped=async(method,path,body)=>{
      const result=await api(method,path,body);
      if(path.startsWith('releases/tags/')) result.assets=assets;
      return result;
    };
    await assert.rejects(dispatchPrepare(event(),wrapped),OrchestrationError);
  }
});
test('mutable or draft releases fail closed',async()=>{
  for(const patch of [{immutable:false},{draft:true}]) {
    const {api}=apiFixture(); const original=api;
    const wrapped=async(method,path,body)=>{const result=await original(method,path,body);if(path.startsWith('releases/tags/'))Object.assign(result,patch);return result;};
    await assert.rejects(dispatchPrepare(event(),wrapped),/immutable and published/);
  }
});

test('Prepare controller carries only one valid full-cycle authorization record',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'gpoint-task-auto-record-'));
  try {
    const git=(...args)=>execFileSync('git',args,{cwd:root,encoding:'utf8'}).trim();
    git('init','-q');git('config','user.name','Release test');git('config','user.email','release-test@example.invalid');
    fs.writeFileSync(path.join(root,'README.md'),'base\n');
    const records=path.join(root,'.release-authorizations');fs.mkdirSync(records);
    fs.writeFileSync(path.join(records,'gpoint-task-2026-10-16-002.json'),JSON.stringify({schema_version:3,
      owner_instruction_scope:'FULL_PRODUCTION_CYCLE',project:'gpoint',authorization_mode:'TASK_SCOPED_AUTO',
      authorization_id:'gpoint-task-2026-10-16-002',source_instruction_identifier:'OWNER_TASK_OLD_RELEASE_2026_10_01',
      candidate_selection_rule:'EXACT_CURRENT_MAIN_WITHIN_AUTHORIZED_SCOPE',
      allowed_operations:['integrate','coalesce','prepare','preflight','deploy','verify','rollback','publish_evidence'],
      production_baseline_sha:'0'.repeat(40),task:'older task',allowed_paths:['apps/miniapp/src/label.ts'],
      acceptance_criteria:['label visible'],exclusions:['database','migrations','security','secrets','payments','ads','paid_traffic','release_infrastructure'],
      state:'ACTIVE',revoked:false,consumed:false}));
    git('add','README.md','.release-authorizations');git('commit','-m','baseline');
    const baseline=git('rev-parse','HEAD');
    const selected={schema_version:3,
      owner_instruction_scope:'FULL_PRODUCTION_CYCLE',project:'gpoint',environment:'production',authorization_mode:'TASK_SCOPED_AUTO',
      authorization_id:'gpoint-task-2026-10-16-001',source_instruction_identifier:'OWNER_TASK_UNIFIED_RELEASE_V1_2026_10_16',
      owner_task_reference:'OWNER task: release the exact scoped GPoint change after required gates.',
      candidate_selection_rule:'EXACT_CURRENT_MAIN_WITHIN_AUTHORIZED_SCOPE',
      allowed_operations:['integrate','coalesce','prepare','preflight','deploy','verify','rollback','publish_evidence'],
      production_baseline_sha:baseline,task:'update one safe UI label',allowed_paths:['apps/miniapp/src/label.ts'],
      acceptance_criteria:['label visible'],exclusions:['database','migrations','security','secrets','payments','ads','paid_traffic','release_infrastructure'],
      state:'ACTIVE',revoked:false,consumed:false};
    selected.owner_task_commitment_sha256=ownerTaskCommitmentSha256(selected);
    fs.writeFileSync(path.join(records,'gpoint-task-2026-10-16-001.json'),JSON.stringify(selected));
    git('add','.release-authorizations');git('commit','-m','register full-cycle authorization');
    fs.mkdirSync(path.join(root,'apps/miniapp/src'),{recursive:true});
    fs.writeFileSync(path.join(root,'apps/miniapp/src/label.ts'),'export const label="hello";\n');
    git('add','apps/miniapp/src/label.ts');git('commit','-m','implement authorized scope');
    const candidate=git('rev-parse','HEAD');
    const changedPaths=candidateScopeDiff(root,selected.authorization_id,baseline,candidate);
    validateTaskAuthorization({record:selected,project:'gpoint',authorizationId:selected.authorization_id,
      candidateSha:candidate,currentMainSha:candidate,productionSha:baseline,changedPaths});
    const apiCalls=[];
    const api=async(method,route)=>{apiCalls.push(route);if(route.includes('gpoint-task-2026-10-16-002/consumed'))return {object:{sha:candidate}};if(route.includes('/revoked')||route.includes('/consumed')||route.includes('/claimed')){const error=new Error('not found');error.status=404;throw error;}return {object:{sha:candidate}};};
    const selectedAuth=await findTaskAuthorization(root,candidate,api);
    assert.equal(selectedAuth.authorizationId,'gpoint-task-2026-10-16-001');
    assert.equal(selectedAuth.ambiguous,false);
    assert.ok(apiCalls.every(route => !route.toLowerCase().includes('linear')));
    const selectedAgain=await findTaskAuthorization(root,candidate,async(method,route)=>{
      if(route.includes('/revoked')||route.includes('/consumed')||route.includes('/claimed')){const error=new Error('not found');error.status=404;throw error;}
      return {object:{sha:candidate}};
    });
    assert.deepEqual(selectedAgain,{authorizationId:'gpoint-task-2026-10-16-001',ambiguous:false});
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});
