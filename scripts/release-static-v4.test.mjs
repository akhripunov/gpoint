import assert from 'node:assert/strict';
import test from 'node:test';
import { expectedStaticParent, staticReceiptAndImages, verifyStaticLiveBaseline } from './release-static-v4.mjs';
const A = 'a'.repeat(40), B = 'b'.repeat(40), D=d=>'sha256:'+d.repeat(64);
const state = {currentProductionSha:A,activeRuntimeSha:A,agataRuntimeSha:A,miniappSourceSha:A,
 currentProductionDbHead:'20260922090000_telegram_channel_avatar',recoveryRequired:false,
 lastReleaseStatus:'COMPLETE',serverDeployStatus:'COMPLETE',releaseOrigin:'durable-production-release',
 componentImages:{app:{sourceSha:A,imageId:D('1'),platform:'linux/amd64'},agata:{sourceSha:A,imageId:D('2'),platform:'linux/amd64'},gsales:{sourceSha:A,imageId:D('3'),platform:'linux/amd64'}}};
const receipt={schema:2,route:'STATIC_UI_ONLY',candidateSha:B,productionBaselineSha:A,agataHead:'agata0011',
gsalesMigrationHead:'001_gsales',gsalesMigrationSha256:'c'.repeat(64)};
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
 const next={...receipt,candidateSha:'d'.repeat(40),productionBaselineSha:B};
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
