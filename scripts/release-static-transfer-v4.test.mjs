import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { applyStaticArtifactV4 } from './release-static-transfer-v4.mjs';

const A='a'.repeat(40),B='b'.repeat(40);
function fake(t, failAt = '') {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'v4-static-transfer-'));
  t.after(()=>fs.rmSync(root,{force:true,recursive:true}));
  const scripts=path.join(root,'scripts');
  fs.mkdirSync(scripts);
  for(const name of ['release_static_executor_v4.py','release_static_exchange_v4.py','release_ui_artifact_v4.py'])
    fs.writeFileSync(path.join(scripts,name),'# checked-in source fixture');
  const zip=path.join(root,'miniapp.zip');
  fs.writeFileSync(zip,'verified immutable placeholder');
  let phase='BASELINE',site=A;
  const seen=[];
  return {
    seen, current:()=>site,
    inputs: {
      candidate:B,baseline:A,bundle:{candidateSha:B,productionBaselineSha:A,
        staticOnly:true,archive:zip},
      images:Object.fromEntries(['app','agata','gsales'].map(x=>[x,{sourceSha:A}])),
      sourceRoot:root,
      upload:(from,to)=>{seen.push('upload');if(!fs.existsSync(from))throw Error('not found')},
      runRemote:(cmd)=>{
        if(cmd.includes('GPOINT_V4_RUNTIME_ENV_UNCHANGED')) return 'GPOINT_V4_RUNTIME_ENV_UNCHANGED=PASS';
        if(cmd.includes('mkdir -p ')) return 'mkdir ok';
        const match=cmd.match(/\.py (stage|switch|update-env|rollback|complete) /);
        if(!match)throw Error('bad remote command');
        const step=match[1];
        if(step === failAt)throw Error('simulation fails at '+step);
        seen.push(step);
        if(step==='switch')site=B;
        if(step==='rollback')site=A;
        const status={stage:'STAGED',switch:'SWITCHED','update-env':'ENV_UPDATED',
          rollback:'ROLLED_BACK',complete:'COMPLETE'}[step];
        return 'GPOINT_V4_STATIC_TRANSACTION='+JSON.stringify({candidateSha:B,
          ...(step==='stage'?{status}:{phase:status})});
      },
      healthCheck:(expected)=>{seen.push('health:'+expected);assert.equal(site,expected)},
      nativeProof:()=>{seen.push('native')},
      onSwitchStart:()=>{seen.push('switchStarted')},
    }
  };
}
test('static transfer applies only archive, checks no-container restart and finalizes',t=>{
  const f=fake(t);
  const outcome=applyStaticArtifactV4(f.inputs);
  assert.equal(outcome.status,'COMPLETE');
  assert.equal(outcome.containersRestarted,false);
  assert.equal(f.current(),B);
  assert.deepEqual(f.seen.filter(x=>x==='switch'),['switch']);
  assert.ok(!f.seen.includes('rollback'));
});
test('failed switch after preparation has safe rollback',t=>{
  const f=fake(t,'switch');
  assert.throws(()=>applyStaticArtifactV4(f.inputs),err=>err.switchStarted===true && err.rollbackProven===true);
  assert.equal(f.current(),A);
});
test('uncertain rollback requires durable recovery',t=>{
  const f=fake(t,'rollback');
  f.inputs.healthCheck=(expected)=>{if(expected===B)throw Error('public health failure')};
  assert.throws(()=>applyStaticArtifactV4(f.inputs),err=>err.rollbackProven===false);
});
