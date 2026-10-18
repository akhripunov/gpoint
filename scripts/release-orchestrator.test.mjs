import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {dispatchPrepare as dispatchPrepareImpl, findTaskAuthorization, validateEvent, OrchestrationError, taskAutoClaimRef,
  controllerFailureDiagnostic, preparedReceiptFailureDiagnostic, appendControllerResultSummary} from './release-orchestrator.mjs';
import {ownerTaskCommitmentSha256, candidateScopeDiff, validateTaskAuthorization} from './release-authorization.mjs';

const sha = 'a'.repeat(40);
const fakeReceipt = async () => ({receiptDigest:`sha256:${'d'.repeat(64)}`, receiptSha256:'e'.repeat(64)});
let receiptResolver = fakeReceipt;
const dispatchPrepare = (payload, api, orchestratorRunId, repoRoot = process.cwd(), authorizationLookup) =>
  dispatchPrepareImpl(payload, api, orchestratorRunId, repoRoot, authorizationLookup, receiptResolver);
const event = (change = {}) => ({repository:{full_name:'akhripunov/gpoint'}, workflow_run:{id:123,run_attempt:2,
  head_sha:sha,head_branch:'main',status:'completed',conclusion:'success',event:'push',path:'.github/workflows/gpoint-prepare.yml',...change}});

function apiFixture({candidateSha = sha, existingClaimSha = null} = {}) {
  const calls=[]; let claimed=existingClaimSha !== null;
  const api=async(method,path,body)=>{
    calls.push({method,path,body});
    if(path==='git/ref/heads/main') return {object:{sha:candidateSha}};
    if(path==='actions/runs/123') return event({head_sha:candidateSha}).workflow_run;
    if(path==='actions/runs/123/attempts/2') return {run_attempt:2,run_started_at:'2026-10-14T04:51:39Z'};
    if(path==='actions/runs/123/attempts/1') return {run_attempt:1,run_started_at:'2026-10-14T04:38:31Z'};
    if(path==='actions/runs/123/attempts/2/jobs?per_page=100') return {total_count:1,jobs:[{name:'prepare',status:'completed',conclusion:'success',head_sha:candidateSha,started_at:'2026-10-14T04:51:40Z',completed_at:'2026-10-14T04:52:20Z'}]};
    if(path==='actions/runs/123/attempts/1/jobs?per_page=100') return {total_count:1,jobs:[{name:'prepare',status:'completed',conclusion:'success',head_sha:candidateSha,started_at:'2026-10-14T04:38:34Z',completed_at:'2026-10-14T04:46:48Z'}]};
    if(path.includes('actions/workflows/gpoint-deploy.yml/runs')) return {workflow_runs:[]};
    if(path.startsWith('git/ref/tags/gpoint-task-auth/')) { if(claimed)return {object:{type:'commit',sha:existingClaimSha ?? candidateSha}}; const e=new OrchestrationError('missing');e.status=404;throw e; }
    if(path==='git/refs') {if(claimed){const e=new OrchestrationError('duplicate');e.status=422;throw e;} claimed=true;return {};}
    if(path.endsWith('/dispatches')) return {};
    throw new Error(`Unexpected ${method} ${path}`);
  };
  return {api,calls};
}

test('trusted success event is exact',()=>assert.equal(validateEvent(event()).id,123));
test('controller failure diagnostics expose bounded stage metadata without leaking error details',()=>{
  const error=new OrchestrationError('Authorization: Bearer private-token https://example.invalid/logs?sig=private');
  error.status=403;
  const diagnostic=controllerFailureDiagnostic(error,{stage:'prepare_receipt_readback',resource:'ghcr_prepare_receipt',
    candidateSha:sha,prepareRunId:'39119441234',prepareRunAttempt:'1'});
  assert.deepEqual(diagnostic,{state:'BLOCKED',stage:'prepare_receipt_readback',code:'GITHUB_HTTP_ERROR',httpStatus:403,
    resource:'ghcr_prepare_receipt',candidateSha:sha,prepareRunId:'39119441234',prepareRunAttempt:'1'});
  assert.doesNotMatch(JSON.stringify(diagnostic),/private-token|Authorization|sig=private|example\.invalid/);
});
test('prepared receipt diagnostics classify only an allowlisted sanitized stage',()=>{
  assert.deepEqual(preparedReceiptFailureDiagnostic('noise\nARTIFACT_CACHE=BLOCKED STAGE=ghcr_receipt ERROR_TYPE=RegistryError\n'),
    {stage:'prepare_receipt_readback',substage:'ghcr_receipt',resource:'ghcr_prepare_receipt',code:'GHCR_READ_FAILED'});
  assert.deepEqual(preparedReceiptFailureDiagnostic('ARTIFACT_CACHE=BLOCKED STAGE=prepare_run_metadata ERROR_TYPE=GitHubError'),
    {stage:'prepare_receipt_readback',substage:'prepare_run_metadata',resource:'prepare_evidence',code:'PREPARE_EVIDENCE_FAILED'});
  assert.deepEqual(preparedReceiptFailureDiagnostic('ARTIFACT_CACHE=BLOCKED STAGE=prepare_publisher_logs ERROR_TYPE=GitHubError'),
    {stage:'prepare_receipt_readback',substage:'prepare_publisher_logs',resource:'prepare_evidence',code:'PREPARE_EVIDENCE_FAILED'});
  assert.deepEqual(preparedReceiptFailureDiagnostic('ARTIFACT_CACHE=BLOCKED STAGE=receipt_validation ERROR_TYPE=ValueError'),
    {stage:'prepare_receipt_readback',substage:'receipt_validation',resource:'prepare_receipt',code:'PREPARE_RECEIPT_INVALID'});
  assert.deepEqual(preparedReceiptFailureDiagnostic('ARTIFACT_CACHE=BLOCKED STAGE=untrusted ERROR_TYPE=SecretError'),
    {stage:'prepare_receipt_readback',resource:'prepare_receipt',code:'LOCAL_SUBPROCESS_FAILED',diagnosticRejection:'unsafe_stage'});
  assert.doesNotMatch(JSON.stringify(preparedReceiptFailureDiagnostic('ARTIFACT_CACHE=BLOCKED STAGE=ghcr_receipt ERROR_TYPE=AuthorizationHeader')),/AuthorizationHeader/);
});
test('prepared receipt structured diagnostics preserve safe GHCR category and status for the exact run',()=>{
  const payload={schema:1,stage:'ghcr_receipt_tag_manifest_auth_token',errorType:'HTTPError',category:'PERMISSION',httpStatus:403,
    candidateSha:sha,runId:'123',runAttempt:'2',ghcrReadbackCompleted:false};
  const output=`ARTIFACT_CACHE_DIAGNOSTIC=${JSON.stringify(payload)}\nARTIFACT_CACHE=BLOCKED STAGE=ghcr_receipt ERROR_TYPE=NetworkStageError`;
  assert.deepEqual(preparedReceiptFailureDiagnostic(output,{candidateSha:sha,runId:'123',runAttempt:'2'}),{
    stage:'prepare_receipt_readback',substage:'ghcr_receipt_tag_manifest_auth_token',resource:'ghcr_prepare_receipt',
    code:'GHCR_READ_FAILED',errorType:'HTTPError',category:'PERMISSION',httpStatus:403,
    ghcrReadbackCompleted:false,candidateSha:sha,runId:'123',runAttempt:'2'});
  const error=new OrchestrationError('private GHCR response body');
  error.controllerDiagnostic=preparedReceiptFailureDiagnostic(output,{candidateSha:sha,runId:'123',runAttempt:'2'});
  const controller=controllerFailureDiagnostic(error,{stage:'prepare_receipt_readback',candidateSha:sha,
    prepareRunId:'123',prepareRunAttempt:'2'});
  assert.deepEqual(controller,{state:'BLOCKED',stage:'prepare_receipt_readback',code:'GHCR_READ_FAILED',httpStatus:403,
    resource:'ghcr_prepare_receipt',substage:'ghcr_receipt_tag_manifest_auth_token',errorType:'HTTPError',
    category:'PERMISSION',ghcrReadbackCompleted:false,candidateSha:sha,prepareRunId:'123',prepareRunAttempt:'2'});
  assert.doesNotMatch(JSON.stringify(controller),/private GHCR response body|"secret"|Bearer |Authorization:/);
});
test('prepared receipt structured diagnostics reject mismatched identity and unsafe categories',()=>{
  const payload={schema:1,stage:'ghcr_receipt_tag_manifest_auth_token',errorType:'HTTPError',category:'SECRET_TOKEN',httpStatus:403,
    candidateSha:'b'.repeat(40),runId:'123',runAttempt:'2',ghcrReadbackCompleted:false};
  const output=`ARTIFACT_CACHE_DIAGNOSTIC=${JSON.stringify(payload)}`;
  assert.deepEqual(preparedReceiptFailureDiagnostic(output,{candidateSha:sha,runId:'123',runAttempt:'2'}),
    {stage:'prepare_receipt_readback',resource:'prepare_receipt',code:'LOCAL_SUBPROCESS_FAILED',
      diagnosticRejection:'identity_mismatch',candidateSha:sha,runId:'123',runAttempt:'2'});
});
test('receipt subprocess failure reports only allowlisted execution metadata, never raw stderr',()=>{
  const diagnostic=preparedReceiptFailureDiagnostic('',{candidateSha:sha,runId:'123',runAttempt:'2'},
    {status:1,signal:null,error:null,stderr:'Traceback at /private/secret/path.py\nModuleNotFoundError: private-module-secret'});
  assert.deepEqual(diagnostic,{stage:'prepare_receipt_readback',resource:'prepare_receipt',
    code:'LOCAL_SUBPROCESS_FAILED',candidateSha:sha,runId:'123',runAttempt:'2',
    subprocessStatus:1,subprocessStderrErrorType:'ModuleNotFoundError',diagnosticRejection:'missing_diagnostic'});
  assert.doesNotMatch(JSON.stringify(diagnostic),/private|secret|path\.py/);
  const error=new OrchestrationError('private subprocess detail');
  error.controllerDiagnostic=diagnostic;
  assert.deepEqual(controllerFailureDiagnostic(error,{candidateSha:sha,prepareRunId:'123',prepareRunAttempt:'2'}),
    {state:'BLOCKED',stage:'prepare_receipt_readback',code:'LOCAL_SUBPROCESS_FAILED',resource:'prepare_receipt',
      candidateSha:sha,prepareRunId:'123',prepareRunAttempt:'2',subprocessStatus:1,
      subprocessStderrErrorType:'ModuleNotFoundError',diagnosticRejection:'missing_diagnostic'});
  assert.deepEqual(preparedReceiptFailureDiagnostic('',{candidateSha:sha,runId:'123',runAttempt:'2'},
    {status:null,signal:null,error:{name:'Error',code:'ENOENT'},stderr:''}),
    {stage:'prepare_receipt_readback',resource:'prepare_receipt',code:'LOCAL_SUBPROCESS_FAILED',
      candidateSha:sha,runId:'123',runAttempt:'2',subprocessErrorCode:'ENOENT',diagnosticRejection:'missing_diagnostic'});
});
test('summary failure after a successful dispatch is reported as uncertain post-dispatch state',()=>{
  assert.throws(()=>appendControllerResultSummary(()=>{throw new Error('sensitive filesystem detail');},
    {state:'TASK_SCOPED_AUTO_DISPATCHED',candidateSha:sha,authorizationId:'gpoint-task-2026-10-16-001'},1),error=>{
      assert.deepEqual(controllerFailureDiagnostic(error),{state:'BLOCKED',stage:'controller_result_summary',
        code:'SUMMARY_WRITE_FAILED',resource:'controller_step_summary',deployDispatched:true});
      assert.doesNotMatch(JSON.stringify(controllerFailureDiagnostic(error)),/sensitive filesystem detail/);
      return true;
    });
});
test('controller records the failing read-only stage and never claims or dispatches after an API error',async()=>{
  const {api,calls}=apiFixture();
  const stages=[];
  const failingApi=async(method,route,body)=>{
    if(route==='git/ref/heads/main') {
      const error=new OrchestrationError('private response body'); error.status=403; throw error;
    }
    return api(method,route,body);
  };
  await assert.rejects(dispatchPrepareImpl(event(),failingApi,'987',process.cwd(),undefined,undefined,
    stage=>stages.push(stage)),error=>{
      const diagnostic=controllerFailureDiagnostic(error,{stage:stages.at(-1),resource:'prepare_run',
        candidateSha:sha,prepareRunId:'123',prepareRunAttempt:'2'});
      assert.deepEqual(diagnostic,{state:'BLOCKED',stage:'current_main_ref',code:'GITHUB_HTTP_ERROR',httpStatus:403,
        resource:'prepare_run',candidateSha:sha,prepareRunId:'123',prepareRunAttempt:'2'});
      assert.doesNotMatch(JSON.stringify(diagnostic),/private response body/);
      return true;
    });
  assert.deepEqual(stages,['prepare_event_validation','current_main_ref']);
  assert.equal(calls.some(item=>item.path==='git/refs'),false);
  assert.equal(calls.some(item=>item.path.endsWith('/dispatches')),false);
});
test('controller preserves only the HTTP status when atomic claim creation fails',async()=>{
  const {api,calls}=apiFixture();
  const stages=[];
  const failingApi=async(method,route,body)=>{
    if(route==='git/refs') {const error=new OrchestrationError('private API response');error.status=503;throw error;}
    return api(method,route,body);
  };
  await assert.rejects(dispatchPrepareImpl(event(),failingApi,'987',process.cwd(),
    async()=>({authorizationId:'gpoint-task-2026-10-16-001',ambiguous:false}),fakeReceipt,
    stage=>stages.push(stage)),error=>{
      assert.deepEqual(controllerFailureDiagnostic(error,{stage:stages.at(-1),resource:'task_authorization_claim',
        candidateSha:sha,prepareRunId:'123',prepareRunAttempt:'2'}),
      {state:'BLOCKED',stage:'atomic_claim_create',code:'GITHUB_HTTP_ERROR',httpStatus:503,
        resource:'task_authorization_claim',candidateSha:sha,prepareRunId:'123',prepareRunAttempt:'2'});
      assert.doesNotMatch(JSON.stringify(controllerFailureDiagnostic(error)),/private API response/);
      return true;
    });
  assert.deepEqual(stages.at(-1),'atomic_claim_create');
  assert.equal(calls.some(item=>item.path.endsWith('/dispatches')),false);
});
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
  assert.equal(result.receiptDigest,`sha256:${'d'.repeat(64)}`);
  assert.equal(calls.some(item=>item.path==='git/refs'),false);
  assert.equal(calls.some(item=>item.path.endsWith('/dispatches')),false);
  assert.equal(calls.some(item=>item.path.includes('releases/')),false);
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
    prepare_run_id:'123',prepare_run_attempt:'2',prepare_receipt_digest:`sha256:${'d'.repeat(64)}`,
    prepare_receipt_sha256:'e'.repeat(64),orchestrator_run_id:'987',task_authorization_id:authorizationId}});
});
test('an existing task claim allows a fresh exact-main Prepare candidate to retry within the same authorization',async()=>{
  const newSha='c'.repeat(40); const oldSha='b'.repeat(40);
  const {api,calls}=apiFixture({candidateSha:newSha,existingClaimSha:oldSha});
  const authorizationId='gpoint-task-2026-10-16-001';
  const result=await dispatchPrepare(event({head_sha:newSha}),api,'987',process.cwd(),async()=>({authorizationId,ambiguous:false}));
  assert.equal(result.state,'TASK_SCOPED_AUTO_RETRY_DISPATCHED');
  assert.equal(result.candidateSha,newSha);
  assert.equal(calls.filter(item=>item.path==='git/refs').length,1,
    'the immutable first claim is preserved and never rewritten');
  const dispatch=calls.find(item=>item.path.endsWith('/dispatches'));
  assert.equal(dispatch.body.inputs.candidate,newSha);
});

test('the same immutable successful Prepare can retry after a failed Deploy has ended',async()=>{
  const {api,calls}=apiFixture({candidateSha:sha,existingClaimSha:sha});
  const authorizationId='gpoint-task-2026-10-16-001';
  const result=await dispatchPrepare(event(),api,'989',process.cwd(),async()=>({authorizationId,ambiguous:false}));
  assert.equal(result.state,'TASK_SCOPED_AUTO_RETRY_DISPATCHED');
  assert.equal(result.candidateSha,sha);
  assert.equal(result.prepareRunId,'123');
  assert.equal(result.prepareRunAttempt,'2');
  assert.equal(calls.filter(item=>item.path.endsWith('/dispatches')).length,1);
});

test('duplicate Prepare event is blocked while a Deploy is active',async()=>{
  const {api,calls}=apiFixture(); const authorizationId='gpoint-task-2026-10-16-001';
  const lookup=async()=>({authorizationId,ambiguous:false});
  let deployActive=false;
  const activeApi=async(method,route,body)=>{
    if(route.includes('actions/workflows/gpoint-deploy.yml/runs'))
      return {workflow_runs:deployActive?[{status:'in_progress'}]:[]};
    const result=await api(method,route,body);
    if(route.endsWith('/dispatches')) deployActive=true;
    return result;
  };
  const first=await dispatchPrepare(event(),activeApi,'987',process.cwd(),lookup);
  assert.equal(first.state,'TASK_SCOPED_AUTO_DISPATCHED');
  assert.equal(calls.filter(item=>item.path.endsWith('/dispatches')).length,1);
  await assert.rejects(dispatchPrepare(event(),activeApi,'988',process.cwd(),lookup),/Deploy workflow is active/);
  assert.equal(calls.filter(item=>item.path.endsWith('/dispatches')).length,1);
});
test('publish-only rerun selects the Prepare attempt that actually ran and uses its GHCR receipt',async()=>{
  const {api,calls}=apiFixture();
  const wrapped=async(method,path,body)=>{
    if(path==='actions/runs/123/attempts/2/jobs?per_page=100')
      return {total_count:1,jobs:[{name:'prepare',status:'completed',conclusion:'success',head_sha:sha,
        started_at:'2026-10-14T04:38:34Z',completed_at:'2026-10-14T04:46:48Z'}]};
    return api(method,path,body);
  };
  const seen=[];
  const result=await dispatchPrepareImpl(event(),wrapped,'987',process.cwd(),undefined,async (...args)=>{
    seen.push(args); return fakeReceipt();
  });
  assert.equal(result.prepareRunAttempt,'1');
  assert.equal(result.tag,`prepare-${sha}-123-1`);
  assert.deepEqual(seen,[[sha,'123','1']]);
  assert.equal(calls.some(x=>x.path.includes('releases/')),false);
});
test('missing or tampered GHCR receipt evidence blocks automatic Deploy',async()=>{
  for (const resolver of [async()=>{throw new Error('GHCR outage');},
    async()=>({receiptDigest:'latest',receiptSha256:'e'.repeat(64)}),
    async()=>({receiptDigest:`sha256:${'d'.repeat(64)}`,receiptSha256:'bad'})]) {
    const {api,calls}=apiFixture();
    await assert.rejects(dispatchPrepareImpl(event(),api,'987',process.cwd(),undefined,resolver),OrchestrationError);
    assert.equal(calls.some(item=>item.path.endsWith('/dispatches')),false);
    assert.equal(calls.some(item=>item.path==='git/refs'),false);
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
