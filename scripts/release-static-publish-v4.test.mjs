import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { applyStaticArtifactV4 } from './release-static-transfer-v4.mjs';
import { classifyReleaseFailure } from './release-gpoint.mjs';

const A='a'.repeat(40),B='b'.repeat(40);
function fixture(t, failAt = '') {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'v5-static-failure-'));
  t.after(()=>fs.rmSync(root,{force:true,recursive:true}));
  fs.mkdirSync(path.join(root,'scripts'));
  for(const name of ['release_static_executor_v4.py','release_static_exchange_v4.py','release_ui_artifact_v4.py'])
    fs.writeFileSync(path.join(root,'scripts',name),'# source fixture');
  const archive=path.join(root,'miniapp.zip');
  fs.writeFileSync(archive,'synthetic verified archive');
  let live=A;
  const seen=[];
  const inputs={
    candidate:B,baseline:A,
    bundle:{candidateSha:B,productionBaselineSha:A,staticOnly:true,archive},
    images:Object.fromEntries(['app','agata','gsales'].map(role=>[role,{sourceSha:A}])),
    sourceRoot:root,
    upload:(from)=>{if(!fs.existsSync(from))throw Error('missing upload source');seen.push('upload')},
    runRemote:(command)=>{
      const step=command.match(/\.py (stage|switch|update-env|rollback|complete) /)?.[1];
      if(!step) {
        if(command.includes('GPOINT_V4_RUNTIME_ENV_UNCHANGED')) return 'GPOINT_V4_RUNTIME_ENV_UNCHANGED=PASS';
        if(command.includes('mkdir -p ')) return 'mkdir ok';
        throw Error('unexpected remote command');
      }
      seen.push(step);
      if(step===failAt) throw Error('synthetic failure at '+step);
      if(step==='switch')live=B;
      if(step==='rollback')live=A;
      const state={stage:'STAGED',switch:'SWITCHED',rollback:'ROLLED_BACK',complete:'COMPLETE'}[step];
      return 'GPOINT_V4_STATIC_TRANSACTION='+JSON.stringify({candidateSha:B,...(step==='stage'?{status:state}:{phase:state})});
    },
    healthCheck:(expected)=>{seen.push('health:'+expected);assert.equal(live,expected)},
    nativeProof:()=>seen.push('native'),
    onSwitchStart:()=>seen.push('switchStarted'),
  };
  return {inputs,seen,current:()=>live};
}

test('stage failure is pre-mutation and never reports a completed transaction',t=>{
  const f=fixture(t,'stage');
  assert.throws(()=>applyStaticArtifactV4(f.inputs),/synthetic failure at stage/);
  assert.equal(f.seen.includes('switchStarted'),false);
  assert.equal(f.seen.includes('rollback'),false);
  assert.equal(f.current(),A);
  assert.equal(classifyReleaseFailure({switchStarted:false}).releaseState,'FAILED_PRE_APPLY');
});

test('post-switch failure is terminal only when rollback and baseline proofs pass',t=>{
  const f=fixture(t,'complete');
  assert.throws(()=>applyStaticArtifactV4(f.inputs),error=>error.switchStarted===true && error.rollbackProven===true);
  assert.equal(f.current(),A);
  assert.ok(f.seen.includes('rollback'));
  assert.notEqual(f.seen.at(-1),'complete');
});

test('uncertain switch or rollback proof remains recovery-required and installation-unconfirmed',t=>{
  const f=fixture(t,'complete');
  f.inputs.healthCheck=(expected)=>{
    if(expected===B) throw Error('synthetic candidate health failure');
    if(expected===A) throw Error('synthetic baseline health unavailable');
  };
  assert.throws(()=>applyStaticArtifactV4(f.inputs),error=>error.switchStarted===true && error.rollbackProven===false);
  const failure=classifyReleaseFailure({switchStarted:true});
  assert.equal(failure.releaseState,'INSTALLATION_UNCONFIRMED');
  assert.equal(failure.failureStage,'POST_SWITCH');
  assert.notEqual(f.current(),B);
});
