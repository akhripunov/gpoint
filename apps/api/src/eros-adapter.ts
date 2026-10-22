import { createHash } from 'node:crypto';
import { SignJWT } from 'jose';
import { signBridgeIdentity } from './eros-worker-contract.js';

const USER_NAMESPACE = 'b92c2772-7534-5a7c-8eed-ec08e0a9c45b';
const CREATOR_NAMESPACE = 'c9568a43-7e99-58a2-a41e-50e031eac7bc';
const RELATIONSHIP_NAMESPACE = '8a275e5e-c6dd-5d56-9dc8-d5642d06d9d0';
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MESSAGE_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_REPLY_CHARS = 4_096;
const MAX_STREAM_BYTES = 128 * 1024;
const MAX_EROS_REQUEST_TIMEOUT_MS = 240_000;

export interface ErosIdentity {
  userId: string;
  creatorId: string;
  relationshipId: string;
}

function uuidBytes(value: string): Buffer {
  return Buffer.from(value.replaceAll('-', ''), 'hex');
}

/** Deterministic RFC 4122 UUIDv5. GPoint ids never leave this server-side mapping. */
export function uuidV5(namespace: string, name: string): string {
  if (!UUID_PATTERN.test(namespace) || !name) throw new Error('eros_identity_input_invalid');
  const hash = createHash('sha1').update(Buffer.concat([uuidBytes(namespace), Buffer.from(name, 'utf8')])).digest();
  const bytes = Buffer.from(hash.subarray(0, 16));
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString('hex');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function erosIdentityFor(input: { userId: string; creatorId: string }): ErosIdentity {
  if (!input.userId || !input.creatorId) throw new Error('eros_identity_input_invalid');
  return {
    userId: uuidV5(USER_NAMESPACE, input.userId),
    creatorId: uuidV5(CREATOR_NAMESPACE, input.creatorId),
    relationshipId: uuidV5(RELATIONSHIP_NAMESPACE, `${input.userId}\0${input.creatorId}`),
  };
}

function csvIds(value: string | undefined): Set<string> {
  return new Set((value ?? '').split(',').map((item) => item.trim()).filter(Boolean));
}

/** Eros is available only to exact synthetic identity allowlists; production requires a separate pilot gate. */
export function isErosChatAllowed(userId: string, creatorId: string, env: NodeJS.ProcessEnv = process.env): boolean {
  const productionPilot = env.NODE_ENV === 'production' && env.EROS_ENABLED === 'true' &&
    env.EROS_PRODUCTION_TEST_MODE === 'true' && env.EROS_DATA_MODE === 'synthetic' &&
    csvIds(env.EROS_PRODUCTION_TEST_USER_IDS).has(userId) &&
    csvIds(env.EROS_PRODUCTION_TEST_CREATOR_IDS).has(creatorId);
  const localSynthetic = ['development', 'test'].includes(env.NODE_ENV ?? '') && env.EROS_ENABLED === 'true' &&
    env.EROS_DATA_MODE === 'synthetic' && isLocalSandboxDatabaseUrl(env.DATABASE_URL) &&
    csvIds(env.EROS_TEST_USER_IDS).has(userId) && csvIds(env.EROS_TEST_CREATOR_IDS).has(creatorId);
  return productionPilot || localSynthetic;
}

export function isSyntheticErosPair(
  user: { telegramUsername?: string | null },
  creator: { slug: string; profileStatus: string },
): boolean {
  return typeof user.telegramUsername === 'string' && /^eros_test_[a-z0-9_]{2,32}$/i.test(user.telegramUsername) &&
    /^eros-test-[a-z0-9-]{2,48}$/i.test(creator.slug) && creator.profileStatus === 'APPROVED';
}

export function erosTestCreatorIds(env: NodeJS.ProcessEnv = process.env): string[] {
  const allowlist = env.NODE_ENV === 'production' ? env.EROS_PRODUCTION_TEST_CREATOR_IDS : env.EROS_TEST_CREATOR_IDS;
  return [...csvIds(allowlist)].slice(0, 20);
}

function isLocalSandboxDatabaseUrl(value: string | undefined): boolean {
  if (!value) return false;
  try {
    const url = new URL(value);
    const database = decodeURIComponent(url.pathname.replace(/^\//, '')).toLowerCase();
    return ['postgres:', 'postgresql:'].includes(url.protocol) &&
      ['localhost', '127.0.0.1', '::1', 'host.docker.internal', 'postgres', 'gpoint-eros-db'].includes(url.hostname.toLowerCase()) &&
      /(?:^|[_-])(test|local|sandbox|eros)(?:$|[_-])/.test(database);
  } catch { return false; }
}

export interface ErosCreatorProfile {
  id: string;
  displayName: string;
  genomeId?: string | null;
}

export interface ErosRelationshipMappingStore {
  getInstanceId(userId: string, creatorId: string): Promise<string | null>;
  saveInstanceId(userId: string, creatorId: string, instanceId: string): Promise<void>;
}

/** Resolve an instance mapping from GPoint-owned state; never query Eros tables. */
export async function resolveErosRelationshipInstanceId(
  store: ErosRelationshipMappingStore,
  userId: string,
  creatorId: string,
): Promise<string | null> {
  if (!userId || !creatorId) throw new ErosAdapterError('eros_identity_input_invalid', 400);
  const instanceId = await store.getInstanceId(userId, creatorId);
  if (instanceId === null) return null;
  if (typeof instanceId !== 'string' || !UUID_PATTERN.test(instanceId)) throw new ErosAdapterError('eros_relationship_invalid');
  return instanceId;
}

export async function prepareErosRelationship(
  client: Pick<ErosEngineClient, 'startSession'>,
  store: ErosRelationshipMappingStore,
  input: { userId: string; creator: ErosCreatorProfile },
): Promise<{ identity: ErosIdentity; session: { sessionId: string; instanceId: string } }> {
  const identity = erosIdentityFor({ userId: input.userId, creatorId: input.creator.id });
  const mappedInstanceId = await resolveErosRelationshipInstanceId(store, input.userId, input.creator.id);
  const genomeId = input.creator.genomeId;
  if (!mappedInstanceId && (typeof genomeId !== 'string' || !UUID_PATTERN.test(genomeId))) {
    throw new ErosAdapterError('eros_persona_not_provisioned');
  }
  const session = await client.startSession(identity.userId, genomeId, mappedInstanceId ?? undefined);
  if (mappedInstanceId && session.instanceId !== mappedInstanceId) {
    throw new ErosAdapterError('eros_relationship_mismatch');
  }
  identity.relationshipId = session.instanceId;
  if (!mappedInstanceId) await store.saveInstanceId(input.userId, input.creator.id, session.instanceId);
  return { identity, session };
}

export class ErosAdapterError extends Error {
  constructor(readonly code: string, readonly status = 503) {
    super(code);
    this.name = 'ErosAdapterError';
  }
}

interface ErosClientOptions {
  baseUrl: string;
  jwtSecret: string;
  workerSigningSecret?: string;
  workerTenantId?: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

interface ErosStartResponse {
  session_id?: unknown;
  instance_id?: unknown;
  persona_name?: unknown;
}

export interface ErosHistoryMessage {
  id: string;
  role: string;
  content: string;
  sent_at: string;
  read_at?: string;
  client_msg_id?: string;
}

export interface ErosHistoryPage {
  messages: ErosHistoryMessage[];
  total: number;
}

export interface ErosSession {
  sessionId: string;
  instanceId: string | null;
  lastActiveAt: string;
  channel: 'text' | 'voice' | 'group';
}

export interface ErosAssistantMessageStateStore {
  createMany(args: { data: Array<{ userId: string; creatorId: string; sessionId: string; messageId: string; readAt: Date | null }>; skipDuplicates: boolean }): Promise<{ count: number }>;
  findMany(args: { where: { userId: string; creatorId: string; sessionId: string; messageId?: { in: string[] } }; select?: { messageId?: boolean; readAt?: boolean } }): Promise<Array<{ messageId: string; readAt?: Date | null }>>;
  updateMany(args: { where: { userId: string; creatorId: string; sessionId: string; messageId: { in: string[] }; readAt: null }; data: { readAt: Date } }): Promise<{ count: number }>;
  groupBy(args: { by: ['creatorId', 'sessionId']; where: { userId: string; readAt: null; OR: Array<{ creatorId: string; sessionId: string }> }; _count: { _all: true } }): Promise<Array<{ creatorId: string; sessionId: string; _count: { _all: number } }>>;
}

export interface ErosAssistantMessageStateDatabase {
  erosAssistantMessageState: ErosAssistantMessageStateStore;
}

function assertMessageStateIdentity(userId: string, creatorId: string, sessionId: string): void {
  if (!userId || !creatorId || !UUID_PATTERN.test(sessionId)) throw new ErosAdapterError('eros_identity_invalid', 400);
}

/** Persist only Eros message identifiers/read timestamps in GPoint; never copy message content. */
export async function recordErosAssistantMessage(
  db: ErosAssistantMessageStateDatabase,
  userId: string,
  creatorId: string,
  sessionId: string,
  messageId: string,
): Promise<void> {
  assertMessageStateIdentity(userId, creatorId, sessionId);
  if (!MESSAGE_ID_PATTERN.test(messageId)) throw new ErosAdapterError('eros_response_invalid');
  await db.erosAssistantMessageState.createMany({
    data: [{ userId, creatorId, sessionId, messageId, readAt: null }],
    skipDuplicates: true,
  });
}

/** Index assistant IDs observed through stock Eros history; bodies remain in Eros only. */
export async function syncErosAssistantMessageStates(
  db: ErosAssistantMessageStateDatabase,
  userId: string,
  creatorId: string,
  sessionId: string,
  messages: ErosHistoryMessage[],
): Promise<void> {
  assertMessageStateIdentity(userId, creatorId, sessionId);
  const data = messages.filter((message) => message.role === 'assistant').map((message) => {
    if (!MESSAGE_ID_PATTERN.test(message.id) || !Number.isFinite(Date.parse(message.sent_at))) throw new ErosAdapterError('eros_response_invalid');
    const readAt = message.read_at === undefined ? null : new Date(message.read_at);
    if (readAt !== null && !Number.isFinite(readAt.getTime())) throw new ErosAdapterError('eros_response_invalid');
    return { userId, creatorId, sessionId, messageId: message.id, readAt };
  });
  if (!data.length) return;
  await db.erosAssistantMessageState.createMany({ data, skipDuplicates: true });
}

export async function markErosAssistantMessagesRead(
  db: ErosAssistantMessageStateDatabase,
  userId: string,
  creatorId: string,
  sessionId: string,
  messageIds: string[],
): Promise<string[]> {
  assertMessageStateIdentity(userId, creatorId, sessionId);
  if (!Array.isArray(messageIds) ||
      messageIds.length < 1 || messageIds.length > 100 || new Set(messageIds).size !== messageIds.length ||
      !messageIds.every((id) => MESSAGE_ID_PATTERN.test(id))) {
    throw new ErosAdapterError('eros_read_request_invalid', 400);
  }
  const rows = await db.erosAssistantMessageState.findMany({
    where: { userId, creatorId, sessionId, messageId: { in: messageIds } },
    select: { messageId: true },
  });
  if (rows.length !== messageIds.length || rows.some((row) => !messageIds.includes(row.messageId))) {
    throw new ErosAdapterError('eros_read_messages_not_owned', 404);
  }
  await db.erosAssistantMessageState.updateMany({
    where: { userId, creatorId, sessionId, messageId: { in: messageIds }, readAt: null },
    data: { readAt: new Date() },
  });
  return messageIds;
}

export async function unreadErosAssistantCounts(
  db: ErosAssistantMessageStateDatabase,
  userId: string,
  sessions: Array<{ creatorId: string; sessionId: string }>,
): Promise<Map<string, number>> {
  if (!userId || !Array.isArray(sessions) || sessions.length > 100 ||
      new Set(sessions.map((item) => item.sessionId)).size !== sessions.length ||
      !sessions.every((item) => !!item.creatorId && UUID_PATTERN.test(item.sessionId))) {
    throw new ErosAdapterError('eros_unread_request_invalid', 400);
  }
  if (!sessions.length) return new Map();
  const counts = new Map<string, number>();
  for (const item of sessions) counts.set(item.sessionId, 0);
  const rows = await db.erosAssistantMessageState.groupBy({
    by: ['creatorId', 'sessionId'],
    where: { userId, readAt: null, OR: sessions.map(({ creatorId, sessionId }) => ({ creatorId, sessionId })) },
    _count: { _all: true },
  });
  for (const row of rows) {
    if (!sessions.some((item) => item.creatorId === row.creatorId && item.sessionId === row.sessionId) || !Number.isSafeInteger(row._count._all) || row._count._all < 0) {
      throw new ErosAdapterError('eros_response_invalid');
    }
    counts.set(row.sessionId, row._count._all);
  }
  return counts;
}

export async function erosAssistantReadAtById(
  db: ErosAssistantMessageStateDatabase,
  userId: string,
  creatorId: string,
  sessionId: string,
  messageIds: string[],
): Promise<Map<string, string | null>> {
  assertMessageStateIdentity(userId, creatorId, sessionId);
  if (!messageIds.length || messageIds.length > 100 || !messageIds.every((id) => MESSAGE_ID_PATTERN.test(id))) return new Map();
  const rows = await db.erosAssistantMessageState.findMany({
    where: { userId, creatorId, sessionId, messageId: { in: messageIds } },
    select: { messageId: true, readAt: true },
  });
  return new Map(rows.map((row) => [row.messageId, row.readAt instanceof Date ? row.readAt.toISOString() : null]));
}

export function mergeErosInboxItems<T extends { creator: { id: string } }>(agataItems: T[], erosItems: T[], isFirstPage: boolean): T[] {
  const erosCreatorIds = new Set(erosItems.map((item) => item.creator.id));
  const filteredAgata = agataItems.filter((item) => !erosCreatorIds.has(item.creator.id));
  return isFirstPage ? [...erosItems, ...filteredAgata] : filteredAgata;
}

export function erosInboxLastMessageSummary(message: { role: string; content: string }): {
  lastMessagePreview: string;
  lastMessageRole: 'user' | 'assistant' | 'system';
} {
  if (message.role === 'system_error') return {
    lastMessagePreview: 'Сообщение не удалось обработать. Попробуйте ещё раз.',
    lastMessageRole: 'system',
  };
  return {
    lastMessagePreview: message.content.slice(0, 240),
    lastMessageRole: message.role === 'user' ? 'user' : 'assistant',
  };
}

const EROS_HISTORY_PAGE_SIZE = 50;
const EROS_CURSOR_PREFIX = 'eros-before:';

export function encodeErosCursor(messageId: string): string {
  if (!MESSAGE_ID_PATTERN.test(messageId)) throw new ErosAdapterError('eros_request_invalid', 400);
  return `${EROS_CURSOR_PREFIX}${messageId}`;
}

export function decodeErosCursor(cursor: string | undefined): string | undefined {
  if (!cursor) return undefined;
  const match = /^eros-before:([A-Za-z0-9_-]{1,64})$/.exec(cursor);
  if (!match || !MESSAGE_ID_PATTERN.test(match[1])) throw new ErosAdapterError('eros_cursor_invalid', 400);
  return match[1];
}

function parseSafeCode(value: unknown): string {
  if (typeof value === 'string' && /^[a-z0-9_-]{1,64}$/i.test(value)) return `eros_${value.toLowerCase()}`;
  return 'eros_request_failed';
}

export class ErosEngineClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;
  private readonly secret: Uint8Array;
  private readonly workerSecret?: Uint8Array;
  private readonly workerTenantId?: string;

  constructor(options: ErosClientOptions) {
    const url = new URL(options.baseUrl);
    const localHosts = new Set(['localhost', '127.0.0.1', '::1', 'host.docker.internal', 'eros', 'gpoint-eros']);
    if (url.protocol !== 'http:' || !localHosts.has(url.hostname.toLowerCase()) || url.username || url.password || url.search || url.hash) {
      throw new Error('eros_service_url_invalid');
    }
    this.baseUrl = url.toString().replace(/\/$/, '');
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.timeoutMs = Math.max(1_000, Math.min(options.timeoutMs ?? 15_000, MAX_EROS_REQUEST_TIMEOUT_MS));
    this.secret = new TextEncoder().encode(options.jwtSecret);
    if (this.secret.byteLength < 32) throw new Error('eros_jwt_secret_too_short');
    if (options.workerSigningSecret !== undefined) {
      this.workerSecret = new TextEncoder().encode(options.workerSigningSecret);
      if (this.workerSecret.byteLength < 32) throw new Error('eros_worker_signing_secret_too_short');
    }
    if (options.workerTenantId !== undefined) {
      if (!UUID_PATTERN.test(options.workerTenantId)) throw new Error('eros_worker_tenant_invalid');
      this.workerTenantId = options.workerTenantId;
    }
  }

  private async bearer(userId: string): Promise<string> {
    return new SignJWT({ aud: 'eros-engine', iss: 'gpoint-eros-adapter' })
      .setProtectedHeader({ alg: 'HS256' })
      .setSubject(userId)
      .setIssuedAt()
      .setExpirationTime('2m')
      .sign(this.secret);
  }

  private async request(path: string, userId: string, body: unknown, accept = 'application/json'): Promise<Response> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: 'POST',
        headers: {
          authorization: `Bearer ${await this.bearer(userId)}`,
          'content-type': 'application/json',
          accept,
        },
        body: JSON.stringify(body),
        redirect: 'error',
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (error) {
      if (error instanceof Error && (error.name === 'TimeoutError' || error.name === 'AbortError')) {
        throw new ErosAdapterError('eros_timeout', 504);
      }
      throw new ErosAdapterError('eros_transport_error', 503);
    }
    if (!response.ok) {
      let code: unknown;
      try { code = (await response.json() as { code?: unknown }).code; } catch { /* hide upstream bodies */ }
      throw new ErosAdapterError(parseSafeCode(code), response.status >= 400 && response.status < 600 ? response.status : 503);
    }
    return response;
  }

  async startSession(userId: string, genomeId: string | null | undefined, instanceId?: string): Promise<{ sessionId: string; instanceId: string }> {
    if (!UUID_PATTERN.test(userId)) throw new ErosAdapterError('eros_identity_invalid', 400);
    if (instanceId && !UUID_PATTERN.test(instanceId)) throw new ErosAdapterError('eros_identity_invalid', 400);
    if (!instanceId && (typeof genomeId !== 'string' || !UUID_PATTERN.test(genomeId))) throw new ErosAdapterError('eros_identity_invalid', 400);
    const response = await this.request('/comp/chat/start', userId, instanceId ? { instance_id: instanceId } : { genome_id: genomeId });
    let value: ErosStartResponse;
    try { value = await response.json() as ErosStartResponse; } catch { throw new ErosAdapterError('eros_response_invalid'); }
    if (typeof value.session_id !== 'string' || !UUID_PATTERN.test(value.session_id) ||
        typeof value.instance_id !== 'string' || !UUID_PATTERN.test(value.instance_id)) {
      throw new ErosAdapterError('eros_response_invalid');
    }
    return { sessionId: value.session_id, instanceId: value.instance_id };
  }

  async sessions(userId: string): Promise<ErosSession[]> {
    if (!UUID_PATTERN.test(userId)) throw new ErosAdapterError('eros_identity_invalid', 400);
    const response = await this.fetchImpl(`${this.baseUrl}/comp/chat/${encodeURIComponent(userId)}/sessions`, {
      method: 'GET',
      headers: { authorization: `Bearer ${await this.bearer(userId)}`, accept: 'application/json' },
      redirect: 'error',
      signal: AbortSignal.timeout(this.timeoutMs),
    }).catch((error: unknown) => {
      if (error instanceof Error && (error.name === 'TimeoutError' || error.name === 'AbortError')) throw new ErosAdapterError('eros_timeout', 504);
      throw new ErosAdapterError('eros_transport_error', 503);
    });
    if (!response.ok) throw new ErosAdapterError('eros_sessions_unavailable', response.status >= 400 && response.status < 600 ? response.status : 503);
    let data: { sessions?: unknown };
    try { data = await response.json() as { sessions?: unknown }; } catch { throw new ErosAdapterError('eros_response_invalid'); }
    if (!Array.isArray(data.sessions) || data.sessions.length > 500) throw new ErosAdapterError('eros_response_invalid');
    return data.sessions.map((item) => {
      if (!item || typeof item !== 'object') throw new ErosAdapterError('eros_response_invalid');
      const session = item as Record<string, unknown>;
      if (typeof session.session_id !== 'string' || !UUID_PATTERN.test(session.session_id) ||
          !(session.instance_id === null || (typeof session.instance_id === 'string' && UUID_PATTERN.test(session.instance_id))) ||
          !['text', 'voice', 'group'].includes(String(session.channel)) ||
          (session.channel !== 'group' && session.instance_id === null) ||
          typeof session.last_active_at !== 'string' || !Number.isFinite(Date.parse(session.last_active_at))) {
        throw new ErosAdapterError('eros_response_invalid');
      }
      return { sessionId: session.session_id, instanceId: session.instance_id as string | null, lastActiveAt: session.last_active_at, channel: session.channel as ErosSession['channel'] };
    });
  }

  async sendMessage(
    userId: string,
    sessionId: string,
    clientMessageId: string,
    content: string,
    identity?: { creatorId: string; instanceId: string },
  ): Promise<{ id: string; text: string }> {
    if (!UUID_PATTERN.test(userId) || !UUID_PATTERN.test(sessionId) || !UUID_PATTERN.test(clientMessageId) || !content.trim() || content.length > 4_000) {
      throw new ErosAdapterError('eros_request_invalid', 400);
    }
    if (!identity || !UUID_PATTERN.test(identity.creatorId) || !UUID_PATTERN.test(identity.instanceId) ||
        !this.workerSecret || !this.workerTenantId) {
      throw new ErosAdapterError('eros_worker_identity_unavailable');
    }
    const bridgeIdentity = signBridgeIdentity({
      tenantId: this.workerTenantId,
      userId,
      creatorId: identity.creatorId,
      sessionId,
      instanceId: identity.instanceId,
      requestId: clientMessageId,
    }, this.workerSecret);
    const response = await this.request(`/comp/chat/${encodeURIComponent(sessionId)}/message/stream`, userId,
      { content, client_msg_id: clientMessageId, memory_scope: 'neutral_and_relationship', audit: { user: bridgeIdentity } }, 'text/event-stream');
    if (!response.body) throw new ErosAdapterError('eros_response_invalid');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let totalBytes = 0;
    let messageId = '';
    let actionType = '';
    let text = '';
    let sawDone = false;
    let sawFinal = false;
    let truncatedAttempt = false;
    let done = false;
    const acceptFrame = (frame: string) => {
      const payload = frame.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n');
      if (!payload) return;
      let event: Record<string, unknown>;
      try { event = JSON.parse(payload) as Record<string, unknown>; } catch { throw new ErosAdapterError('eros_response_invalid'); }
      if (event.type === 'meta') {
        if (sawDone && truncatedAttempt) text = '';
        sawDone = false;
        sawFinal = false;
        truncatedAttempt = false;
        if (typeof event.message_id === 'string') messageId = event.message_id;
        if (typeof event.action_type === 'string') actionType = event.action_type;
      } else if (event.type === 'delta' && typeof event.content === 'string') {
        text += event.content;
        if (text.length > MAX_REPLY_CHARS) throw new ErosAdapterError('eros_reply_too_large');
      } else if (event.type === 'error') {
        throw new ErosAdapterError(parseSafeCode(event.code));
      } else if (event.type === 'done') {
        sawDone = true;
        truncatedAttempt = event.truncated === true;
        if (event.ghost_fallback === true) throw new ErosAdapterError('eros_fallback_response');
      } else if (event.type === 'final') {
        if (!sawDone) throw new ErosAdapterError('eros_response_invalid');
        sawFinal = true;
      }
    };
    try {
      while (!done) {
        const chunk = await reader.read();
        done = chunk.done;
        if (chunk.value) {
          totalBytes += chunk.value.byteLength;
          if (totalBytes > MAX_STREAM_BYTES) throw new ErosAdapterError('eros_response_too_large');
          buffer += decoder.decode(chunk.value, { stream: !done });
        }
        const frames = buffer.replaceAll('\r\n', '\n').split('\n\n');
        buffer = frames.pop() ?? '';
        for (const frame of frames) acceptFrame(frame);
      }
      if (buffer.trim()) acceptFrame(buffer);
    } catch (error) {
      if (!done) await reader.cancel().catch(() => undefined);
      throw error;
    } finally {
      reader.releaseLock();
    }
    if (!sawDone || !sawFinal || truncatedAttempt || !MESSAGE_ID_PATTERN.test(messageId) || actionType !== 'reply' || !text.trim()) {
      throw new ErosAdapterError('eros_response_invalid');
    }
    return { id: messageId, text };
  }

  async history(userId: string, sessionId: string, limit = 50, before?: string): Promise<ErosHistoryPage> {
    if (!UUID_PATTERN.test(userId) || !UUID_PATTERN.test(sessionId) || !Number.isInteger(limit) || limit < 1 || limit > 100 || (before !== undefined && !MESSAGE_ID_PATTERN.test(before))) {
      throw new ErosAdapterError('eros_request_invalid', 400);
    }
    const pageSize = Math.min(limit, EROS_HISTORY_PAGE_SIZE);
    const params = new URLSearchParams({ limit: String(pageSize) });
    if (before) params.set('before', before);
    const response = await this.fetchImpl(`${this.baseUrl}/bff/v1/comp/chat/${encodeURIComponent(sessionId)}/history?${params}`, {
      method: 'GET',
      headers: { authorization: `Bearer ${await this.bearer(userId)}`, accept: 'application/json' },
      redirect: 'error',
      signal: AbortSignal.timeout(this.timeoutMs),
    }).catch((error: unknown) => {
      if (error instanceof Error && (error.name === 'TimeoutError' || error.name === 'AbortError')) throw new ErosAdapterError('eros_timeout', 504);
      throw new ErosAdapterError('eros_transport_error', 503);
    });
    if (!response.ok) {
      let code: unknown;
      try { code = (await response.json() as { code?: unknown }).code; } catch { /* hide upstream bodies */ }
      throw new ErosAdapterError(parseSafeCode(code), response.status >= 400 && response.status < 600 ? response.status : 503);
    }
    let data: { messages?: unknown; total?: unknown };
    try { data = await response.json() as { messages?: unknown; total?: unknown }; } catch { throw new ErosAdapterError('eros_response_invalid'); }
    if (!Array.isArray(data.messages) || data.messages.length > pageSize || data.total !== data.messages.length) {
      throw new ErosAdapterError('eros_response_invalid');
    }
    const messages = data.messages.map((item) => {
      if (!item || typeof item !== 'object') throw new ErosAdapterError('eros_response_invalid');
      const message = item as Record<string, unknown>;
      if (typeof message.id !== 'string' || !MESSAGE_ID_PATTERN.test(message.id) ||
          !['user', 'assistant', 'system_error'].includes(String(message.role)) ||
          typeof message.content !== 'string' || typeof message.sent_at !== 'string' || !Number.isFinite(Date.parse(message.sent_at))) {
        throw new ErosAdapterError('eros_response_invalid');
      }
      return {
        id: message.id,
        role: message.role as string,
        content: message.content,
        sent_at: message.sent_at,
        ...(typeof message.read_at === 'string' ? { read_at: message.read_at } : {}),
        ...(typeof message.client_msg_id === 'string' && UUID_PATTERN.test(message.client_msg_id) ? { client_msg_id: message.client_msg_id } : {}),
      };
    });
    return { messages, total: data.total as number };
  }
}

export function createErosEngineClient(env: NodeJS.ProcessEnv = process.env, fetchImpl?: typeof fetch): ErosEngineClient | null {
  if (env.EROS_ENABLED !== 'true') return null;
  if (!env.EROS_SERVICE_URL || !env.EROS_JWT_SECRET) throw new ErosAdapterError('eros_configuration_incomplete');
  let timeoutMs: number | undefined;
  if (env.EROS_REQUEST_TIMEOUT_MS !== undefined) {
    if (!/^[0-9]{1,6}$/.test(env.EROS_REQUEST_TIMEOUT_MS)) throw new ErosAdapterError('eros_timeout_invalid', 500);
    timeoutMs = Number(env.EROS_REQUEST_TIMEOUT_MS);
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1_000 || timeoutMs > MAX_EROS_REQUEST_TIMEOUT_MS) {
      throw new ErosAdapterError('eros_timeout_invalid', 500);
    }
  }
  return new ErosEngineClient({
    baseUrl: env.EROS_SERVICE_URL,
    jwtSecret: env.EROS_JWT_SECRET,
    workerSigningSecret: env.EROS_WORKER_SIGNING_SECRET,
    workerTenantId: env.EROS_WORKER_TENANT_ID,
    timeoutMs,
    fetchImpl,
  });
}
