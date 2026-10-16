import test from 'node:test';
import assert from 'node:assert/strict';
const policy=await import('../src/messages-unread-policy.js').catch(()=>({}));

test('badges hide zero, cap visual text and keep exact counts available separately',()=>{
  assert.equal(typeof policy.badgeText,'function','unread badge policy missing');
  for(const [n,want] of [[0,null],[1,'1'],[99,'99'],[100,'99+']])assert.equal(policy.badgeText(n),want);
});
test('system notices are displayed separately and never enter assistant read acknowledgements',()=>{
  assert.equal(typeof policy.chatMessagePresentation,'function','chat message presentation policy missing');
  assert.deepEqual(policy.chatMessagePresentation('user'),{side:'mine',incoming:false});
  assert.deepEqual(policy.chatMessagePresentation('assistant'),{side:'theirs',incoming:true});
  assert.deepEqual(policy.chatMessagePresentation('system'),{side:'system',incoming:false});
});
test('polling spreads clients and backs off failures without unbounded retry delay',()=>{
  assert.equal(typeof policy.initialPollDelay,'function','unread polling policy missing');
  assert.equal(policy.initialPollDelay(()=>0),0);assert.equal(policy.initialPollDelay(()=>.9),2700);
  assert.equal(policy.unreadPollDelay(0),3000);assert.equal(policy.unreadPollDelay(1),3000);
  assert.equal(policy.unreadPollDelay(2),6000);assert.equal(policy.unreadPollDelay(20),60000);
});
test('read queue acknowledges only visible messages in batches and deduplicates concurrent flush',async()=>{
  assert.equal(typeof policy.createReadQueue,'function','visible read queue missing');
  let active=true;const calls=[];const timers=new Map();let next=0;
  const queue=policy.createReadQueue({isActive:()=>active,setTimer:fn=>{timers.set(++next,fn);return next},clearTimer:id=>timers.delete(id),acknowledge:async ids=>{calls.push(ids);return {acknowledgedMessageIds:ids}}});
  queue.view('offscreen',false);queue.view('visible',true);queue.view('visible',true);
  active=false;await queue.flush();assert.equal(calls.length,0);
  active=true;await Promise.all([queue.flush(),queue.flush()]);assert.deepEqual(calls,[['visible']]);
  queue.view('visible',true);await queue.flush();assert.equal(calls.length,1);
  for(let i=0;i<105;i++)queue.view('new-'+i,true);
  await queue.flush();await queue.flush();assert.deepEqual(calls.map(ids=>ids.length),[1,100,5]);
  queue.dispose();queue.view('disposed',true);await queue.flush();assert.equal(calls.length,3);
});
test('failed acknowledgements retry only while the message remains visible',async()=>{
  assert.equal(typeof policy.createReadQueue,'function','visible read queue missing');
  let attempts=0;const timers=[];
  const queue=policy.createReadQueue({isActive:()=>true,setTimer:(fn,delay)=>{timers.push({fn,delay});return timers.length},clearTimer:()=>{},acknowledge:async ids=>{attempts++;if(attempts===1)throw Error('offline');return {acknowledgedMessageIds:ids}}});
  queue.view('incoming',true);await queue.flush();assert.equal(attempts,1);
  assert.equal(timers.at(-1).delay,3000);
  queue.view('incoming',false);await queue.flush();assert.equal(attempts,1);
  queue.view('incoming',true);await queue.flush();assert.equal(attempts,2);
  queue.dispose();
});
