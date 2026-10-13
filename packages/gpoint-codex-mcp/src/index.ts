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
const accounts = { pending: 'pending-device-code', access: 'access-token', refresh: 'refresh-token', expiry: 'access-expiry', creator: 'creator-id' } as const;
type Keychain = { get: (account: string) => Promise<string | null>; set: (account: string, value: string) => Promise<void>; delete: (account: string) => Promise<void> };
type Api = { request: <T>(path: string, init?: RequestInit) => Promise<T> };

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
    async connect() {
      const started = await api.request<{ deviceCode: string; userCode: string; expiresIn: number; verificationPath: string }>('/auth/codex/device/start', { method: 'POST', body: '{}' });
      await keychain.set(accounts.pending, started.deviceCode);
      return `Open your GPoint Mini App, select the existing conversation, choose Connect Codex, and enter ${started.userCode}. The code expires in ${Math.round(started.expiresIn / 60)} minutes.`;
    },
    async finish() {
      const deviceCode = await readCredential(accounts.pending);
      const result = await api.request<{ status: string } & Partial<Tokens>>('/auth/codex/device/exchange', { method: 'POST', body: JSON.stringify({ deviceCode }) });
      if (result.status !== 'connected' || !result.accessToken || !result.refreshToken || !result.accessExpiresAt || !result.creatorId) return `Connection status: ${result.status}. Approve the code in the selected GPoint conversation, then retry finish_gpoint_connection.`;
      await saveTokens(result as Tokens);
      return `Connected to the approved GPoint conversation (creator ${result.creatorId}). Access is stored in macOS Keychain.`;
    },
    async history(limit = 50) {
      const { access, creatorId } = await getAccess();
      const result = await api.request<{ items: Array<{ role: string; text: string; occurredAt: string }>; nextCursor: string | null }>(`/messages/creator/${encodeURIComponent(creatorId)}?limit=${limit}`, { headers: authorization(access) });
      return result.items.map(message => `${message.role === 'assistant' ? 'AGATA' : 'OWNER'} [${message.occurredAt}]: ${message.text}`).join('\n');
    },
    send(text: string) {
      return enqueueSend(async () => {
      if (!text.trim() || text.length > 4000) throw new Error('Message must contain 1–4000 characters.');
      const { access, creatorId } = await getAccess();
      const id = randomUUID();
      const result = await api.request<{ status: string; response?: { text: string } }>(`/messages/creator/${encodeURIComponent(creatorId)}`, { method: 'POST', headers: authorization(access), body: JSON.stringify({ clientMessageId: id, text }) });
      if (result.status === 'completed' && result.response) return result.response.text;
      return `AGATA accepted message ${id}; its reply is still pending and was not correlated to this request. Run read_gpoint_history to see new messages.`;
      });
    },
    async disconnect() {
      const { access, creatorId } = await getAccess();
      await api.request(`/auth/codex/connection/${encodeURIComponent(creatorId)}/revoke`, { method: 'POST', headers: authorization(access) });
      await Promise.all(Object.values(accounts).map(account => keychain.delete(account)));
      return 'Codex access to this GPoint conversation was revoked and local credentials were removed.';
    },
  };
}

export function buildServer() {
  const api: Api = { request: (path, init) => createApi(process.env.GPOINT_API_URL).request(path, init) };
  const tools = createCodexTools(api, macKeychain);
  const server = new McpServer({ name: 'gpoint-agata', version: '0.1.0' });
  server.registerTool('connect_gpoint', { description: 'Begin owner-authorized connection to one existing GPoint conversation; only a one-time user code is shown.', inputSchema: z.object({}) }, async () => ({ content: [{ type: 'text', text: await tools.connect() }] }));
  server.registerTool('finish_gpoint_connection', { description: 'Complete the approved one-time GPoint connection and store capability tokens in macOS Keychain.', inputSchema: z.object({}) }, async () => ({ content: [{ type: 'text', text: await tools.finish() }] }));
  server.registerTool('read_gpoint_history', { description: 'Read the latest messages from the one owner-approved GPoint conversation.', inputSchema: z.object({ limit: z.number().int().min(1).max(100).default(50) }) }, async ({ limit }) => ({ content: [{ type: 'text', text: await tools.history(limit) }] }));
  server.registerTool('send_gpoint_message', { description: 'Send the owner’s exact text to AGATA through the existing GPoint message pipeline and return the exact AGATA response.', inputSchema: z.object({ text: z.string().min(1).max(4000) }) }, async ({ text }) => ({ content: [{ type: 'text', text: await tools.send(text) }] }));
  server.registerTool('disconnect_gpoint', { description: 'Revoke this Codex connection and remove local Keychain credentials.', inputSchema: z.object({}) }, async () => ({ content: [{ type: 'text', text: await tools.disconnect() }] }));
  return server;
}
