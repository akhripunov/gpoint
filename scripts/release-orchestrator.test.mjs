import test from 'node:test';
import assert from 'node:assert/strict';
import {dispatchPrepare, validateEvent, OrchestrationError, claimRef} from './release-orchestrator.mjs';

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
test('duplicate event is a no-op after atomic claim',async()=>{
  const {api,calls}=apiFixture();
  await dispatchPrepare(event(),api);
  const duplicate=await dispatchPrepare(event(),api);
  assert.equal(duplicate.state,'PREFLIGHT_ALREADY_CLAIMED');
  assert.equal(calls.filter(item=>item.path.endsWith('/dispatches')).length,1);
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
  assert.equal(calls.find(x=>x.path.endsWith('/dispatches')).body.inputs.prepare_run_attempt,'1');
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
test('mutable or draft releases fail closed',async()=>{
  for(const patch of [{immutable:false},{draft:true}]) {
    const {api}=apiFixture(); const original=api;
    const wrapped=async(method,path,body)=>{const result=await original(method,path,body);if(path.startsWith('releases/tags/'))Object.assign(result,patch);return result;};
    await assert.rejects(dispatchPrepare(event(),wrapped),/immutable and published/);
  }
});
