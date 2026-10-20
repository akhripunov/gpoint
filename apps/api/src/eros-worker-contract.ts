import { createHash, createHmac, timingSafeEqual } from 'node:crypto';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_MESSAGES = 64;
const MAX_MESSAGE_CHARS = 16_000;
const MAX_TOTAL_CHARS = 48_000;
const MAX_REPLY_CHARS = 4_096;
const BRIDGE_TOKEN_PREFIX = 'g1';
const BRIDGE_TOKEN_DOMAIN = 'gpoint-eros-provider-identity-v1';
const EROS_SYSTEM_AUDIT_USER = '11111111-1111-1111-1111-111111111111';

export interface ErosBridgeIdentity {
  tenantId: string;
  userId: string;
  creatorId: string;
  sessionId: string;
  instanceId: string;
  requestId: string;
}

/** Preserve the signed chat-turn scope while giving each stock Eros provider call its own idempotency key. */
export function deriveBridgeInferenceIdentity(identity: ErosBridgeIdentity, requestFingerprint: string): ErosBridgeIdentity {
  const bytes = createHash('sha256')
    .update(identity.requestId)
    .update('\0')
    .update(requestFingerprint)
    .digest()
    .subarray(0, 16);
  bytes[6] = (bytes[6] & 0x0f) | 0x80;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString('hex');
  return {
    ...identity,
    requestId: `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`,
  };
}

export interface ErosBridgeMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface ParsedErosBridgeRequest {
  identity?: ErosBridgeIdentity;
  model: string;
  messages: ErosBridgeMessage[];
  maxTokens: number;
  temperature: number;
  stream: boolean;
}

export interface ParsedErosEmbeddingRequest { model: string; input: string[]; dimensions: 512 }

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function uuidToBytes(value: string): Buffer {
  if (!UUID.test(value)) throw new Error('eros_bridge_identity_invalid');
  return Buffer.from(value.replaceAll('-', ''), 'hex');
}

function bytesToUuid(value: Buffer): string {
  const hex = value.toString('hex');
  const uuid = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  if (!UUID.test(uuid)) throw new Error('eros_bridge_identity_invalid');
  return uuid;
}

/** Compact signed provider metadata for stock Eros's 256-character audit.user cap. */
export function signBridgeIdentity(identity: ErosBridgeIdentity, secret: Uint8Array, nowSeconds = Math.floor(Date.now() / 1000)): string {
  if (secret.byteLength < 32 || !Number.isInteger(nowSeconds) || nowSeconds < 0) throw new Error('eros_bridge_identity_invalid');
  const payload = Buffer.concat([
    uuidToBytes(identity.tenantId), uuidToBytes(identity.userId), uuidToBytes(identity.creatorId),
    uuidToBytes(identity.sessionId), uuidToBytes(identity.instanceId), uuidToBytes(identity.requestId),
    Buffer.from([(nowSeconds >>> 24) & 0xff, (nowSeconds >>> 16) & 0xff, (nowSeconds >>> 8) & 0xff, nowSeconds & 0xff]),
  ]).toString('base64url');
  const signed = `${BRIDGE_TOKEN_PREFIX}.${payload}`;
  const signature = createHmac('sha256', secret).update(BRIDGE_TOKEN_DOMAIN).update('\0').update(signed).digest('base64url');
  const token = `${signed}.${signature}`;
  if (token.length > 256) throw new Error('eros_bridge_identity_invalid');
  return token;
}

export async function verifyBridgeIdentity(token: string, secret: Uint8Array, nowSeconds = Math.floor(Date.now() / 1000)): Promise<ErosBridgeIdentity> {
  if (typeof token !== 'string' || token.length > 256 || secret.byteLength < 32) throw new Error('eros_bridge_identity_invalid');
  const match = /^g1\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$/.exec(token);
  if (!match) throw new Error('eros_bridge_identity_invalid');
  const signed = `${BRIDGE_TOKEN_PREFIX}.${match[1]}`;
  const expected = createHmac('sha256', secret).update(BRIDGE_TOKEN_DOMAIN).update('\0').update(signed).digest();
  const supplied = Buffer.from(match[2], 'base64url');
  if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) throw new Error('eros_bridge_identity_invalid');
  const payload = Buffer.from(match[1], 'base64url');
  if (payload.length !== 100 || payload.toString('base64url') !== match[1]) throw new Error('eros_bridge_identity_invalid');
  const issuedAt = payload.readUInt32BE(96);
  if (issuedAt > nowSeconds + 3 || nowSeconds - issuedAt > 120) throw new Error('eros_bridge_identity_invalid');
  return {
    tenantId: bytesToUuid(payload.subarray(0, 16)), userId: bytesToUuid(payload.subarray(16, 32)),
    creatorId: bytesToUuid(payload.subarray(32, 48)), sessionId: bytesToUuid(payload.subarray(48, 64)),
    instanceId: bytesToUuid(payload.subarray(64, 80)), requestId: bytesToUuid(payload.subarray(80, 96)),
  };
}

export async function parseBridgeRequest(value: unknown, secret: Uint8Array, nowSeconds = Math.floor(Date.now() / 1000)): Promise<ParsedErosBridgeRequest> {
  if (!isRecord(value) || Object.keys(value).some((key) => ['user_id', 'tenant_id', 'creator_id', 'session_id', 'instance_id', 'request_id'].includes(key))) {
    throw new Error('eros_chat_request_invalid');
  }
  const model = value.model;
  const rawMessages = value.messages;
  const maxTokens = value.max_tokens ?? 1024;
  const temperature = value.temperature ?? 1;
  // OpenAI chat-completions defaults to non-streaming. Eros's non-stream
  // requests omit this false-valued field on the wire; defaulting to true
  // makes the bridge return SSE where callers expect a JSON completion.
  const stream = value.stream ?? false;
  if (typeof model !== 'string' || !/^[A-Za-z0-9._:@/-]{1,128}$/.test(model) ||
      !Array.isArray(rawMessages) || rawMessages.length < 1 || rawMessages.length > MAX_MESSAGES ||
      typeof maxTokens !== 'number' || !Number.isSafeInteger(maxTokens) || maxTokens < 1 || maxTokens > MAX_REPLY_CHARS ||
      typeof temperature !== 'number' || !Number.isFinite(temperature) || temperature !== 1 ||
      typeof stream !== 'boolean' || ('user' in value && typeof value.user !== 'string')) {
    throw new Error('eros_chat_request_invalid');
  }
  let total = 0;
  const messages = rawMessages.map((message): ErosBridgeMessage => {
    if (!isRecord(message) || !['system', 'user', 'assistant'].includes(String(message.role)) ||
        typeof message.content !== 'string' || message.content.length < 1 || message.content.length > MAX_MESSAGE_CHARS ||
        Object.keys(message).some((key) => key !== 'role' && key !== 'content')) {
      throw new Error('eros_chat_request_invalid');
    }
    total += message.content.length;
    if (total > MAX_TOTAL_CHARS) throw new Error('eros_chat_request_invalid');
    return { role: message.role as ErosBridgeMessage['role'], content: message.content };
  });
  const identity = typeof value.user === 'string' && value.user !== EROS_SYSTEM_AUDIT_USER
    ? await verifyBridgeIdentity(value.user, secret, nowSeconds)
    : undefined;
  return { identity, model, messages, maxTokens, temperature, stream };
}

export function buildChatCompletionSse(input: { id: string; model: string; text: string; created?: number }): string {
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(input.id) || !/^[A-Za-z0-9._:@/-]{1,128}$/.test(input.model) ||
      typeof input.text !== 'string' || !input.text.trim() || input.text.length > MAX_REPLY_CHARS) {
    throw new Error('eros_completion_invalid');
  }
  const base = { id: input.id, object: 'chat.completion.chunk', created: input.created ?? Math.floor(Date.now() / 1000), model: input.model };
  const content = { ...base, choices: [{ index: 0, delta: { content: input.text }, finish_reason: null }] };
  const finish = { ...base, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] };
  return `data: ${JSON.stringify(content)}\n\ndata: ${JSON.stringify(finish)}\n\ndata: [DONE]\n\n`;
}

export function parseErosEmbeddingRequest(value: unknown): ParsedErosEmbeddingRequest {
  if (!isRecord(value) || Object.keys(value).some((key) => !['model', 'input', 'dimensions'].includes(key)) ||
      value.dimensions !== 512 || value.model !== 'qwen3-embedding:0.6b') throw new Error('eros_embedding_request_invalid');
  const raw = typeof value.input === 'string' ? [value.input] : value.input;
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > 16 ||
      raw.some((text) => typeof text !== 'string' || text.trim().length === 0 || text.length > 6_000)) {
    throw new Error('eros_embedding_request_invalid');
  }
  return { model: value.model, input: raw as string[], dimensions: 512 };
}

export function buildOpenAiEmbeddingResponse(model: string, embeddings: unknown): { object: 'list'; model: string; data: Array<{ object: 'embedding'; index: number; embedding: number[] }> } {
  if (!Array.isArray(embeddings) || embeddings.length < 1 || embeddings.length > 16 ||
      embeddings.some((vector) => !Array.isArray(vector) || vector.length !== 512 ||
        vector.some((value) => typeof value !== 'number' || !Number.isFinite(value)))) {
    throw new Error('eros_embedding_response_invalid');
  }
  return { object: 'list', model, data: embeddings.map((embedding, index) => ({ object: 'embedding', index, embedding: embedding as number[] })) };
}
