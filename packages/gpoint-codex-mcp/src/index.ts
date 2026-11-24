import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { chmodSync, existsSync, readFileSync, renameSync, unlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { McpServer } from '@modelcontextprotocol/server';
import { serveStdio } from '@modelcontextprotocol/server/stdio';
import * as z from 'zod/v4';

const SERVICE = 'com.gpoint.codex-chat';
const accounts = {
  pending: 'pending-device-code', access: 'access-token', refresh: 'refresh-token', expiry: 'access-expiry', creator: 'creator-id',
  pendingMessage: 'pending-message', scenario: 'active-dialogue-scenario',
} as const;
type Keychain = { get: (account: string) => Promise<string | null>; set: (account: string, value: string) => Promise<void>; delete: (account: string) => Promise<void> };
type Api = { request: <T>(path: string, init?: RequestInit) => Promise<T> };
type PendingMessage = { clientMessageId: string; creatorId: string; text: string };
type DialogueScenario = { scenarioId: string; maxTurns: number; turnsUsed: number; active: boolean };
type MessageResult =
  | { status: 'completed'; text: string; clientMessageId: string; userMessageId: string; responseId: string }
  | { status: 'pending'; clientMessageId: string; creatorId: string };

const MAX_AUTOMATIC_SCENARIO_TURNS = 20;

let keychainBinary: Promise<string> | null = null;
function compileKeychainHelper() {
  if (!keychainBinary) keychainBinary = new Promise((resolve, reject) => {
    const source = join(dirname(fileURLToPath(import.meta.url)), 'keychain-helper.swift');
    const digest = createHash('sha256').update(readFileSync(source)).digest('hex').slice(0, 20);
    const binary = join(tmpdir(), `gpoint-codex-keychain-${digest}`);
    if (existsSync(binary)) { resolve(binary); return; }
    const temporary = `${binary}-${randomUUID()}`;
    const child = spawn('/usr/bin/swiftc', [source, '-o', temporary], { stdio: ['ignore', 'ignore', 'ignore'] });
    child.on('error', reject);
    child.on('close', status => {
      if (status !== 0) { if (existsSync(temporary)) unlinkSync(temporary); return reject(new Error('macOS Keychain helper could not be compiled.')); }
      try { renameSync(temporary, binary); chmodSync(binary, 0o700); }
      catch (error) { if (existsSync(temporary)) unlinkSync(temporary); if (!existsSync(binary)) return reject(error); }
      resolve(binary);
    });
  });
  return keychainBinary;
}

async function keychainCommand(operation: 'get' | 'set' | 'delete', account: string, input?: string) {
  const binary = await compileKeychainHelper();
  return new Promise<string>((resolve, reject) => {
    const child = spawn(binary, [operation, SERVICE, account], { stdio: ['pipe', 'pipe', 'ignore'] });
    const output: Buffer[] = [];
    child.stdout.on('data', chunk => output.push(Buffer.from(chunk)));
    child.on('error', reject);
    child.on('close', status => status === 4 && operation === 'get' ? resolve('') : status === 0 ? resolve(Buffer.concat(output).toString('utf8')) : reject(new Error('macOS Keychain operation failed')));
    if (input !== undefined) child.stdin.end(input, 'utf8'); else child.stdin.end();
  });
}

export const macKeychain: Keychain = {
  async get(account) {
    return (await keychainCommand('get', account)) || null;
  },
  async set(account, value) {
    await keychainCommand('set', account, value);
  },
  async delete(account) {
    await keychainCommand('delete', account);
  },
};

export function createApi(baseUrl: string | undefined): Api {
  if (!baseUrl) throw new Error('Set GPOINT_API_URL to the existing GPoint API base URL.');
  const base = new URL(baseUrl);
  const loopback = ['localhost', '127.0.0.1', '::1'].includes(base.hostname);
  if (base.protocol !== 'https:' && !(loopback && base.protocol === 'http:')) throw new Error('GPOINT_API_URL must use HTTPS (HTTP is allowed only for localhost).');
  if (base.username || base.password || base.search || base.hash) throw new Error('GPOINT_API_URL must not contain credentials, query parameters or a fragment.');
  return {
    async request<T>(path: string, init: RequestInit = {}) {
      const response = await fetch(new URL(path.replace(/^\//, ''), base.toString().endsWith('/') ? base : new URL(`${base}/`)), { ...init, headers: { 'content-type': 'application/json', ...init.headers }, signal: AbortSignal.timeout(45_000), cache: 'no-store' });
      const data = await response.json().catch(() => null) as T | { code?: string } | null;
      if (!response.ok) throw new Error(`GPoint API request failed (${response.status}${data && typeof data === 'object' && 'code' in data && data.code ? `: ${data.code}` : ''}).`);
      return data as T;
    },
  };
}

function authorization(token: string): HeadersInit { return { authorization: `Bearer ${token}` }; }
type Tokens = { accessToken: string; refreshToken: string; accessExpiresAt: string; refreshExpiresAt: string; creatorId: string };

export function createCodexTools(api: Api, keychain: Keychain) {
  let sendTail: Promise<void> = Promise.resolve();
  let refreshInFlight: Promise<{ access: string; creatorId: string }> | null = null;
  const enqueueSend = <T,>(operation: () => Promise<T>): Promise<T> => {
    const current = sendTail.then(operation, operation);
    sendTail = current.then(() => undefined, () => undefined);
    return current;
  };
  const readCredential = async (account: string) => {
    const value = await keychain.get(account);
    if (!value) throw new Error('Codex is not connected. Run connect_gpoint first.');
    return value;
  };
  const saveTokens = async (tokens: Tokens) => {
    await Promise.all([
      keychain.set(accounts.access, tokens.accessToken), keychain.set(accounts.refresh, tokens.refreshToken),
      keychain.set(accounts.expiry, tokens.accessExpiresAt), keychain.set(accounts.creator, tokens.creatorId),
      keychain.delete(accounts.pending),
    ]);
  };
  const readPendingMessage = async (): Promise<PendingMessage | null> => {
    const raw = await keychain.get(accounts.pendingMessage);
    if (!raw) return null;
    try {
      const value = JSON.parse(raw) as Partial<PendingMessage>;
      if (typeof value.clientMessageId === 'string' && /^[0-9a-f-]{36}$/i.test(value.clientMessageId) &&
          typeof value.creatorId === 'string' && value.creatorId.length > 0 && value.creatorId.length <= 128 &&
          typeof value.text === 'string' && value.text.trim().length > 0 && value.text.length <= 4_000) {
        return value as PendingMessage;
      }
    } catch { /* fail closed below */ }
    throw new Error('Pending message state in macOS Keychain is invalid; do not send a replacement message.');
  };
  const readScenario = async (): Promise<DialogueScenario | null> => {
    const raw = await keychain.get(accounts.scenario);
    if (!raw) return null;
    try {
      const value = JSON.parse(raw) as Partial<DialogueScenario>;
      if (typeof value.scenarioId === 'string' && Number.isInteger(value.maxTurns) && value.maxTurns! >= 1 && value.maxTurns! <= MAX_AUTOMATIC_SCENARIO_TURNS &&
          Number.isInteger(value.turnsUsed) && value.turnsUsed! >= 0 && value.turnsUsed! <= value.maxTurns! && typeof value.active === 'boolean') {
        return value as DialogueScenario;
      }
    } catch { /* fail closed below */ }
    throw new Error('Automatic scenario state in macOS Keychain is invalid; stop before sending more messages.');
  };
  const getAccess = async () => {
    const [access, expiry, creatorId] = await Promise.all([readCredential(accounts.access), readCredential(accounts.expiry), readCredential(accounts.creator)]);
    if (Date.parse(expiry) - 30_000 > Date.now()) return { access, creatorId };
    if (!refreshInFlight) refreshInFlight = (async () => {
      const refreshToken = await readCredential(accounts.refresh);
      const refreshed = await api.request<Omit<Tokens, 'refreshExpiresAt'>>('/auth/codex/device/refresh', { method: 'POST', body: JSON.stringify({ refreshToken }) });
      await Promise.all([keychain.set(accounts.access, refreshed.accessToken), keychain.set(accounts.refresh, refreshed.refreshToken), keychain.set(accounts.expiry, refreshed.accessExpiresAt)]);
      return { access: refreshed.accessToken, creatorId: refreshed.creatorId };
    })().finally(() => { refreshInFlight = null; });
    return refreshInFlight;
  };
  return {
    async connect(creatorSlug: string) {
      if (creatorSlug !== 'models-hub') throw new Error('Connect only to the existing Models Hub creator (models-hub).');
      if (await readPendingMessage()) throw new Error('A previous Models Hub send is unresolved; resume it before reconnecting.');
      const started = await api.request<{ deviceCode: string; telegramLink: string; expiresIn: number }>('/auth/codex/device/start', { method: 'POST', body: JSON.stringify({ creatorSlug }) });
      await keychain.set(accounts.pending, started.deviceCode);
      return `Confirm this one-time connection to your existing Models Hub conversation in the GPoint Telegram bot: ${started.telegramLink}\nThe link expires in ${Math.round(started.expiresIn / 60)} minutes. After confirming, run finish_gpoint_connection.`;
    },
    async finish() {
      const deviceCode = await readCredential(accounts.pending);
      const result = await api.request<{ status: string } & Partial<Tokens>>('/auth/codex/device/exchange', { method: 'POST', body: JSON.stringify({ deviceCode }) });
      if (result.status !== 'connected' || !result.accessToken || !result.refreshToken || !result.accessExpiresAt || !result.creatorId) return `Connection status: ${result.status}. Approve the code in the selected GPoint conversation, then retry finish_gpoint_connection.`;
      await saveTokens(result as Tokens);
      return `Connected to your approved existing Models Hub conversation. Credentials are stored in macOS Keychain.`;
    },
    async connectionStatus() {
      const [access, refresh, expiry, creatorId, pending, scenario] = await Promise.all([
        keychain.get(accounts.access), keychain.get(accounts.refresh), keychain.get(accounts.expiry), keychain.get(accounts.creator),
        readPendingMessage(), readScenario(),
      ]);
      if (!creatorId || (!access && !refresh)) {
        return { status: 'disconnected' as const, serverValidation: 'not_checked' as const, pendingMessage: Boolean(pending), automaticScenarioActive: Boolean(scenario?.active) };
      }
      const accessCurrent = Boolean(access && expiry && Date.parse(expiry) > Date.now());
      return {
        status: accessCurrent ? 'local_token_present' as const : 'refresh_required' as const,
        creatorId,
        accessExpiresAt: expiry,
        serverValidation: 'not_checked' as const,
        pendingMessage: Boolean(pending),
        automaticScenarioActive: Boolean(scenario?.active),
      };
    },
    async history(limit = 50) {
      const { access, creatorId } = await getAccess();
      const result = await api.request<{ items: Array<{ id?: string; role: string; text: string; clientMessageId?: string; occurredAt: string }>; nextCursor: string | null }>(`/messages/creator/${encodeURIComponent(creatorId)}?limit=${limit}`, { headers: authorization(access) });
      return result.items.map(message => {
        const speaker = message.role === 'assistant' ? 'MODELS HUB' : message.role === 'user' ? 'YOU' : 'SYSTEM';
        const ids = [message.id ? `id=${message.id}` : '', message.clientMessageId ? `clientMessageId=${message.clientMessageId}` : ''].filter(Boolean).join('; ');
        return `${speaker}${ids ? ` [${ids}; ${message.occurredAt}]` : ` [${message.occurredAt}]`}: ${message.text}`;
      }).join('\n');
    },
    send(text: string) {
      return enqueueSend(async () => {
        if ((await readScenario())?.active) throw new Error('An automatic scenario is active; use send_models_hub_scenario_turn or stop it first.');
        return sendNewMessage(text);
      });
    },
    async beginScenario(description: string, maxTurns: number) {
      if (!description.trim() || description.length > 4_000) throw new Error('Scenario must contain 1–4000 characters.');
      if (!Number.isInteger(maxTurns) || maxTurns < 1 || maxTurns > MAX_AUTOMATIC_SCENARIO_TURNS) throw new Error('Automatic scenario turn limit must be 1–20.');
      return enqueueSend(async () => {
        if (await readPendingMessage()) throw new Error('A Models Hub message is unresolved; resume it before starting a scenario.');
        if ((await readScenario())?.active) throw new Error('An automatic scenario is already active; finish it before starting another.');
        await getAccess();
        const state: DialogueScenario = { scenarioId: randomUUID(), maxTurns, turnsUsed: 0, active: true };
        await keychain.set(accounts.scenario, JSON.stringify(state));
        return { scenarioId: state.scenarioId, maxTurns: state.maxTurns, turnsUsed: 0 };
      });
    },
    sendScenarioTurn(text: string) {
      return enqueueSend(async () => {
        const state = await readScenario();
        if (!state?.active) throw new Error('No automatic Models Hub scenario is active. Start one first.');
        if (await readPendingMessage()) throw new Error('The previous Models Hub turn is unresolved; call resume_pending_gpoint_message before continuing.');
        if (state.turnsUsed >= state.maxTurns) throw new Error(`Automatic scenario turn limit reached (${state.maxTurns}); stop the scenario.`);
        if (!text.trim() || text.length > 4_000) throw new Error('Message must contain 1–4000 characters.');
        const { access, creatorId } = await getAccess();
        state.turnsUsed += 1;
        await keychain.set(accounts.scenario, JSON.stringify(state));
        return createAndSubmitMessage(text, creatorId, access);
      });
    },
    async stopScenario() {
      return enqueueSend(async () => {
        await keychain.delete(accounts.scenario);
        return 'Automatic Models Hub scenario stopped.';
      });
    },
    resumePendingMessage() {
      return enqueueSend(async () => {
        const pending = await readPendingMessage();
        if (!pending) throw new Error('There is no pending Models Hub message to resume.');
        const { access, creatorId } = await getAccess();
        if (creatorId !== pending.creatorId) throw new Error('Pending message belongs to another creator; reconnect only to the approved Models Hub conversation before resuming.');
        return submitPendingMessage(pending, access);
      });
    },
    async disconnect() {
      if (await readPendingMessage()) throw new Error('A Models Hub send is unresolved; resume it before disconnecting so delivery is not lost.');
      const { access, creatorId } = await getAccess();
      await api.request(`/auth/codex/connection/${encodeURIComponent(creatorId)}/revoke`, { method: 'POST', headers: authorization(access) });
      await Promise.all(Object.values(accounts).map(account => keychain.delete(account)));
      return 'Codex access to the existing Models Hub conversation was revoked and local credentials were removed.';
    },
  };

  async function sendNewMessage(text: string): Promise<MessageResult> {
    if (!text.trim() || text.length > 4_000) throw new Error('Message must contain 1–4000 characters.');
    if (await readPendingMessage()) throw new Error('A previous Models Hub send is unresolved; call resume_pending_gpoint_message before sending another.');
    const { access, creatorId } = await getAccess();
    return createAndSubmitMessage(text, creatorId, access);
  }

  async function createAndSubmitMessage(text: string, creatorId: string, access: string): Promise<MessageResult> {
    const pending: PendingMessage = { clientMessageId: randomUUID(), creatorId, text: text.trim() };
    await keychain.set(accounts.pendingMessage, JSON.stringify(pending));
    return submitPendingMessage(pending, access);
  }

  async function submitPendingMessage(pending: PendingMessage, access: string): Promise<MessageResult> {
    let result: { status?: string; userMessageId?: string; response?: { id?: string; text?: string } };
    try {
      result = await api.request<typeof result>(`/messages/creator/${encodeURIComponent(pending.creatorId)}`, {
        method: 'POST', headers: authorization(access),
        body: JSON.stringify({ clientMessageId: pending.clientMessageId, text: pending.text }),
      });
    } catch {
      throw new Error(`Models Hub delivery is unresolved. The exact message and id are saved in macOS Keychain; do not send a replacement. Call resume_pending_gpoint_message to retry with the same id.`);
    }
    if (result.status === 'pending') {
      if (result.userMessageId && result.userMessageId !== pending.clientMessageId) {
        throw new Error('GPoint returned a mismatched pending message id. The original remains pending; call resume_pending_gpoint_message.');
      }
      return { status: 'pending', clientMessageId: pending.clientMessageId, creatorId: pending.creatorId };
    }
    if (result.status !== 'completed' || result.userMessageId !== pending.clientMessageId || typeof result.response?.id !== 'string' || typeof result.response.text !== 'string') {
      throw new Error('GPoint returned an uncorrelated Models Hub result. The original message remains pending; call resume_pending_gpoint_message.');
    }
    await keychain.delete(accounts.pendingMessage);
    return { status: 'completed', text: result.response.text, clientMessageId: pending.clientMessageId, userMessageId: result.userMessageId, responseId: result.response.id };
  }
}

export function buildServer() {
  const api: Api = { request: (path, init) => createApi(process.env.GPOINT_API_URL).request(path, init) };
  const tools = createCodexTools(api, macKeychain);
  const server = new McpServer({ name: 'gpoint-dialogue-lab', version: '0.1.0' });
  const sendResult = (result: MessageResult) => result.status === 'completed'
    ? { content: [
      { type: 'text' as const, text: result.text },
      { type: 'text' as const, text: `[GPoint correlation: user=${result.userMessageId}; Models Hub=${result.responseId}; clientMessageId=${result.clientMessageId}]` },
    ] }
    : { content: [
      { type: 'text' as const, text: `Message ${result.clientMessageId} was accepted but its reply is still pending. Do not send a replacement; call resume_pending_gpoint_message.` },
    ] };
  server.registerTool('connect_gpoint', { description: 'Begin a one-time owner-authorized link to the owner’s existing Models Hub conversation (creator slug models-hub) through the existing GPoint Telegram bot. No test account or new conversation is created.', inputSchema: z.object({ creatorSlug: z.literal('models-hub') }) }, async ({ creatorSlug }) => ({ content: [{ type: 'text', text: await tools.connect(creatorSlug) }] }));
  server.registerTool('finish_gpoint_connection', { description: 'Complete the approved one-time GPoint connection and store capability tokens in macOS Keychain.', inputSchema: z.object({}) }, async () => ({ content: [{ type: 'text', text: await tools.finish() }] }));
  server.registerTool('get_gpoint_connection_status', { description: 'Inspect local Keychain credential and pending-operation state. Clearly reports that server-side grant validity is not checked by this local status tool.', inputSchema: z.object({}) }, async () => ({ content: [{ type: 'text', text: JSON.stringify(await tools.connectionStatus()) }] }));
  server.registerTool('read_gpoint_history', { description: 'Read the existing owner-approved Models Hub conversation from the same GPoint/Eros transcript used by Telegram Mini App. Returns exact text, speaker, message ids and clientMessageId for correlation.', inputSchema: z.object({ limit: z.number().int().min(1).max(100).default(50) }) }, async ({ limit }) => ({ content: [{ type: 'text', text: await tools.history(limit) }] }));
  server.registerTool('send_gpoint_message', { description: 'Manual mode: send one exact owner-authored message to the existing Models Hub conversation through the shared GPoint message API/Eros session. Returns the exact Models Hub reply and correlated message ids. Timed-out sends are kept pending and must be resumed, never replaced.', inputSchema: z.object({ text: z.string().min(1).max(4000) }) }, async ({ text }) => sendResult(await tools.send(text)));
  server.registerTool('resume_pending_gpoint_message', { description: 'Resolve the last uncertain Models Hub send by reusing its exact text and clientMessageId from macOS Keychain; the existing GPoint/Eros endpoint deduplicates by that id. Never use a new id to retry.', inputSchema: z.object({}) }, async () => sendResult(await tools.resumePendingMessage()));
  server.registerTool('begin_models_hub_scenario', { description: 'Begin a Codex-orchestrated automatic dialogue scenario against the existing Models Hub thread. Specify the scenario and a hard limit of 1–20 total outgoing turns. The scenario text is not persisted; each following tool call sends exactly one turn so Codex can display its reply before continuing.', inputSchema: z.object({ scenario: z.string().min(1).max(4000), maxTurns: z.number().int().min(1).max(MAX_AUTOMATIC_SCENARIO_TURNS) }) }, async ({ scenario, maxTurns }) => ({ content: [{ type: 'text', text: JSON.stringify(await tools.beginScenario(scenario, maxTurns)) }] }));
  server.registerTool('send_models_hub_scenario_turn', { description: 'Send exactly one Codex-authored turn in the active bounded scenario and return that exact Models Hub reply before the next turn. Call sequentially and show each reply in the Codex chat. The server enforces the scenario cap.', inputSchema: z.object({ text: z.string().min(1).max(4000) }) }, async ({ text }) => sendResult(await tools.sendScenarioTurn(text)));
  server.registerTool('stop_models_hub_scenario', { description: 'Stop the active automatic Models Hub scenario. Does not send a message or inference request.', inputSchema: z.object({}) }, async () => ({ content: [{ type: 'text', text: await tools.stopScenario() }] }));
  server.registerTool('disconnect_gpoint', { description: 'Revoke this Codex connection and remove local Keychain credentials.', inputSchema: z.object({}) }, async () => ({ content: [{ type: 'text', text: await tools.disconnect() }] }));
  return server;
}
