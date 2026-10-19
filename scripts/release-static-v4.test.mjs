import assert from 'node:assert/strict';
import test from 'node:test';
import { expectedStaticParent, staticReceiptAndImages, verifyStaticLiveBaseline } from './release-static-v4.mjs';
const A = 'a'.repeat(40), B = 'b'.repeat(40), D=d=>'sha256:'+d.repeat(64);
const state = {currentProductionSha:A,activeRuntimeSha:A,agataRuntimeSha:A,miniappSourceSha:A,
 currentProductionDbHead:'20260922090000_telegram_channel_avatar',recoveryRequired:false,
 lastReleaseStatus:'COMPLETE',serverDeployStatus:'COMPLETE',releaseOrigin:'durable-production-release',
 componentImages:{app:{sourceSha:A,imageId:D('1'),platform:'linux/amd64'},agata:{sourceSha:A,imageId:D('2'),platform:'linux/amd64'},gsales:{sourceSha:A,imageId:D('3'),platform:'linux/amd64'}}};
const receipt={schema:3,transport:'ghcr-oci',staticArtifactRoute:'V4_FAST_STATIC',route:'STATIC_UI_ONLY',
candidateSha:B,productionBaselineSha:A,agataHead:'agata0011',gsalesMigrationHead:'001_gsales',
gsalesMigrationSha256:'c'.repeat(64),runId:1,runAttempt:1,
staticArtifact:{sourceSha:B,kind:'gpoint-miniapp-static',runId:1,runAttempt:1}};
const facts={ LIVE_RELEASE_ENV_SHA:A,LIVE_MINIAPP_SHA:A,LIVE_PRISMA_HEAD:'20260922090000_telegram_channel_avatar',
LIVE_AGATA_HEAD:'agata0011',LIVE_GSALES_HEAD:'001_gsales',LIVE_GSALES_MIGRATION_SHA256:'c'.repeat(64),
LIVE_API_HEALTH:'healthy',LIVE_API_SHA:A,LIVE_BOT_SHA:A,LIVE_WORKER_SHA:A,
LIVE_API_RUNNING:'true',LIVE_BOT_RUNNING:'true',LIVE_WORKER_RUNNING:'true',
LIVE_AGATA_API_SHA:A,LIVE_AGATA_WORKER_SHA:A,LIVE_AGATA_API_HEALTH:'healthy',
LIVE_AGATA_WORKER_HEALTH:'healthy',LIVE_AGATA_API_RUNNING:'true',LIVE_AGATA_WORKER_RUNNING:'true'};
const native=Object.fromEntries(Object.entries(state.componentImages).map(([k,x])=>[k,{nativeImageId:x.imageId,revisionLabel:x.sourceSha}]));
test('static inherited receipt and live source proof',()=>{
 assert.equal(staticReceiptAndImages(receipt,state).route,'STATIC_UI_CANDIDATE');
 assert.equal(verifyStaticLiveBaseline(state,receipt,facts,native).images.agata.sourceSha,A);
});
test('mixed source labels are allowed if current release SHA differs from images',()=>{
 const prev={...state,currentProductionSha:B,miniappSourceSha:B};
 const next={...receipt,candidateSha:'d'.repeat(40),productionBaselineSha:B,
  staticArtifact:{...receipt.staticArtifact,sourceSha:'d'.repeat(40)}};
 const mixFacts={...facts,LIVE_RELEASE_ENV_SHA:A,LIVE_MINIAPP_SHA:B};
 assert.equal(verifyStaticLiveBaseline(prev,next,mixFacts,native).images.app.sourceSha,A);
});
test('baseline mismatch/drift fails closed',()=>{
 assert.throws(()=>verifyStaticLiveBaseline(state,receipt,{...facts,LIVE_API_SHA:B},native));
 assert.throws(()=>verifyStaticLiveBaseline(state,receipt,{...facts,LIVE_GSALES_HEAD:'ABSENT'},native));
 assert.throws(()=>verifyStaticLiveBaseline(state,receipt,facts,{...native,gsales:{...native.gsales,nativeImageId:D('4')}}));
 assert.throws(()=>staticReceiptAndImages(receipt,{...state,recoveryRequired:true}));
 assert.throws(()=>staticReceiptAndImages({...receipt,productionBaselineSha:'d'.repeat(40)},state));
});
test('V5-looking local state cannot become a V4 parent without verified Deployment and VPS evidence',()=>{
 const v5={...state,releaseOrigin:'github-deployment',componentImages:Object.fromEntries(
  Object.entries(state.componentImages).map(([role,image])=>[role,{...image,registryDigest:D('4')}]))};
 assert.throws(()=>expectedStaticParent(v5),/Deployment|evidence/);
 assert.throws(()=>expectedStaticParent({...v5,recoveryRequired:false}),/Deployment|evidence/);
});
test('V5 full deployment is a V4 UI-only parent only after exact GitHub, GHCR and VPS read-back',()=>{
 const v5={...state,releaseOrigin:'github-deployment',lastReleaseStatus:'COMPLETE',serverDeployStatus:'COMPLETE',
  activeRuntimeSha:A,agataRuntimeSha:A,currentProductionDbHead:'prisma-1',agataAlembicHead:'agata0011',
  gsalesDbHead:'001_gsales',gsalesMigrationSha256:'c'.repeat(64),
  componentImages:Object.fromEntries(Object.entries(state.componentImages).map(([role,image],i)=>
   [role,{...image,registryDigest:D(String(i+4))}]))};
 const receiptDigest=D('7');
 const evidence={
  deployment:{source:'github-api',sha:A,status:'success',environment:'production',task:'gpoint-production-deploy',
   productionEnvironment:true,transientEnvironment:false,runId:'10',runAttempt:'1',prepareRunId:'9',prepareRunAttempt:'1',
   authorizationMode:'TASK_SCOPED_AUTO',authorizationId:'me42-owner-cycle-1',receiptDigest,
   imageDigests:{app:D('4'),agata:D('5'),gsales:D('6')}},
  prepare:{source:'verified-ghcr-receipt',candidateSha:A,runId:'9',runAttempt:'1',receiptDigest,
   prismaHead:'prisma-1',agataHead:'agata0011',gsalesMigrationHead:'001_gsales',gsalesMigrationSha256:'c'.repeat(64),
   images:{app:{manifestDigest:D('4'),configDigest:D('1'),nativeId:D('4'),sourceSha:A},
    agata:{manifestDigest:D('5'),configDigest:D('2'),nativeId:D('5'),sourceSha:A},
    gsales:{manifestDigest:D('6'),configDigest:D('3'),nativeId:D('6'),sourceSha:A}}},
  live:{source:'read-only-vps',runtimeSha:A,miniappSha:A,prismaHead:'prisma-1',agataHead:'agata0011',
   gsalesHead:'001_gsales',gsalesMigrationSha256:'c'.repeat(64),apiHealth:'healthy',agataApiHealth:'healthy',
   gsalesApiHealth:'healthy',gsalesAgataApiHealth:'healthy',gsalesAgataWorkerHealth:'healthy',
   workerRunning:true,botRunning:true,agataWorkerRunning:true,gsalesAgataWorkerRunning:true,
   nativeImages:{app:{imageId:D('4'),sourceSha:A,platform:'linux/amd64'},
    agata:{imageId:D('5'),sourceSha:A,platform:'linux/amd64'},
    gsales:{imageId:D('6'),sourceSha:A,platform:'linux/amd64'}}},
  unresolvedRecovery:false,
 };
 const manifest=staticReceiptAndImages(receipt,v5,evidence);
 assert.equal(manifest.images.app.imageId,D('1'));
 assert.equal(manifest.images.app.registryDigest,D('4'));
 const v5Facts={...facts,LIVE_PRISMA_HEAD:'prisma-1',LIVE_AGATA_HEAD:'agata0011',
  LIVE_GSALES_HEAD:'001_gsales',LIVE_GSALES_MIGRATION_SHA256:'c'.repeat(64)};
 const v5Native=Object.fromEntries(Object.entries(v5.componentImages).map(([role,image],i)=>[
   role,{nativeImageId:D(String(i+4)),revisionLabel:image.sourceSha}]));
 assert.equal(verifyStaticLiveBaseline(v5,receipt,v5Facts,v5Native,evidence).route,'STATIC_UI_CANDIDATE');
});
