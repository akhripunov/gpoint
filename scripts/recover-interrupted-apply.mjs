#!/usr/bin/env node
// Metadata-only recovery of an interrupted full V5 Apply. Never imports or invokes an Apply operation.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {verifyReleaseBundle} from './release-bundle.mjs';
import {validateOciReceipt} from './release-oci.mjs';
import {createGitHubDeploymentsClient, deploymentIdentity} from './github-deployments.mjs';
import {inspectProductionReadOnly,runRemoteCommand} from './release-gpoint-vps-adapter.mjs';
import {assertGsalesRuntimeState,factsFromReadOnlyInspection,stateFromLiveVps} from './deployment-run-state.mjs';
import {buildReadOnlyVPSProofCommand,parseReadOnlyVPSProof} from './release-component-vps-proof-v4.mjs';

const ROOT=fileURLToPath(new URL('..',import.meta.url));
const REPO='akhripunov/gpoint', SHA=/^[a-f0-9]{40}$/, DIGEST=/^sha256:[a-f0-9]{64}$/;
const POS=/^[1-9][0-9]*$/;
const ROLES=['app','agata','gsales'];
const IMAGE_FIELDS={
 app:['LIVE_API_IMAGE','LIVE_WORKER_IMAGE','LIVE_BOT_IMAGE'],
 agata:['LIVE_AGATA_API_IMAGE_ID','LIVE_AGATA_WORKER_IMAGE_ID','LIVE_GSALES_AGATA_API_IMAGE_ID','LIVE_GSALES_AGATA_MEMORY_WORKER_IMAGE_ID'],
 gsales:['LIVE_GSALES_API_IMAGE_ID'],
};
const RUNNING=['API','WORKER','BOT','AGATA_API','AGATA_WORKER'];
const HEALTH=['API','AGATA_API','AGATA_WORKER'];
const fatal=(name)=>{throw new Error('RECOVERY_GATE='+name);};
const ensure=(truth,name)=>{if(!truth)fatal(name);};
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
const api=async (route,token)=>{
 const response=await fetch('https://api.github.com/repos/'+REPO+'/'+route,{
  headers:{Authorization:'Bearer '+token,Accept:'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'},
  signal:AbortSignal.timeout(30000)});
 ensure(response.ok,'GITHUB_READ_HTTP_'+response.status);
 return response.json();
};
export function assertRecoveryEligibility({deployment,statuses,run,jobs,mainSha,candidateSha,deploymentId,originalRunId,prepareRunId,prepareRunAttempt,executorSha}){
 ensure(SHA.test(candidateSha)&&SHA.test(executorSha)&&candidateSha!==executorSha
  &&mainSha===executorSha,'EXACT_RECOVERY_EXECUTOR_MAIN');
 ensure(Number.isSafeInteger(deploymentId)&&deploymentId>0 && POS.test(String(originalRunId)),'ID');
 ensure(deployment.id===deploymentId&&deployment.sha===candidateSha&&deployment.environment==='production'
  && deployment.task==='gpoint-production-deploy','DEPLOYMENT');
 const identity=deploymentIdentity(deployment);
 ensure(identity.candidateSha===candidateSha&&identity.runId===String(originalRunId)
  && identity.authorizationMode==='EXACT_SHA'&&identity.authorizationId===null
  && identity.route==='FULL_RELEASE'&&identity.prepareRunId===String(prepareRunId)
  && identity.prepareRunAttempt===String(prepareRunAttempt),'ORIGINAL_IDENTITY');
 ensure(run.id===Number(originalRunId)&&run.head_sha===candidateSha&&run.path==='.github/workflows/gpoint-deploy.yml'
  &&run.event==='workflow_dispatch'&&run.head_branch==='main'
  &&run.status==='completed'&&run.conclusion==='failure'
  &&String(run.run_attempt)===identity.runAttempt,'ORIGINAL_RUN_FAILURE');
 ensure((jobs.jobs??[]).filter(j=>j.name==='deploy'&&j.conclusion==='failure'&&String(j.run_attempt)===identity.runAttempt).length===1
  && !(jobs.jobs??[]).some(j=>j.name==='deploy'&&j.conclusion==='success')
  && !(jobs.jobs??[]).some(j=>(j.steps??[]).some(s=>s.name==='Publish deployment result as a GHCR OCI artifact'&&s.conclusion==='success')),
  'ORIGINAL_DEPLOY_NOT_ATTESTED');
 ensure(Array.isArray(statuses)&&statuses.length>=2&&statuses[0].state==='error'
  &&statuses[0].description===`stage=post_apply sha=${candidateSha.slice(0,12)} run=${originalRunId}/${identity.runAttempt}`
  &&statuses[0].log_url===identity.logUrl
  &&statuses.some(x=>x.state==='in_progress'&&x.description===`stage=pre_apply sha=${candidateSha.slice(0,12)} run=${originalRunId}/${identity.runAttempt}`)
  &&!statuses.some(x=>x.state==='success'),'ORIGINAL_POST_APPLY_ERROR');
 return identity;
}
export function recoveryInventory(receipt,identity,candidateSha){
 // Schema-3 GHCR receipts may omit route for a full Deploy; the Deployment
 // itself carries the canonical FULL_RELEASE route. Never infer a static route.
 ensure(receipt?.candidateSha===candidateSha && receipt.schema===3
  && receipt.transport==='ghcr-oci' && receipt.status==='READY'
  && (receipt.route===undefined || receipt.route==='FULL_RELEASE')
  && (receipt.staticOnly===undefined || receipt.staticOnly===false)
  && identity.route==='FULL_RELEASE','RECEIPT_ROUTE');
 const inventory={};
 for(const role of ROLES){
  const image=receipt.images?.[role], rehearsal=receipt.deployRehearsal?.images?.[role];
  ensure(DIGEST.test(image?.digest??'')&&DIGEST.test(image?.configDigest??'')
    &&DIGEST.test(rehearsal?.nativeId??'')
    &&rehearsal.nativeId!==image.configDigest
    &&rehearsal.configDigest===image.configDigest&&rehearsal.digest===image.digest
    &&rehearsal.sourceSha===candidateSha&&image.sourceSha===candidateSha
    &&image.platform==='linux/amd64'&&identity.imageDigests[role]===image.digest,
   'PREPARE_IMAGE_'+role);
  inventory[role]={name:role,sourceSha:candidateSha,imageId:image.configDigest,nativeId:rehearsal.nativeId,
   registryDigest:image.digest,platform:'linux/amd64'};
 }
 return inventory;
}
export function verifyRecoveredVps({receipt,identity,candidateSha,facts,nativeOutput}){
 const inventory=recoveryInventory(receipt,identity,candidateSha);
 const state=stateFromLiveVps(facts);
 assertGsalesRuntimeState(facts);
 ensure(state.currentProductionSha===candidateSha&&state.miniappSourceSha===candidateSha
  &&state.currentProductionDbHead===receipt.prismaHead&&state.agataAlembicHead===receipt.agataHead
  &&state.gsalesMigrationHead===receipt.gsalesMigrationHead
  &&state.gsalesMigrationSha256===receipt.gsalesMigrationSha256,'LIVE_SHA_AND_MIGRATIONS');
 for(const role of RUNNING)ensure(facts['LIVE_'+role+'_RUNNING']==='true','RUNNING_'+role);
 for(const role of HEALTH)ensure(facts['LIVE_'+role+'_HEALTH']==='healthy','HEALTH_'+role);
 for(const role of ROLES)for(const key of IMAGE_FIELDS[role])
  ensure(facts[key]===inventory[role].nativeId,'LIVE_IMAGE_'+key);
 const native=parseReadOnlyVPSProof(nativeOutput,inventory);
 ensure(ROLES.every(role=>native[role].nativeImageId===inventory[role].nativeId),'NATIVE_PROOF');
 const fingerprint={sourceSha:candidateSha,miniappSha:state.miniappSourceSha,
  prismaHead:state.currentProductionDbHead,agataHead:state.agataAlembicHead,
  gsalesHead:state.gsalesMigrationHead,gsalesMigrationSha256:state.gsalesMigrationSha256,
  images:Object.fromEntries(ROLES.map(r=>[r,inventory[r].nativeId])),
  running:Object.fromEntries(RUNNING.map(r=>[r,facts['LIVE_'+r+'_RUNNING']])),
  healthy:Object.fromEntries(HEALTH.map(r=>[r,facts['LIVE_'+r+'_HEALTH']])),
  gsalesRuntime:Object.fromEntries(['API','AGATA_API','AGATA_MEMORY_WORKER'].map(role=>[role,Object.fromEntries(
   ['STATUS','RUNNING','HEALTH','EXIT_CODE','OOM_KILLED'].map(field=>[field.toLowerCase(),facts[`LIVE_GSALES_${role}_${field}`]])
  )]))};
 return {inventory,fingerprint};
}
function inspect({receipt,identity,candidateSha}){
 const inventory=recoveryInventory(receipt,identity,candidateSha);
 const facts=factsFromReadOnlyInspection(inspectProductionReadOnly());
 const nativeOutput=runRemoteCommand(buildReadOnlyVPSProofCommand(inventory));
 return verifyRecoveredVps({receipt,identity,candidateSha,facts,nativeOutput});
}
async function context(env){
 const {CANDIDATE_SHA:candidateSha,RECOVERY_DEPLOYMENT_ID:rawId,ORIGINAL_DEPLOY_RUN_ID:originalRunId,
  PREPARE_RUN_ID:prepareRunId,PREPARE_RUN_ATTEMPT:prepareRunAttempt,GITHUB_TOKEN:token}=env;
 ensure(SHA.test(candidateSha??'')&&POS.test(rawId??'')&&POS.test(originalRunId??'')
  &&POS.test(prepareRunId??'')&&POS.test(prepareRunAttempt??'')&&token,'INPUTS');
 const deploymentId=Number(rawId);
 const client=createGitHubDeploymentsClient({token});
 const deployment=await client.getDeployment(deploymentId);
 const originalAttempt=deploymentIdentity(deployment).runAttempt;
 ensure(POS.test(originalAttempt),'ORIGINAL_ATTEMPT');
 const [statuses,run,jobs,main,deployments]=await Promise.all([
  client.listStatuses(deploymentId),
  api('actions/runs/'+originalRunId+'/attempts/'+originalAttempt,token),
  api('actions/runs/'+originalRunId+'/attempts/'+originalAttempt+'/jobs?per_page=100',token),
  api('git/ref/heads/main',token),client.listDeployments()]);
 ensure(deployments[0]?.id===deploymentId&&deployments.filter(d=>d.id===deploymentId).length===1,'LATEST_DEPLOYMENT');
 const identity=assertRecoveryEligibility({deployment,statuses,run,jobs,mainSha:main.object.sha,
  candidateSha,deploymentId,originalRunId,prepareRunId,prepareRunAttempt,executorSha:env.GITHUB_SHA});
 return {client,deployment,statuses,identity,candidateSha,deploymentId,originalRunId,
  prepareRunId,prepareRunAttempt};
}
function exactFile(env){
 const v=env.RECOVERY_RESULT;
 ensure(typeof v==='string'&&path.isAbsolute(v)&&v.startsWith(env.RUNNER_TEMP+'/')
  &&v===path.join(env.RUNNER_TEMP,
   `gpoint-recovery-${env.RECOVERY_DEPLOYMENT_ID}-${env.GITHUB_RUN_ID}-${env.GITHUB_RUN_ATTEMPT}.json`),'RESULT_PATH');
 return v;
}
export function recoveryReference(e){
 return `ghcr.io/akhripunov/gpoint-deploy-evidence:recover-${e.CANDIDATE_SHA}-${e.RECOVERY_DEPLOYMENT_ID}-${e.GITHUB_RUN_ID}-${e.GITHUB_RUN_ATTEMPT}`;
}
async function verify(env){
 ensure(env.GITHUB_ACTOR==='akhripunov'&&env.GITHUB_TRIGGERING_ACTOR==='akhripunov'
  &&env.GITHUB_REPOSITORY===REPO&&env.GITHUB_REF==='refs/heads/main','OWNER');
 ensure(execFileSync('git',['rev-parse','HEAD'],{cwd:ROOT,encoding:'utf8'}).trim()===env.GITHUB_SHA
  &&!execFileSync('git',['status','--porcelain'],{cwd:ROOT,encoding:'utf8'}).trim(),'CLEAN_CHECKOUT');
  ensure(execFileSync('git',['merge-base','--is-ancestor',env.CANDIDATE_SHA,env.GITHUB_SHA],{
    cwd:ROOT,encoding:'utf8'}).trim()==='','CANDIDATE_NOT_ANCESTOR');
 const c=await context(env);
 const bundle=verifyReleaseBundle(env.GPOINT_RELEASE_BUNDLE,c.candidateSha);
 const originalBytes=fs.readFileSync(path.join(env.GPOINT_RELEASE_BUNDLE,'receipt.json'));
 const receipt=validateOciReceipt(JSON.parse(originalBytes.toString('utf8')),c.candidateSha,
  {runId:c.prepareRunId,runAttempt:c.prepareRunAttempt});
 ensure(bundle.ghcrVerified===true && bundle.runId===Number(c.prepareRunId)
  &&bundle.runAttempt===Number(c.prepareRunAttempt)
  &&c.identity.prepareReceiptSha256===hash(originalBytes)
  &&c.identity.prepareReceiptDigest===JSON.parse(fs.readFileSync(path.join(env.GPOINT_RELEASE_BUNDLE,'ghcr-evidence.json'),'utf8')).manifestDigest,
  'EXACT_PREPARE_RECEIPT');
 const first=inspect({receipt,identity:c.identity,candidateSha:c.candidateSha});
 const second=inspect({receipt,identity:c.identity,candidateSha:c.candidateSha});
 ensure(same(first.fingerprint,second.fingerprint),'STABLE_LIVE_READBACK');
 const proof={schema:1,operation:'INTERRUPTED_APPLY_INDEPENDENT_RECOVERY',status:'COMPLETE',
  releaseState:'COMPLETE',recoveryRequired:false,applicationVerification:'VPS_READBACK_VERIFIED',
  serverDeployStatus:'COMPLETE',candidateSha:c.candidateSha,deploymentId:c.deploymentId,
  originalRunId:String(c.originalRunId),originalRunAttempt:c.identity.runAttempt,
  deployRunConclusion:'failure',recoveryRunId:String(env.GITHUB_RUN_ID),
  recoveryRunAttempt:String(env.GITHUB_RUN_ATTEMPT),executorSha:env.GITHUB_SHA,
  prepareRunId:String(c.prepareRunId),prepareRunAttempt:String(c.prepareRunAttempt),
  authorizationMode:'EXACT_SHA',authorizationId:null,
  ciReceipt:{selectedTransport:'ghcr-oci',ghcrVerified:true,ghcrPostSwitchVerified:true,
   ghcrManifestDigest:c.identity.prepareReceiptDigest,prepareReceiptSha256:c.identity.prepareReceiptSha256,
   prepareReceiptJson:originalBytes.toString('utf8'),runId:String(c.prepareRunId),runAttempt:String(c.prepareRunAttempt)},
  componentImages:Object.fromEntries(ROLES.map(role=>[role,{
   name:role,sourceSha:c.candidateSha,imageId:first.inventory[role].imageId,
   registryDigest:first.inventory[role].registryDigest,platform:first.inventory[role].platform}])),
  dbHeadAfter:first.fingerprint.prismaHead,agataDbHeadAfter:first.fingerprint.agataHead,
  gsalesMigrationHeadAfter:first.fingerprint.gsalesHead,
  gsalesMigrationSha256After:first.fingerprint.gsalesMigrationSha256,
  readbackFingerprint:first.fingerprint,
  originalFailure:{deploymentId:c.deploymentId,statusId:c.statuses[0].id,
   status:c.statuses[0].state,description:c.statuses[0].description,
   originalRunConclusion:'failure',originalDeployJobConclusion:'failure'},
  originalDeployEvidence:'UNAVAILABLE_AFTER_RUNNER_INTERRUPTION',
  attestationSource:'NEW_TRUSTED_RECOVERY_RUN_NOT_ORIGINAL_DEPLOY_RUN'};
 const file=exactFile(env);
 fs.writeFileSync(file,JSON.stringify(proof,null,2)+'\n',{flag:'wx',mode:0o600});
 console.log('RECOVERY_LIVE_VERIFICATION=PASS DEPLOYMENT_ID='+c.deploymentId);
}
async function finalize(env){
 ensure(env.GITHUB_ACTOR==='akhripunov'&&env.GITHUB_TRIGGERING_ACTOR==='akhripunov'
  &&env.GITHUB_REPOSITORY===REPO&&env.GITHUB_REF==='refs/heads/main','OWNER');
 const digest=env.RECOVERY_EVIDENCE_DIGEST;
 ensure(DIGEST.test(digest??''),'PUBLISHED_OCI_DIGEST');
 const file=exactFile(env);
 const bytes=fs.readFileSync(file);
 const proof=JSON.parse(bytes.toString('utf8'));
 const c=await context(env);
 ensure(proof.schema===1&&proof.operation==='INTERRUPTED_APPLY_INDEPENDENT_RECOVERY'
  &&proof.candidateSha===c.candidateSha&&proof.deploymentId===c.deploymentId
  &&proof.originalRunId===c.originalRunId&&proof.originalRunAttempt===c.identity.runAttempt
  &&proof.recoveryRunId===String(env.GITHUB_RUN_ID)
  &&proof.recoveryRunAttempt===String(env.GITHUB_RUN_ATTEMPT)&&proof.executorSha===env.GITHUB_SHA
  &&proof.originalFailure.statusId===c.statuses[0].id,'EXACT_RECOVERY_RECORD');
 const config=env.RECOVERY_REGISTRY_CONFIG;
 ensure(typeof config==='string'&&path.isAbsolute(config)&&fs.statSync(config).isFile(),'REGISTRY_AUTH');
 const ref=recoveryReference(env), repo='ghcr.io/akhripunov/gpoint-deploy-evidence';
 const run=(args)=>execFileSync('oras',args,{encoding:'utf8',timeout:60000}).trim();
 ensure(run(['resolve','--registry-config',config,ref])===digest,'PUBLISHED_DIGEST_READBACK');
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'gpoint-recovery-pull-'));
 try {
  run(['pull','--registry-config',config,repo+'@'+digest,'-o',directory]);
  ensure(same(fs.readdirSync(directory),[path.basename(file)])
   &&fs.readFileSync(path.join(directory,path.basename(file))).equals(bytes),'IMMUTABLE_OCI_BYTES');
 } finally {fs.rmSync(directory,{recursive:true,force:true});}
 const original=JSON.parse(proof.ciReceipt.prepareReceiptJson);
 const receipt=validateOciReceipt(original,c.candidateSha,{runId:c.prepareRunId,runAttempt:c.prepareRunAttempt});
 ensure(hash(Buffer.from(proof.ciReceipt.prepareReceiptJson,'utf8'))===c.identity.prepareReceiptSha256
  &&proof.ciReceipt.ghcrManifestDigest===c.identity.prepareReceiptDigest,'RECOVERED_PREPARE_IDENTITY');
 const now=inspect({receipt,identity:c.identity,candidateSha:c.candidateSha});
 ensure(same(now.fingerprint,proof.readbackFingerprint),'RECOVERY_VPS_CHANGED');
 const beforeWrite=await context(env);
 ensure(beforeWrite.statuses[0].id===c.statuses[0].id
  &&beforeWrite.deployment.created_at===c.deployment.created_at,'DEPLOYMENT_CHANGED_BEFORE_WRITE');
 const description=`stage=complete sha=${c.candidateSha.slice(0,12)} run=${c.identity.runId}/${c.identity.runAttempt} recovered=${env.GITHUB_RUN_ID}/${env.GITHUB_RUN_ATTEMPT}`;
 ensure(description.length<=140,'STATUS_LENGTH');
 await c.client.publishStatus(c.deployment,c.identity,{state:'success',description,logUrl:c.identity.logUrl});
 const readback=await c.client.latestStatus(c.deployment);
 ensure(readback.state==='success'&&readback.description===description&&readback.log_url===c.identity.logUrl,'STATUS_READBACK');
 console.log('INTERRUPTED_APPLY_RECOVERY=SUCCESS DEPLOYMENT_ID='+c.deploymentId);
}
if(import.meta.url===new URL('file://'+process.argv[1]).href){
 try{
  ensure(process.argv.length===3&&['--verify','--finalize'].includes(process.argv[2]),'CLI');
  if(process.argv[2]==='--verify')await verify(process.env);else await finalize(process.env);
 }catch(error){
  console.error('INTERRUPTED_APPLY_RECOVERY=BLOCKED '+(/^(RECOVERY_GATE=[A-Z0-9_]+)$/.exec(error?.message??'')?.[0]??'TYPE='+String(error?.name??'Error')));
  process.exitCode=1;
 }
}
