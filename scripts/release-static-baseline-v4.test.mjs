import test from 'node:test';
import assert from 'node:assert/strict';
import { expectedV3LiveRevisions } from './release-gpoint.mjs';
const A='a'.repeat(40),B='b'.repeat(40);
const id=d=>'sha256:'+d.repeat(64);
const roles={
  app:{sourceSha:A,imageId:id('1'),platform:'linux/amd64'},
  agata:{sourceSha:A,imageId:id('2'),platform:'linux/amd64'},
  gsales:{sourceSha:A,imageId:id('3'),platform:'linux/amd64'},
};
test('V3 baseline has exact-SHA legacy compatibility',()=>{
 const facts=expectedV3LiveRevisions({currentProductionSha:A});
 assert.deepEqual(facts,{runtimeSha:A,miniappSha:A,agataSha:A,mixed:false});
});
test('full pipeline after fast UI supports inherited runtime vs newer site',()=>{
 const state={currentProductionSha:B,miniappSourceSha:B,
  activeRuntimeSha:A,agataRuntimeSha:A,componentImages:roles,
  lastReleaseStatus:'COMPLETE',serverDeployStatus:'COMPLETE',recoveryRequired:false};
 assert.deepEqual(expectedV3LiveRevisions(state),
  {runtimeSha:A,miniappSha:B,agataSha:A,mixed:true});
 assert.throws(()=>expectedV3LiveRevisions({...state,agataRuntimeSha:B}));
 assert.throws(()=>expectedV3LiveRevisions({...state,recoveryRequired:true}));
});
