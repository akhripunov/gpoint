import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {deploymentPayload} from './github-deployments.mjs';
import {assertRecoveryEligibility,recoveryInventory,verifyRecoveredVps,recoveryReference} from './recover-interrupted-apply.mjs';

const sha='a'.repeat(40), executor='b'.repeat(40), native=['4','5','6'].map(x=>'sha256:'+x.repeat(64));
const identity={candidateSha:sha,runId:'1234',runAttempt:'1',prepareRunId:'1000',
 prepareRunAttempt:'1',prepareReceiptDigest:'sha256:'+'e'.repeat(64),prepareReceiptSha256:'f'.repeat(64),
 imageDigests:{app:'sha256:'+'7'.repeat(64),agata:'sha256:'+'8'.repeat(64),gsales:'sha256:'+'9'.repeat(64)},
 authorizationMode:'EXACT_SHA',authorizationId:null,route:'FULL_RELEASE',
 logUrl:'https://github.com/akhripunov/gpoint/actions/runs/1234/attempts/1'};
const deployment={id:75,sha,environment:'production',task:'gpoint-production-deploy',ref:sha,
 created_at:'2026-10-19T00:00:00Z',payload:JSON.stringify(deploymentPayload(identity))};
const statuses=[
 {id:5,state:'error',log_url:identity.logUrl,description:'stage=post_apply sha='+sha.slice(0,12)+' run=1234/1'},
 {id:4,state:'in_progress',log_url:identity.logUrl,description:'stage=pre_apply sha='+sha.slice(0,12)+' run=1234/1'},
];
const run={id:1234,head_sha:sha,path:'.github/workflows/gpoint-deploy.yml',
 event:'workflow_dispatch',head_branch:'main',status:'completed',conclusion:'failure',run_attempt:1};
const jobs={jobs:[{name:'deploy',conclusion:'failure',run_attempt:1,steps:[{name:'Verify receipt, canonical preflight, apply and persist evidence',conclusion:'failure'}]}]};
const args={deployment,statuses,run,jobs,mainSha:executor,executorSha:executor,candidateSha:sha,deploymentId:75,originalRunId:'1234',prepareRunId:'1000',prepareRunAttempt:'1'};
const roles=['app','agata','gsales'];
const receipt={schema:3,transport:'ghcr-oci',status:'READY',candidateSha:sha,
 prismaHead:'prisma_v1',agataHead:'agata_v1',gsalesMigrationHead:'gsales_v1',gsalesMigrationSha256:'c'.repeat(64),
 images:Object.fromEntries(roles.map((r,i)=>[r,{repository:{
  app:'ghcr.io/akhripunov/gpoint-runtime',
  agata:'ghcr.io/akhripunov/gpoint-agata-runtime',
  gsales:'ghcr.io/akhripunov/gpoint-g-sales-runtime'}[r],digest:identity.imageDigests[r],
  configDigest:'sha256:'+String(i+1).repeat(64),platform:'linux/amd64',sourceSha:sha}])),
 deployRehearsal:{images:Object.fromEntries(roles.map((r,i)=>[r,{nativeId:native[i],
  configDigest:'sha256:'+String(i+1).repeat(64),digest:identity.imageDigests[r],sourceSha:sha}]))}};
const containers={app:['compose-api-1','compose-bot-1','compose-worker-1'],
 agata:['compose-agata-api-1','compose-agata-memory-worker-1','compose-gsales-agata-api-1','compose-gsales-agata-memory-worker-1'],
 gsales:['compose-gsales-api-1']};
const fields={app:['LIVE_API_IMAGE','LIVE_WORKER_IMAGE','LIVE_BOT_IMAGE'],
 agata:['LIVE_AGATA_API_IMAGE_ID','LIVE_AGATA_WORKER_IMAGE_ID','LIVE_GSALES_AGATA_API_IMAGE_ID','LIVE_GSALES_AGATA_MEMORY_WORKER_IMAGE_ID'],
 gsales:['LIVE_GSALES_API_IMAGE_ID']};
const services=['API','WORKER','BOT','AGATA_API','AGATA_WORKER','GSALES_API','GSALES_AGATA_API','GSALES_AGATA_MEMORY_WORKER'];
const healthy=['API','AGATA_API','AGATA_WORKER','GSALES_API','GSALES_AGATA_API','GSALES_AGATA_MEMORY_WORKER'];
const facts={LIVE_RELEASE_ENV_SHA:sha,LIVE_MINIAPP_SHA:sha,LIVE_PRISMA_HEAD:receipt.prismaHead,
 LIVE_AGATA_HEAD:receipt.agataHead,LIVE_GSALES_HEAD:receipt.gsalesMigrationHead,
 LIVE_GSALES_MIGRATION_SHA256:receipt.gsalesMigrationSha256};
for(const role of services){facts['LIVE_'+role+'_SHA']=sha;facts['LIVE_'+role+'_RUNNING']='true'}
for(const role of healthy)facts['LIVE_'+role+'_HEALTH']='healthy';
for(const [role,keys] of Object.entries(fields))for(const key of keys)facts[key]=native[roles.indexOf(role)];
const nativeOutput=roles.flatMap((r,i)=>[
 'COMPONENT_V4_IMAGE='+[r,native[i],'linux','amd64',sha].join('|'),
 ...containers[r].map(c=>'COMPONENT_V4_CONTAINER='+[r,c,native[i],'true'].join('|'))]).join('\n');

test('recovery eligibility binds failed original attempt and same unmodified Deployment',()=>{
 assert.equal(assertRecoveryEligibility(args).candidateSha,sha);
 for(const altered of [
  {mainSha:'c'.repeat(40)}, {deploymentId:76},{statuses:[{...statuses[0],description:'stage=complete'},statuses[1]]},
  {statuses:[{...statuses[0],state:'success'},statuses[1]]},
  {run:{...run,conclusion:'success'}},{run:{...run,head_sha:'b'.repeat(40)}},
  {jobs:{jobs:[{name:'deploy',conclusion:'success',run_attempt:1}]}},
  {deployment:{...deployment,sha:'b'.repeat(40)}},
  {deployment:{...deployment,payload:JSON.stringify(deploymentPayload({...identity,authorizationMode:'TASK_SCOPED_AUTO',authorizationId:'task-test-001'}))}},
 ])assert.throws(()=>assertRecoveryEligibility({...args,...altered}));
});
test('recovery enforces native ID, OCI config, running containers, health, db and revision',()=>{
 assert.equal(recoveryInventory(receipt,identity,sha).agata.nativeId,native[1]);
 assert.equal(recoveryInventory({...receipt,route:'FULL_RELEASE',staticOnly:false},identity,sha).agata.nativeId,native[1]);
 assert.throws(()=>recoveryInventory(receipt,{...identity,route:'STATIC_UI_ONLY'},sha));
 for(const invalid of [
  {...receipt,route:'STATIC_UI_ONLY'},
  {...receipt,route:'UNKNOWN'},
  {...receipt,route:null},
  {...receipt,staticOnly:'true'},
  {...receipt,staticOnly:true},
  {...receipt,transport:'legacy'},
  {...receipt,status:'FAILED'},
 ])assert.throws(()=>recoveryInventory(invalid,identity,sha));
 const verified=verifyRecoveredVps({receipt,identity,candidateSha:sha,facts,nativeOutput});
 assert.equal(verified.fingerprint.sourceSha,sha);
 assert.throws(()=>recoveryInventory({...receipt,deployRehearsal:{images:{...receipt.deployRehearsal.images,app:{
  ...receipt.deployRehearsal.images.app,nativeId:receipt.images.app.configDigest}}}},identity,sha));
 for(const altered of [
  {facts:{...facts,LIVE_PRISMA_HEAD:'unexpected'}},
  {facts:{...facts,LIVE_GSALES_AGATA_API_IMAGE_ID:native[2]}},
  {facts:{...facts,LIVE_BOT_RUNNING:'false'}},
  {facts:{...facts,LIVE_AGATA_WORKER_HEALTH:'unhealthy'}},
  {nativeOutput:nativeOutput.replace('compose-gsales-api-1|'+native[2]+'|true','compose-gsales-api-1|'+native[1]+'|true')},
 ])assert.throws(()=>verifyRecoveredVps({receipt,identity,candidateSha:sha,facts,nativeOutput,...altered}));
});
test('recovery OCI reference is unique to exact deployment, run and attempt',()=>{
 const a={CANDIDATE_SHA:sha,RECOVERY_DEPLOYMENT_ID:'75',GITHUB_RUN_ID:'12345',GITHUB_RUN_ATTEMPT:'1'};
 assert.match(recoveryReference(a),/recover-a{40}-75-12345-1$/);
 assert.notEqual(recoveryReference(a),recoveryReference({...a,GITHUB_RUN_ATTEMPT:'2'}));
});

test('interrupted recovery status is bound to original failed run and distinct recovery workflow',async()=>{
 const {interruptedRecoveryForStatus,authorizationReconciliationForStatus}=await import('./github-deployments.mjs');
 const current={state:'success',log_url:identity.logUrl,
  description:'stage=complete sha='+sha.slice(0,12)+' run=1234/1 recovered=9988/1'};
 assert.deepEqual(interruptedRecoveryForStatus(current,identity),{runId:'9988',runAttempt:'1'});
 assert.equal(authorizationReconciliationForStatus(current,identity),null);
 for(const wrong of [
  {...current,log_url:'https://invalid.example/'},
  {...current,description:current.description.replace('9988/1','1234/1')},
  {...current,description:current.description.replace('9988/1','invalid')},
 ])assert.throws(()=>interruptedRecoveryForStatus(wrong,identity));
});

test('recovered parent requires trusted new attestation rather than the failed original OCI artifact',async()=>{
 const {loadV5ParentDeployEvidence}=await import('./release-gpoint.mjs');
 const verified={identity,deployment,status:{state:'success'},
  recoveryAttestation:{runId:'9988',runAttempt:'1'}};
 const expected={candidateSha:sha,authorizationMode:'EXACT_SHA',authorizationId:null,
  status:'COMPLETE',releaseState:'COMPLETE',recoveryRequired:false,applicationVerification:'VPS_READBACK_VERIFIED',
  deployRunConclusion:'failure',recoveryRunConclusion:'success',recoveryAttestationVerified:true};
 let supplied;
 const found=await loadV5ParentDeployEvidence(verified,sha,'token-read',{
  fetchOriginal:async()=>{throw Error('Original OCI is unavailable')},
  fetchRecovered:async args=>{supplied=args;return expected},
 });
 assert.equal(found,expected);
 assert.equal(supplied.runId,'9988');
 assert.equal(supplied.originalRunId,'1234');
 assert.equal(supplied.prepareReceiptSha256,identity.prepareReceiptSha256);
 for(const broken of [
  {...expected,recoveryAttestationVerified:false},
  {...expected,recoveryRunConclusion:'failure'},
  {...expected,recoveryRequired:true},
 ])await assert.rejects(loadV5ParentDeployEvidence(verified,sha,'token-read',{fetchRecovered:async()=>broken}));
});

test('immutable recovery proof must not erase original failed Deploy or alter native/config digest evidence',async()=>{
 const {verifyInterruptedRecoveryProof}=await import('./reconcile-deployment.mjs');
 const crypto=await import('node:crypto');
 const json=JSON.stringify({...receipt,runId:1000,runAttempt:1,transport:'ghcr-oci',status:'READY'});
 const sha256=crypto.createHash('sha256').update(json).digest('hex');
 const fingerprint=verifyRecoveredVps({receipt,identity,candidateSha:sha,facts,nativeOutput}).fingerprint;
 const source={
  schema:1,operation:'INTERRUPTED_APPLY_INDEPENDENT_RECOVERY',status:'COMPLETE',releaseState:'COMPLETE',
  recoveryRequired:false,applicationVerification:'VPS_READBACK_VERIFIED',serverDeployStatus:'COMPLETE',
  candidateSha:sha,deploymentId:75,originalRunId:'1234',originalRunAttempt:'1',
  recoveryRunId:'9988',recoveryRunAttempt:'1',executorSha:'b'.repeat(40),
  prepareRunId:'1000',prepareRunAttempt:'1',authorizationMode:'EXACT_SHA',authorizationId:null,
  deployRunConclusion:'failure',originalDeployEvidence:'UNAVAILABLE_AFTER_RUNNER_INTERRUPTION',
  attestationSource:'NEW_TRUSTED_RECOVERY_RUN_NOT_ORIGINAL_DEPLOY_RUN',
  originalFailure:{deploymentId:75,statusId:5,status:'error',
   description:'stage=post_apply sha='+sha.slice(0,12)+' run=1234/1',
   originalRunConclusion:'failure',originalDeployJobConclusion:'failure'},
  ciReceipt:{ghcrVerified:true,ghcrPostSwitchVerified:true,selectedTransport:'ghcr-oci',
   ghcrManifestDigest:identity.prepareReceiptDigest,prepareReceiptSha256:sha256,prepareReceiptJson:json},
  readbackFingerprint:fingerprint,
  componentImages:Object.fromEntries(roles.map(r=>[r,{
   sourceSha:sha,imageId:receipt.images[r].configDigest,registryDigest:receipt.images[r].digest,
   platform:'linux/amd64'}])),
 };
 const expected={candidateSha:sha,deploymentId:75,originalRunId:'1234',originalRunAttempt:'1',
  recoveryRunId:'9988',recoveryRunAttempt:'1',prepareRunId:'1000',prepareRunAttempt:'1',
  prepareReceiptDigest:identity.prepareReceiptDigest,prepareReceiptSha256:sha256};
 assert.equal(verifyInterruptedRecoveryProof(source,expected),source);
 const bridge = new URL('./verify-interrupted-recovery-proof.mjs',import.meta.url);
 const invoke = proof => spawnSync(process.execPath,[bridge.pathname],{
  input:JSON.stringify({proof,expected}),encoding:'utf8',timeout:30000});
 const accepted=invoke(source);
 assert.equal(accepted.status,0,accepted.stderr);
 assert.deepEqual(JSON.parse(accepted.stdout),{verified:true});
 const rejected=invoke({...source,deployRunConclusion:'success'});
 assert.equal(rejected.status,1);
 assert.equal(rejected.stdout,'');
 assert.match(rejected.stderr,/INTERRUPTED_RECOVERY_PROOF=BLOCKED/);
 for(const wrong of [
  {...source,deployRunConclusion:'success'},
  {...source,ciReceipt:{...source.ciReceipt,prepareReceiptJson:json+' '}},
  {...source,originalFailure:{...source.originalFailure,originalRunConclusion:'success'}},
  {...source,componentImages:{...source.componentImages,app:{...source.componentImages.app,imageId:native[0]}}},
  {...source,readbackFingerprint:{...fingerprint,images:{...fingerprint.images,agata:native[0]}}},
 ])assert.throws(()=>verifyInterruptedRecoveryProof(wrong,expected));
});
