import test from 'node:test';
import assert from 'node:assert/strict';
import {dispatchPrepare, validateEvent, OrchestrationError, claimRef} from './release-orchestrator.mjs';
import {evaluateScope} from './release-scope-gate.mjs';

const sha = 'a'.repeat(40);
const event = (change = {}) => ({repository:{full_name:'akhripunov/gpoint'}, workflow_run:{id:123,run_attempt:2,
  head_sha:sha,head_branch:'main',status:'completed',conclusion:'success',event:'push',path:'.github/workflows/gpoint-prepare.yml',...change}});

function apiFixture() {
  const calls=[]; let claimed=false;
  const api=async(method,path,body)=>{
    calls.push({method,path,body});
    if(path==='git/ref/heads/main') return {object:{sha}};
    if(path==='actions/runs/123') return event().workflow_run;
    if(path.startsWith('releases/tags/')) return {tag_name:path.split('/').at(-1),draft:false,immutable:true,assets:[{id:456,name:'bundle.zip',state:'uploaded',size:100,digest:`sha256:${'b'.repeat(64)}`}]};
    if(path.startsWith('git/ref/tags/gpoint-prepare-')) return {object:{type:'commit',sha}};
    if(path.includes('actions/workflows/gpoint-deploy.yml/runs')) return {workflow_runs:[]};
    if(path.startsWith('git/ref/tags/gpoint-orchestrator/')) { if(claimed)return {object:{type:'commit',sha}}; const e=new OrchestrationError('missing');e.status=404;throw e; }
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
test('one successful Prepare atomically claims and dispatches exact preflight only',async()=>{
  const {api,calls}=apiFixture();
  const result=await dispatchPrepare(event(),api,'987');
  assert.equal(result.state,'PREFLIGHT_DISPATCHED');
  assert.equal(claimRef(event().workflow_run),`refs/tags/gpoint-orchestrator/preflight-${sha}-123-2`);
  const dispatch=calls.find(item=>item.path.endsWith('/dispatches'));
  assert.deepEqual(dispatch.body,{ref:'main',inputs:{candidate:sha,mode:'preflight',approve_production:'false',prepare_run_id:'123',prepare_run_attempt:'2',orchestrator_run_id:'987'}});
});
test('accepted but timed-out preflight dispatch is read back without another POST',async()=>{
  const {api,calls}=apiFixture();
  const wrapped=async(method,path,body)=>{
    if(method==='POST' && path.endsWith('/dispatches')) {
      calls.push({method,path,body});
      calls.push({method:'GET',path:'actions/workflows/gpoint-deploy.yml/runs?branch=main&head_sha='+sha,
        result:{workflow_runs:[{id:789,run_attempt:1,head_sha:sha,head_branch:'main',status:'queued',event:'workflow_dispatch',
          path:'.github/workflows/gpoint-deploy.yml',display_title:`GPoint preflight ${sha} prepare/123-2 controller/987 by owner`}]}});
      throw Object.assign(new OrchestrationError('accepted response lost'),{status:503});
    }
    const found=calls.find(item=>item.method==='GET' && item.path.includes('head_sha='+sha));
    if(method==='GET' && path.startsWith('actions/workflows/gpoint-deploy.yml/runs?branch=main&head_sha=')) {
      calls.push({method,path}); return found.result;
    }
    return api(method,path,body);
  };
  const result=await dispatchPrepare(event(),wrapped,'987',()=>{});
  assert.equal(result.state,'PREFLIGHT_DISPATCH_RECONCILED');
  assert.equal(result.deployRunId,'789');
  assert.equal(calls.filter(item=>item.method==='POST' && item.path.endsWith('/dispatches')).length,1);
});
test('duplicate event is a no-op after atomic claim',async()=>{
  const {api,calls}=apiFixture();
  await dispatchPrepare(event(),api);
  const duplicate=await dispatchPrepare(event(),api);
  assert.equal(duplicate.state,'PREFLIGHT_ALREADY_CLAIMED');
  assert.equal(calls.filter(item=>item.path.endsWith('/dispatches')).length,1);
});
test('mutable or draft releases fail closed',async()=>{
  for(const patch of [{immutable:false},{draft:true}]) {
    const {api}=apiFixture(); const original=api;
    const wrapped=async(method,path,body)=>{const result=await original(method,path,body);if(path.startsWith('releases/tags/'))Object.assign(result,patch);return result;};
    await assert.rejects(dispatchPrepare(event(),wrapped),/immutable and published/);
  }
});
test('TASK_SCOPED_AUTO allows only recorded bounded low-risk scope',()=>{
  const base={baselineSha:'a'.repeat(40),candidateSha:'b'.repeat(40),mode:'TASK_SCOPED_AUTO',
    authorizationRef:'linear:GPT-123',taskScope:'Fix account menu link',authorizedPrefixes:['src/account'],
    changedPaths:['src/account/menu.ts'],riskAttestation:'LOW'};
  assert.equal(evaluateScope(base).status,'AUTO_RELEASE_ALLOWED');
  for (const change of [
    {changedPaths:['src/account/menu.ts','src/other.ts']},
    {changedPaths:['.github/workflows/deploy.yml'],authorizedPrefixes:['.']},
    {riskAttestation:'UNKNOWN'}, {authorizationRef:''}, {mode:'EXACT_SHA'},
  ]) assert.equal(evaluateScope({...base,...change}).status,'OWNER_DECISION_REQUIRED');
});
