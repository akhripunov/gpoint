import type { CreatorProfileDTO, ExploreResponseDTO, CurrentUserDTO, PaginatedResponse, PostDTO, ReelDTO, VipClickResponseDTO, VipOfferDTO } from '../../../../packages/contracts/src/index.js';
import { markBootStage } from '../boot-diagnostics';
// Production serves the Mini App and API from one origin; Caddy reserves /api
// for the API while all other client routes resolve to the SPA. Keep the empty
// base during local Vite development so its existing API proxy remains used.
export const apiBase=import.meta.env.VITE_API_URL??(import.meta.env.DEV?'':'/api');
// Media delivery is mounted at the origin root (/media), while API requests
// use the /api prefix. Resolving a root-relative media path against `/api`
// makes `new URL()` throw in the browser because `/api` is not an absolute
// URL base. Keep media same-origin in production and local development.
export const mediaUrl=(path:string)=>new URL(path,window.location.origin).toString();
export class ApiError extends Error{constructor(public status:number,message:string){super(message)}}
let token=localStorage.getItem('gpoint.session'); export const getToken=()=>token;
async function request<T>(path:string,init:RequestInit={},timeoutMs=15000){const headers=new Headers(init.headers);if(init.body!==undefined&&!(init.body instanceof FormData)&&!headers.has('content-type'))headers.set('content-type','application/json');if(token)headers.set('authorization',`Bearer ${token}`);const controller=new AbortController();const externalSignal=init.signal;if(externalSignal){if(externalSignal.aborted)controller.abort();else externalSignal.addEventListener('abort',()=>controller.abort(),{once:true})}const started=Date.now();const timeout=setTimeout(()=>controller.abort(),timeoutMs);const stage=path==='/me'?'ME':path.startsWith('/feed/for-you')?'FEED':null;if(stage==='ME')markBootStage('ME_REQUEST_STARTED');if(stage==='FEED')markBootStage('FEED_REQUEST_STARTED');try{const r=await fetch(`${apiBase}${path}`,{...init,headers,cache:'no-store',credentials:'same-origin',signal:controller.signal});let data:unknown;try{data=await r.json()}catch{data=null}console.info('[GPoint] API response',{path,status:r.status,latencyMs:Date.now()-started});if(!r.ok)throw new ApiError(r.status,(data as {message?:string})?.message??'Request failed');if(stage==='ME')markBootStage('ME_SUCCESS');if(stage==='FEED')markBootStage('FEED_SUCCESS',{itemCount:Array.isArray((data as any)?.items)?(data as any).items.length:undefined});return data as T}catch(error){if(error instanceof DOMException&&error.name==='AbortError')throw new ApiError(408,'Request timed out');throw error}finally{clearTimeout(timeout)}}
type TelegramWebApp={initData?:string;initDataUnsafe?:{user?:unknown};ready?:()=>void;version?:string;platform?:string;requestChat?:(requestId:string,callback?:(success:boolean)=>void)=>void;openTelegramLink?:(url:string)=>void};
function telegramWebApp(){return (window as Window&{Telegram?:{WebApp?:TelegramWebApp}}).Telegram?.WebApp}
function browserDevAuthEnabled(){return import.meta.env.VITE_DEV_BROWSER_AUTH==='true'&&['localhost','127.0.0.1','::1'].includes(window.location.hostname.toLowerCase())}
function telegramInitDataFromLaunchUrl(){for(const raw of [window.location.hash.replace(/^#/,'').replace(/^\?/,'') ,window.location.search.replace(/^\?/,'')]){if(!raw)continue;const value=new URLSearchParams(raw).get('tgWebAppData')?.trim();if(value)return value}return null}
function telegramBootstrapState(){const webApp=telegramWebApp();return `web_app=${Boolean(webApp)};sdk_init_data=${Boolean(webApp?.initData?.trim())};launch_init_data=${Boolean(telegramInitDataFromLaunchUrl())}`}
function reportTelegramBootstrapFailure(){void fetch(`${apiBase}/auth/telegram`,{method:'POST',headers:{'content-type':'application/json','x-gpoint-telegram-bootstrap':telegramBootstrapState()},body:'{}',cache:'no-store',credentials:'same-origin'}).catch(()=>undefined)}
async function telegramInitData(){const dev=import.meta.env.VITE_DEV_AUTH_MODE==='true';for(let attempt=0;attempt<100;attempt+=1){const webApp=telegramWebApp();if(attempt===0)webApp?.ready?.();const initData=webApp?.initData?.trim()||telegramInitDataFromLaunchUrl();if(initData){console.info('[GPoint] telegram bootstrap',{telegram_init_data_present:true,telegram_init_data_length:initData.length,telegram_user_present:!!webApp?.initDataUnsafe?.user});return initData}if(!dev)await new Promise(resolve=>setTimeout(resolve,100));}console.info('[GPoint] telegram bootstrap',{telegram_init_data_present:false,telegram_init_data_length:0,telegram_user_present:false,telegram_webapp_present:!!telegramWebApp()});return dev?'dev':null}
export async function auth(){if(browserDevAuthEnabled()){token=null;localStorage.removeItem('gpoint.session');console.info('[GPoint] local browser dev auth');const e2eUserId=new URLSearchParams(window.location.search).get('e2eUserId')??undefined;const data=await request<{accessToken:string}>('/auth/browser',{method:'POST',body:JSON.stringify(e2eUserId?{testKey:e2eUserId}:undefined)});token=data.accessToken;localStorage.setItem('gpoint.session',token);return me()}const initData=await telegramInitData();const dev=import.meta.env.VITE_DEV_AUTH_MODE==='true';const devTelegramUserId=new URLSearchParams(window.location.search).get('e2eUserId')??undefined;if(!initData&&!dev){reportTelegramBootstrapFailure();throw new ApiError(401,'Telegram authorization unavailable')}if(!initData)return null;token=null;localStorage.removeItem('gpoint.session');console.info('[GPoint] auth request',{endpoint:'/auth/telegram',telegram_init_data_present:initData!=='dev'});const data=await request<{accessToken:string}>('/auth/telegram',{method:'POST',headers:{'x-gpoint-telegram-bootstrap':telegramBootstrapState()},body:JSON.stringify({initData,devTelegramUserId})});token=data.accessToken;localStorage.setItem('gpoint.session',token);console.info('[GPoint] auth response',{status:200,token_issued:true});return me()}
export const me=()=>request<CurrentUserDTO>('/me');
export const explore=(tab:string,cursor?:string)=>request<ExploreResponseDTO>(`/explore?mode=${tab.toLowerCase().replaceAll(' ','-')}${cursor?`&cursor=${encodeURIComponent(cursor)}`:''}`);
export const following=()=>request<any>('/following');
export const engagement=(eventType:string,entityType?:string,entityId?:string,surface?:string)=>request<any>('/events',{method:'POST',body:JSON.stringify({eventType,entityType,entityId,surface})});
export const preferences=()=>request<any>('/preferences');
export const setPreference=(preferenceType:string,entityType:string,entityId:string)=>request<any>('/preferences',{method:'POST',body:JSON.stringify({preferenceType,entityType,entityId})});
export const removePreference=(preferenceType:string,entityType:string,entityId:string)=>request<any>(`/preferences/${preferenceType}/${entityType}/${entityId}`,{method:'DELETE'});
export const resetPreferences=()=>request<any>('/preferences/reset',{method:'POST'});
export const feedForYou=(cursor?:string)=>request<any>(`/feed/for-you${cursor?`?cursor=${encodeURIComponent(cursor)}`:''}`);
export const feedFollowing=(cursor?:string)=>request<any>(`/feed/following${cursor?`?cursor=${encodeURIComponent(cursor)}`:''}`);
export const likePost=(id:string)=>request<any>(`/feed/${id}/like`,{method:'POST'});
export const unlikePost=(id:string)=>request<any>(`/feed/${id}/like`,{method:'DELETE'});
export const telegramChannel=()=>request<any>('/creator/me/telegram-channel');
export const telegramWebAppInfo=()=>{const webApp=telegramWebApp();return {version:webApp?.version??'unknown',platform:webApp?.platform??'unknown',requestChatPresent:typeof webApp?.requestChat==='function'};};
const MIN_TELEGRAM_WEBAPP_VERSION='9.6';
function supportsNativeChannelSelection(){const version=telegramWebApp()?.version??'';const parts=version.split('.').map(Number);return parts.length>=2&&Number.isFinite(parts[0])&&Number.isFinite(parts[1])&&(parts[0]>9||(parts[0]===9&&parts[1]>=6));}
type PreparedChannelSelection={requestId:string;challengeId:string;expiresAt:string;supported:boolean};
let prepareInFlight:Promise<PreparedChannelSelection>|null=null;
let preparedChannelSelection:PreparedChannelSelection|null=null;
let prepareAttemptCount=0;
const prepareLifecycleId=typeof crypto!=='undefined'&&'randomUUID' in crypto?crypto.randomUUID():'local-selection-lifecycle';
export function resetPreparedChannelSelection(){preparedChannelSelection=null;}
export async function prepareCreatorChannelSelection(){if(preparedChannelSelection&&Date.parse(preparedChannelSelection.expiresAt)>Date.now())return preparedChannelSelection;if(prepareInFlight)return prepareInFlight;const started=Date.now();const attempt=++prepareAttemptCount;const controller=new AbortController();console.info('[GPoint] creator channel prepare started',{...telegramWebAppInfo(),onboardingLifecycleId:prepareLifecycleId,prepareAttemptCount:attempt,startedAt:new Date(started).toISOString()});const operation=(async()=>{try{const result=await request<PreparedChannelSelection>('/creator/channel/selection/prepare',{method:'POST',signal:controller.signal},12000);preparedChannelSelection=result;console.info('[GPoint] creator channel prepared',{onboardingLifecycleId:prepareLifecycleId,prepareAttemptCount:attempt,status:201,latencyMs:Date.now()-started,preparedRequestCreated:true,preparedRequestIdPresent:!!result.requestId});return result}catch(error){console.info('[GPoint] creator channel prepare failed',{onboardingLifecycleId:prepareLifecycleId,prepareAttemptCount:attempt,status:error instanceof ApiError?error.status:'unknown',latencyMs:Date.now()-started,preparedRequestCreated:false,preparedRequestIdPresent:false});throw error}})();const watchdog=new Promise<PreparedChannelSelection>((_,reject)=>setTimeout(()=>{controller.abort();reject(new ApiError(408,'Could not prepare channel selector'))},12000));prepareInFlight=Promise.race([operation,watchdog]).finally(()=>{prepareInFlight=null});return prepareInFlight;}
export const creatorChannelSelectionStatus=(challengeId:string)=>request<any>(`/creator/channel/selection/${encodeURIComponent(challengeId)}`);
export const completeCreatorChannelSelection=(challengeId:string)=>request<any>('/creator/channel/selection/complete',{method:'POST',body:JSON.stringify({challengeId})});
export const startTelegramChannelConnection=()=>request<any>('/creator/channel/start',{method:'POST'});
export const prepareCreatorChannelReverification=()=>request<PreparedChannelSelection>('/creator/channel/reverify',{method:'POST'});
export const telegramChannelConnectionStatus=(challengeId:string)=>request<any>(`/creator/channel/start/${encodeURIComponent(challengeId)}`);
export const openTelegramStartChannel=()=>{const url='https://t.me/thegpointbot?startchannel&admin=post_messages';const webApp=telegramWebApp();if(typeof webApp?.openTelegramLink!=='function')throw new ApiError(426,'Update Telegram to connect a channel.');webApp.openTelegramLink(url);};
export function requestCreatorTelegramChannel(requestId:string){const info=telegramWebAppInfo();console.info('[GPoint] requestChat attempt',{telegramVersion:info.version,telegramPlatform:info.platform,requestChatFunctionPresent:info.requestChatPresent,requestChatReqIdPresent:!!requestId});if(!supportsNativeChannelSelection())throw new ApiError(426,`Update Telegram to connect a channel. Requires Telegram ${MIN_TELEGRAM_WEBAPP_VERSION}.`);if(!requestId)throw new ApiError(409,'Channel selector is not ready.');const webApp=telegramWebApp();return new Promise<boolean>(resolve=>{let called=false;const callback=(success:boolean)=>{console.info('[GPoint] requestChat callback',{callbackCalled:true,result:success?'SENT':'CANCELLED'});resolve(success)};try{called=true;webApp!.requestChat!(requestId,callback)}catch(error){console.info('[GPoint] requestChat failed',{callbackCalled:false,result:'FAILED'});throw error}if(!called)console.info('[GPoint] requestChat failed',{callbackCalled:false,result:'FAILED'});});}
export const connectTelegramChannel=(body:any)=>request<any>('/creator/me/telegram-channel',{method:'POST',body:JSON.stringify(body)});
export const updateTelegramChannel=(body:any)=>request<any>('/creator/me/telegram-channel',{method:'PATCH',body:JSON.stringify(body)});
export const disconnectTelegramChannel=()=>request<any>('/creator/me/telegram-channel',{method:'DELETE'});
export const creator=(slug:string)=>request<CreatorProfileDTO>(`/creators/${encodeURIComponent(slug)}`);
export const posts=(id:string,cursor?:string)=>request<PaginatedResponse<PostDTO>>(`/creators/${id}/posts${cursor?`?cursor=${encodeURIComponent(cursor)}`:''}`);
export const reels=(cursor?:string)=>request<PaginatedResponse<ReelDTO>>(`/reels${cursor?`?cursor=${encodeURIComponent(cursor)}`:''}`);
export const follow=(id:string,following:boolean)=>request<{following:boolean}>(`/creators/${id}/follow`,{method:following?'POST':'DELETE'});
export const vipOffer=(id:string)=>request<VipOfferDTO>(`/vip/creator/${id}`);
export const vipClick=(id:string,placement:string)=>request<VipClickResponseDTO>(`/vip/${id}/click`,{method:'POST',body:JSON.stringify({placement})});
export const creatorMe=()=>request<{creator:any;vip:any}>('/creator/me');
export const creatorOnboarding=(body:any)=>request<{creator:any}>('/creator/onboarding',{method:'POST',body:JSON.stringify(body)});
export const creatorSubmit=()=>request<{status:string}>('/creator/me/submit',{method:'POST'});
export const creatorAnalytics=()=>request<any>('/creator/me/analytics');
export const creatorPosts=()=>request<any>('/creator/me/posts');
export const creatorPostCreate=(body:any)=>request<any>('/creator/me/posts',{method:'POST',body:JSON.stringify(body)});
export const creatorPostUpdate=(id:string,body:any)=>request<any>(`/creator/me/posts/${id}`,{method:'PATCH',body:JSON.stringify(body)});
export const creatorReelUpdate=(id:string,body:any)=>request<any>(`/creator/me/posts/${id}`,{method:'PATCH',body:JSON.stringify(body)});
export const creatorReels=()=>request<any>('/creator/me/reels');
export const creatorReelCreate=(body:any)=>request<any>('/creator/me/reels',{method:'POST',body:JSON.stringify(body)});
export const creatorVip=()=>request<any>('/creator/me/vip');
export const creatorVipUpdate=(body:any)=>request<any>('/creator/me/vip',{method:'PATCH',body:JSON.stringify(body)});
export const adminCreators=()=>request<any[]>('/admin/creators');
export const adminModerateCreator=(id:string,action:string,reason?:string)=>request<any>(`/admin/creators/${id}/${action}`,{method:'POST',body:JSON.stringify({reason})});
export const adminModeratePost=(id:string,action:string,reason?:string)=>request<any>(`/admin/posts/${id}/${action}`,{method:'POST',body:JSON.stringify({reason})});
export const adminPosts=()=>request<any[]>('/admin/posts');
export const adminVip=()=>request<any[]>('/admin/vip');
export const adminModerateVip=(id:string,action:string,reason?:string)=>request<any>(`/admin/vip/${id}/${action}`,{method:'POST',body:JSON.stringify({reason})});
export const creatorMedia=()=>request<{items:any[]}>('/creator/me/media');
export const creatorMediaIntent=(body:any)=>request<any>('/creator/me/media/intent',{method:'POST',body:JSON.stringify(body)});
export const creatorMediaFinalize=(id:string)=>request<any>(`/creator/me/media/${id}/finalize`,{method:'POST'});
export const creatorMediaDelete=(id:string)=>request<any>(`/creator/me/media/${id}`,{method:'DELETE'});
export const creatorProfileUpdate=(body:any)=>request<any>('/creator/me/profile',{method:'PATCH',body:JSON.stringify(body)});
