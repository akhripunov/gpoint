import { jwtVerify } from 'jose';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_MESSAGES = 64;
const MAX_MESSAGE_CHARS = 16_000;
const MAX_TOTAL_CHARS = 48_000;
const MAX_REPLY_CHARS = 4_096;

export interface ErosBridgeIdentity {
  tenantId: string;
  userId: string;
  creatorId: string;
  sessionId: string;
  instanceId: string;
  requestId: string;
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

export async function verifyBridgeIdentity(token: string, secret: Uint8Array, nowSeconds = Math.floor(Date.now() / 1000)): Promise<ErosBridgeIdentity> {
  if (typeof token !== 'string' || token.length > 2_048 || secret.byteLength < 32) throw new Error('eros_bridge_identity_invalid');
  let payload: Record<string, unknown>;
  try {
    const verified = await jwtVerify(token, secret, {
      algorithms: ['HS256'], issuer: 'eros-engine', audience: 'gpoint-worker-gateway',
      currentDate: new Date(nowSeconds * 1000), clockTolerance: 3,
    });
    payload = verified.payload as Record<string, unknown>;
  } catch {
    throw new Error('eros_bridge_identity_invalid');
  }
  const userId = payload.sub;
  const tenantId = payload.tenant_id;
  const creatorId = payload.creator_id;
  const sessionId = payload.session_id;
  const instanceId = payload.instance_id;
  const requestId = payload.jti;
  const issuedAt = payload.iat;
  const expiresAt = payload.exp;
  if (![tenantId, userId, creatorId, sessionId, instanceId, requestId].every((value) => typeof value === 'string' && UUID.test(value)) ||
      typeof issuedAt !== 'number' || typeof expiresAt !== 'number' || expiresAt <= issuedAt ||
      expiresAt - issuedAt > 120 || issuedAt > nowSeconds + 3 || nowSeconds - issuedAt > 120) {
    throw new Error('eros_bridge_identity_invalid');
  }
  return { tenantId: tenantId as string, userId: userId as string, creatorId: creatorId as string, sessionId: sessionId as string, instanceId: instanceId as string, requestId: requestId as string };
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
  const identity = typeof value.user === 'string' ? await verifyBridgeIdentity(value.user, secret, nowSeconds) : undefined;
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
