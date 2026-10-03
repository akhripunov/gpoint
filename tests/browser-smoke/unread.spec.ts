import {expect,test,type Page} from '@playwright/test';
import {installMiniAppBrowserMocks} from './miniapp-mocks';

async function unreadFixture(page:Page, options:{count?:number;other?:number;long?:boolean;failReads?:number;staleInbox?:boolean}={}) {
  const {errors}=await installMiniAppBrowserMocks(page,{chatHistoryAccepted:true});
  const count=options.count??2;
  const rows=Array.from({length:count},(_,i)=>({id:`2396112c-8425-440b-890a-${String(i).padStart(12,'0')}`,role:'assistant',text:`Incoming ${i} ${options.long?'Long visible text. '.repeat(90):''}`,occurredAt:new Date().toISOString(),incomingReadAt:null as string|null}));
  const state={summaryCalls:0,inboxCalls:0,reads:[] as string[][],failReads:options.failReads??0,holdNext:false,held:false,release:()=>{},hidden:false,other:options.other??0};
  await page.route('http://127.0.0.1:5199/messages**',async route=>{
    if(route.request().method()==='OPTIONS'){await route.fallback();return;}
    const path=new URL(route.request().url()).pathname;
    const headers={'access-control-allow-origin':'http://127.0.0.1:5189','access-control-allow-credentials':'true'};
    const n=rows.filter(r=>r.incomingReadAt===null).length;
    let data:any;let status=200;
    if(path==='/messages/unread'){
      state.summaryCalls++;data={totalUnread:n+state.other,items:[...(n?[{creatorId:'browser-smoke-model-id',unreadCount:n}]:[]),...(state.other?[{creatorId:'other',unreadCount:state.other}]:[])]};
      if(state.holdNext){state.holdNext=false;state.held=true;await new Promise<void>(resolve=>{state.release=resolve;});}
    }else if(path==='/messages/state')data={enabled:true,chatHistory:true,onboardingCompleted:true,disclosureRequired:false,longTermMemory:false,memoryOptIn:false,aiAvailable:true};
    else if(path==='/messages'){state.inboxCalls++;data={items:[{creator:{id:'browser-smoke-model-id',slug:'models_hub',displayName:'Models Hub',avatarUrl:null,verified:true},lastMessagePreview:'Incoming',lastMessageRole:'assistant',lastMessageAt:new Date().toISOString(),unreadCount:options.staleInbox?count:n}],nextCursor:null};}
    else if(path.endsWith('/read')){
      const ids=route.request().postDataJSON().messageIds;state.reads.push(ids);
      if(state.failReads-->0){status=503;data={code:'MESSAGES_TEMPORARILY_UNAVAILABLE'};}
      else {for(const row of rows)if(ids.includes(row.id))row.incomingReadAt=new Date().toISOString();data={acknowledgedMessageIds:ids};}
    }else if(path==='/messages/creator/browser-smoke-model-id')data={creator:{id:'browser-smoke-model-id',slug:'models_hub',displayName:'Models Hub'},items:rows,nextCursor:null};
    else{await route.fallback();return;}
    try{await route.fulfill({status,headers,contentType:'application/json',body:JSON.stringify(data)});}catch(error){if(!route.request().failure())throw error;}
  });
  return {state,rows,errors};
}

test('unread count updates outside Messages and background polling pauses',async({page})=>{
  const {state}=await unreadFixture(page,{count:1});
  await page.goto('/');await expect(page.locator('.bottom-nav .messages-unread-badge')).toHaveText('1');
  await page.clock.install();
  await page.evaluate(()=>{Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});document.dispatchEvent(new Event('visibilitychange'));});
  const calls=state.summaryCalls;
  await page.clock.fastForward(12000);expect(state.summaryCalls).toBe(calls);
  await page.evaluate(()=>{Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});document.dispatchEvent(new Event('visibilitychange'));});
  await expect.poll(()=>state.summaryCalls).toBeGreaterThan(calls);
  expect(state.reads).toEqual([]);
});

test('offscreen incoming rows stay unread until scrolled into view',async({page})=>{
  const {state,rows}=await unreadFixture(page,{count:12,long:true});
  await page.goto('/messages/models_hub');
  await expect(page.locator('.chat-bubble')).toHaveCount(12);
  await expect.poll(()=>state.reads.flat().length).toBeGreaterThan(0);
  expect(new Set(state.reads.flat()).size).toBeLessThan(12);
  expect(rows[0].incomingReadAt).toBeNull();
  await page.locator('.chat-history').evaluate(el=>{el.scrollTop=0;});
  await expect.poll(()=>rows[0].incomingReadAt).not.toBeNull();
});

test('hidden chat does not acknowledge messages and read failures retry without clearing early',async({page})=>{
  const {state,rows}=await unreadFixture(page,{count:1,failReads:1});
  await page.addInitScript(()=>Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'}));
  await page.goto('/messages/models_hub');
  await expect(page.getByRole('textbox',{name:'Message'})).toBeVisible();
  expect(state.reads).toEqual([]);
  await page.evaluate(()=>{Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});document.dispatchEvent(new Event('visibilitychange'));});
  await expect.poll(()=>state.reads.length).toBe(1);
  expect(rows[0].incomingReadAt).toBeNull();
  await expect.poll(()=>rows[0].incomingReadAt,{timeout:7000}).not.toBeNull();
  expect(state.reads).toHaveLength(2);
});

test('late polling response cannot restore a badge after a successful read',async({page})=>{
  const {state}=await unreadFixture(page,{count:1});
  await page.goto('/messages');await expect(page.locator('.bottom-nav .messages-unread-badge')).toHaveText('1');
  await page.clock.install();state.holdNext=true;await page.clock.fastForward(3500);
  await expect.poll(()=>state.held).toBe(true);
  const heldCalls=state.summaryCalls;await page.clock.fastForward(9000);expect(state.summaryCalls).toBe(heldCalls);
  await page.getByRole('button',{name:/Models Hub/}).click();
  await page.clock.fastForward(1000);await expect.poll(()=>state.reads.length).toBe(1);
  await page.getByRole('button',{name:'Back to messages'}).click();
  await expect(page.locator('.bottom-nav .messages-unread-badge')).toHaveCount(0);
  state.release();
  await page.clock.fastForward(3500);
  await expect(page.locator('.bottom-nav .messages-unread-badge')).toHaveCount(0);
});

test('authoritative zero summary overrides a stale Inbox snapshot',async({page})=>{
  const {state}=await unreadFixture(page,{count:1,staleInbox:true});
  await page.goto('/messages');await expect(page.locator('.bottom-nav .messages-unread-badge')).toHaveText('1');
  await page.getByRole('button',{name:/Models Hub/}).click();await expect.poll(()=>state.reads.length).toBe(1);
  await page.getByRole('button',{name:'Back to messages'}).click();
  await expect(page.locator('.messages-thread .messages-unread-badge')).toHaveCount(0);
});

for(const [width,height] of [[390,844],[360,800],[393,852],[430,932]])test(`unread 99+ badge remains accessible and unclipped at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height});const {errors}=await unreadFixture(page,{count:100});
  await page.goto('/messages');await expect(page.locator('.bottom-nav .messages-unread-badge')).toHaveText('99+');
  await expect(page.locator('.bottom-nav').getByRole('button',{name:'Messages, 100 unread messages'})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  for(const badge of await page.locator('.messages-unread-badge').all()){
    const box=await badge.boundingBox();expect(box).not.toBeNull();expect(box!.x).toBeGreaterThanOrEqual(0);expect(box!.x+box!.width).toBeLessThanOrEqual(width);
  }
  await expect(page).toHaveTitle(/GPoint/);
  await expect(page.locator('vite-error-overlay')).toHaveCount(0);expect(errors).toEqual([]);
  await page.screenshot({path:`test-results/browser-smoke/unread-${width}-99plus.png`});
});
