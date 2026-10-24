import { createCipheriv, createDecipheriv, createHash, createHmac, hkdfSync, randomBytes, randomUUID, timingSafeEqual } from 'node:crypto';
import { BadRequestException, Body, Controller, Get, Headers, Header, HttpException, OnModuleDestroy, OnModuleInit, Post, Req, Res, UnauthorizedException } from '@nestjs/common';
import type { FastifyReply, FastifyRequest } from 'fastify';
import { PrismaClient, type Prisma } from '@prisma/client';
import { buildChatCompletionSse, buildOpenAiEmbeddingResponse, deriveBridgeInferenceIdentity, parseBridgeRequest, parseErosEmbeddingRequest, type ParsedErosBridgeRequest } from './eros-worker-contract.js';
import { isAllowedOllamaEmbeddingEndpoint } from './eros-network-policy.js';

const prisma = new PrismaClient();
const REPLY_LIMIT = 4_096;
const POLL_MS = 500;
const MAX_OUTSTANDING_JOBS = 16;
const MAX_RETAINED_JOBS = 272;
const IDEMPOTENCY_RETENTION = '10 minutes';
type WorkerTx = Prisma.TransactionClient;
type JobRow = {
  id: string; request_id: string; request_hash: string; tenant_id: string; user_id: string; creator_id: string | null;
  session_id: string; instance_id: string; job_type: string; request: Record<string, unknown>; status: string;
  lease_token: string | null; deadline_at: Date; response_text: string | null; error_code: string | null;
};

type ErosQueueIdentity = Pick<JobRow, 'id' | 'request_id' | 'tenant_id' | 'user_id' | 'creator_id' | 'session_id' | 'instance_id'>;
type EncryptedQueueRequest = { v: 1; alg: 'A256GCM'; nonce: string; ciphertext: string; tag: string };

function bearer(headers: Record<string, string | string[] | undefined>, key: string, env = process.env): void {
  const configured = env[key];
  const supplied = headers.authorization;
  if (!configured || typeof supplied !== 'string' || !supplied.startsWith('Bearer ')) throw new UnauthorizedException();
  const expected = Buffer.from(configured);
  const actual = Buffer.from(supplied.slice(7));
  if (expected.length !== actual.length || !timingSafeEqual(expected, actual)) throw new UnauthorizedException();
}

function signingSecret(env = process.env): Uint8Array {
  const value = env.EROS_WORKER_SIGNING_SECRET;
  if (!value || Buffer.byteLength(value) < 32) throw new HttpException({ code: 'EROS_BRIDGE_UNAVAILABLE' }, 503);
  return new TextEncoder().encode(value);
}

function workerTenant(env = process.env): string {
  const value = env.EROS_WORKER_TENANT_ID ?? '';
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  if (!uuid.test(value)) {
    throw new HttpException({ code: 'EROS_BRIDGE_UNAVAILABLE' }, 503);
  }
  return value;
}

function queueEncryptionKey(env = process.env): Buffer {
  const secret = signingSecret(env);
  return Buffer.from(hkdfSync('sha256', secret, Buffer.from('gpoint-eros-queue-v1'), Buffer.from('request-payload-aead'), 32));
}

function queueAad(identity: ErosQueueIdentity, purpose: 'request' | 'response' = 'request'): Buffer {
  return Buffer.from([
    identity.tenant_id, identity.id, identity.request_id, identity.user_id,
    identity.creator_id ?? '', identity.session_id, identity.instance_id, purpose,
  ].join('\0'));
}

function encryptQueueRequest(request: ParsedErosBridgeRequest, identity: ErosQueueIdentity): EncryptedQueueRequest {
  const nonce = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', queueEncryptionKey(), nonce);
  cipher.setAAD(queueAad(identity));
  const ciphertext = Buffer.concat([
    cipher.update(JSON.stringify({ model: request.model, messages: request.messages, max_tokens: request.maxTokens, temperature: request.temperature }), 'utf8'),
    cipher.final(),
  ]);
  return { v: 1, alg: 'A256GCM', nonce: nonce.toString('base64url'), ciphertext: ciphertext.toString('base64url'), tag: cipher.getAuthTag().toString('base64url') };
}

function decryptQueueRequest(value: unknown, identity: ErosQueueIdentity): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new HttpException({ code: 'EROS_QUEUE_PAYLOAD_INVALID' }, 503);
  const envelope = value as Partial<EncryptedQueueRequest>;
  if (envelope.v !== 1 || envelope.alg !== 'A256GCM' || typeof envelope.nonce !== 'string' ||
      typeof envelope.ciphertext !== 'string' || typeof envelope.tag !== 'string') {
    throw new HttpException({ code: 'EROS_QUEUE_PAYLOAD_INVALID' }, 503);
  }
  try {
    const nonce = Buffer.from(envelope.nonce, 'base64url');
    const ciphertext = Buffer.from(envelope.ciphertext, 'base64url');
    const tag = Buffer.from(envelope.tag, 'base64url');
    if (nonce.length !== 12 || tag.length !== 16 || ciphertext.length > 96_000) throw new Error('invalid_envelope');
    const decipher = createDecipheriv('aes-256-gcm', queueEncryptionKey(), nonce);
    decipher.setAAD(queueAad(identity));
    decipher.setAuthTag(tag);
    const plaintext = Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
    const request = JSON.parse(plaintext);
    if (!request || typeof request !== 'object' || Array.isArray(request)) throw new Error('invalid_payload');
    return request as Record<string, unknown>;
  } catch {
    throw new HttpException({ code: 'EROS_QUEUE_PAYLOAD_INVALID' }, 503);
  }
}

function encryptQueueResponse(text: string, identity: ErosQueueIdentity): string {
  const nonce = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', queueEncryptionKey(), nonce);
  cipher.setAAD(queueAad(identity, 'response'));
  const ciphertext = Buffer.concat([cipher.update(text, 'utf8'), cipher.final()]);
  return JSON.stringify({
    v: 1, alg: 'A256GCM', nonce: nonce.toString('base64url'),
    ciphertext: ciphertext.toString('base64url'), tag: cipher.getAuthTag().toString('base64url'),
  } satisfies EncryptedQueueRequest);
}

function decryptQueueResponse(value: string, identity: ErosQueueIdentity): string {
  try {
    const envelope = JSON.parse(value) as Partial<EncryptedQueueRequest>;
    if (envelope.v !== 1 || envelope.alg !== 'A256GCM' || typeof envelope.nonce !== 'string' ||
        typeof envelope.ciphertext !== 'string' || typeof envelope.tag !== 'string') throw new Error('invalid_envelope');
    const nonce = Buffer.from(envelope.nonce, 'base64url');
    const ciphertext = Buffer.from(envelope.ciphertext, 'base64url');
    const tag = Buffer.from(envelope.tag, 'base64url');
    if (nonce.length !== 12 || tag.length !== 16 || ciphertext.length > REPLY_LIMIT * 4) throw new Error('invalid_envelope');
    const decipher = createDecipheriv('aes-256-gcm', queueEncryptionKey(), nonce);
    decipher.setAAD(queueAad(identity, 'response'));
    decipher.setAuthTag(tag);
    return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
  } catch {
    throw new HttpException({ code: 'EROS_QUEUE_PAYLOAD_INVALID' }, 503);
  }
}

function decodeJobResponse(row: JobRow): JobRow {
  if (row.response_text === null) return row;
  const identity: ErosQueueIdentity = {
    id: row.id, request_id: row.request_id, tenant_id: row.tenant_id, user_id: row.user_id,
    creator_id: row.creator_id, session_id: row.session_id, instance_id: row.instance_id,
  };
  return { ...row, response_text: decryptQueueResponse(row.response_text, identity) };
}

async function withWorkerTenant<T>(operation: (tx: WorkerTx) => Promise<T>): Promise<T> {
  const tenantId = workerTenant();
  return prisma.$transaction(async (tx) => {
    await tx.$queryRawUnsafe('SELECT set_config(\'app.tenant_id\', $1, true)', tenantId);
    return operation(tx);
  });
}

function requestFingerprint(request: ParsedErosBridgeRequest): string {
  return createHmac('sha256', queueEncryptionKey()).update(JSON.stringify({
    model: request.model, messages: request.messages, max_tokens: request.maxTokens, temperature: request.temperature,
  })).digest('hex');
}

function systemTaskIdentity(request: ParsedErosBridgeRequest, tenantId: string) {
  const bytes = createHash('sha256').update(tenantId).update('\0').update(requestFingerprint(request)).digest().subarray(0, 16);
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString('hex');
  return {
    tenantId,
    userId: '00000000-0000-4000-8000-000000000001',
    creatorId: '00000000-0000-4000-8000-000000000004',
    sessionId: '00000000-0000-4000-8000-000000000002',
    instanceId: '00000000-0000-4000-8000-000000000003',
    requestId: `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`,
  };
}

function safeErrorFrame(code: string): string {
  return `data: ${JSON.stringify({ error: { message: 'Worker request failed', type: 'server_error', code } })}\n\ndata: [DONE]\n\n`;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** Claim Eros bridge work on the existing authenticated GPOINT Product Worker contract. */
export async function claimErosForGpointWorker(): Promise<{ job: Record<string, unknown> } | null> {
  const tenantId = workerTenant();
  const row = await withWorkerTenant(async (tx) => {
    await tx.$queryRawUnsafe(
      'SELECT pg_advisory_xact_lock(hashtextextended($1, 0)) IS NULL AS locked', tenantId,
    );
    await tx.$executeRawUnsafe(
      `UPDATE engine.eros_inference_jobs SET status = 'EXPIRED', error_code = 'lease_expired',
         lease_token = NULL, lease_expires_at = NULL, updated_at = now()
       WHERE tenant_id = $1 AND status = 'RUNNING' AND lease_expires_at <= now()`, tenantId,
    );
    await tx.$executeRawUnsafe(
      `UPDATE engine.eros_inference_jobs SET status = 'EXPIRED', error_code = 'deadline_expired', updated_at = now()
       WHERE tenant_id = $1 AND status = 'PENDING' AND deadline_at <= now()`, tenantId,
    );
    const lease = randomUUID();
    const rows = await tx.$queryRawUnsafe<JobRow[]>(
      `WITH candidate AS (
         SELECT id FROM engine.eros_inference_jobs
         WHERE tenant_id = $1 AND status = 'PENDING' AND deadline_at > now()
           AND NOT EXISTS (SELECT 1 FROM engine.eros_inference_jobs WHERE tenant_id = $1 AND status = 'RUNNING')
         ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1
       )
       UPDATE engine.eros_inference_jobs AS job SET status = 'RUNNING', lease_token = $2::uuid,
         lease_expires_at = job.deadline_at, attempts = attempts + 1, updated_at = now()
       FROM candidate WHERE job.id = candidate.id
       RETURNING job.id::text, job.request_id::text, job.request_hash, job.tenant_id,
         job.user_id::text, job.creator_id::text, job.session_id::text, job.instance_id::text, job.job_type, job.request,
         job.status, job.lease_token::text, job.deadline_at, job.response_text, job.error_code`, tenantId, lease,
    );
    return rows[0];
  });
  if (!row) return null;
  const identity: ErosQueueIdentity = {
    id: row.id, request_id: row.request_id, tenant_id: row.tenant_id, user_id: row.user_id,
    creator_id: row.creator_id, session_id: row.session_id, instance_id: row.instance_id,
  };
  return { job: {
    id: row.id, leaseToken: row.lease_token, attempt: 1, target: 'GPOINT', tenant_id: tenantId,
    request: decryptQueueRequest(row.request, identity), purpose: 'eros_chat_completion',
  } };
}

export async function completeErosForGpointWorker(input: {
  job_id: string; lease_token: string; text: string; model: string;
}): Promise<{ handled: boolean }> {
  const tenantId = workerTenant();
  return withWorkerTenant(async (tx) => {
    const existing = await tx.$queryRawUnsafe<ErosQueueIdentity[]>(
      `SELECT id::text, request_id::text, tenant_id, user_id::text, creator_id::text,
              session_id::text, instance_id::text
       FROM engine.eros_inference_jobs WHERE id = $1::uuid AND tenant_id = $2`, input.job_id, tenantId,
    );
    if (!existing[0]) return { handled: false };
    const responseIdentity: ErosQueueIdentity = existing[0];
    const count = await tx.$executeRawUnsafe(
      `UPDATE engine.eros_inference_jobs SET status = 'COMPLETED', response_text = $3,
         lease_token = NULL, lease_expires_at = NULL, updated_at = now()
      WHERE id = $1::uuid AND lease_token = $2::uuid AND tenant_id = $4 AND status = 'RUNNING'
         AND deadline_at > now()`, input.job_id, input.lease_token,
      encryptQueueResponse(input.text, responseIdentity), tenantId,
    );
    if (count !== 1) throw new HttpException({ code: 'WORKER_LEASE_REJECTED' }, 409);
    return { handled: true };
  });
}

export async function failErosForGpointWorker(input: {
  job_id: string; lease_token: string; error_code: string;
}): Promise<{ handled: boolean }> {
  const tenantId = workerTenant();
  return withWorkerTenant(async (tx) => {
    const existing = await tx.$queryRawUnsafe<Array<{ id: string }>>(
      `SELECT id::text FROM engine.eros_inference_jobs WHERE id = $1::uuid AND tenant_id = $2`, input.job_id, tenantId,
    );
    if (!existing[0]) return { handled: false };
    const count = await tx.$executeRawUnsafe(
      `UPDATE engine.eros_inference_jobs SET status = 'FAILED', error_code = $3,
         lease_token = NULL, lease_expires_at = NULL, updated_at = now()
       WHERE id = $1::uuid AND lease_token = $2::uuid AND tenant_id = $4 AND status = 'RUNNING'`,
      input.job_id, input.lease_token, input.error_code, tenantId,
    );
    if (count !== 1) throw new HttpException({ code: 'WORKER_LEASE_REJECTED' }, 409);
    return { handled: true };
  });
}

export async function checkErosForGpointWorker(input: {
  job_id: string; lease_token: string;
}): Promise<{ active: boolean; terminal_status: string | null }> {
  const tenantId = workerTenant();
  return withWorkerTenant(async (tx) => {
    await tx.$executeRawUnsafe(
      `UPDATE engine.eros_inference_jobs SET status = 'EXPIRED', lease_token = NULL,
         lease_expires_at = NULL, error_code = 'deadline_expired', updated_at = now()
       WHERE id = $1::uuid AND tenant_id = $2 AND status = 'RUNNING' AND deadline_at <= now()`, input.job_id, tenantId,
    );
    const rows = await tx.$queryRawUnsafe<Array<{ active: boolean; status: string }>>(
      `SELECT (status = 'RUNNING' AND lease_token = $2::uuid AND deadline_at > now()) AS active, status
       FROM engine.eros_inference_jobs WHERE id = $1::uuid AND tenant_id = $3`, input.job_id, input.lease_token, tenantId,
    );
    const row = rows[0];
    return { active: row?.active === true, terminal_status: row && ['CANCELLED', 'EXPIRED'].includes(row.status) ? row.status : null };
  });
}

export async function erosQueueStatusForGpointWorker(): Promise<{ pending_jobs: number; running_jobs: number }> {
  const tenantId = workerTenant();
  const rows = await withWorkerTenant((tx) => tx.$queryRawUnsafe<Array<{ pending: bigint; running: bigint }>>(
    `SELECT count(*) FILTER (WHERE status = 'PENDING')::bigint AS pending,
            count(*) FILTER (WHERE status = 'RUNNING')::bigint AS running
     FROM engine.eros_inference_jobs WHERE tenant_id = $1`, tenantId,
  ));
  return { pending_jobs: Number(rows[0]?.pending ?? 0), running_jobs: Number(rows[0]?.running ?? 0) };
}

@Controller('eros-worker')
export class ErosWorkerGatewayController implements OnModuleInit, OnModuleDestroy {
  private janitor?: NodeJS.Timeout;

  onModuleInit(): void {
    this.janitor = setInterval(() => { void this.cleanupQueue().catch(() => undefined); }, 60_000);
    this.janitor.unref();
    void this.cleanupQueue().catch(() => undefined);
  }

  onModuleDestroy(): void {
    if (this.janitor) clearInterval(this.janitor);
  }

  private async cleanupQueue(): Promise<void> {
    await withWorkerTenant(async (tx) => {
      const tenantId = workerTenant();
      await tx.$executeRawUnsafe(
        `UPDATE engine.eros_inference_jobs SET status = 'EXPIRED', lease_token = NULL, lease_expires_at = NULL,
           error_code = 'deadline_expired', updated_at = now()
         WHERE tenant_id = $1 AND status IN ('PENDING', 'RUNNING') AND deadline_at <= now()`, tenantId,
      );
      await tx.$executeRawUnsafe(
        `DELETE FROM engine.eros_inference_jobs WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED')
           AND tenant_id = $1 AND updated_at < now() - ($2::text)::interval`, tenantId, IDEMPOTENCY_RETENTION,
      );
    });
  }

  private async createOrReadJob(request: ParsedErosBridgeRequest): Promise<JobRow> {
    const tenantId = workerTenant();
    const fingerprint = requestFingerprint(request);
    const identity = request.identity
      ? deriveBridgeInferenceIdentity(request.identity, fingerprint)
      : systemTaskIdentity(request, tenantId);
    if (identity.tenantId !== tenantId) throw new HttpException({ code: 'EROS_IDENTITY_MISMATCH' }, 403);
    const id = randomUUID();
    const timeoutRaw = Number(process.env.EROS_BRIDGE_TIMEOUT_SECONDS ?? '210');
    if (!Number.isInteger(timeoutRaw) || timeoutRaw < 30 || timeoutRaw > 240) throw new HttpException({ code: 'EROS_BRIDGE_UNAVAILABLE' }, 503);
    const deadline = new Date(Date.now() + timeoutRaw * 1000);
    const row = await withWorkerTenant(async (tx) => {
      // Serialize admission for this tenant so both the pending and retained
      // bounds remain true under concurrent Eros requests.
      await tx.$queryRawUnsafe(
        'SELECT pg_advisory_xact_lock(hashtextextended($1, 0)) IS NULL AS locked', tenantId,
      );
      await tx.$executeRawUnsafe(
        `UPDATE engine.eros_inference_jobs SET status = 'EXPIRED', lease_token = NULL, lease_expires_at = NULL,
           error_code = 'deadline_expired', updated_at = now()
         WHERE tenant_id = $1 AND status IN ('PENDING', 'RUNNING') AND deadline_at <= now()`, tenantId,
      );
      await tx.$executeRawUnsafe(
        `DELETE FROM engine.eros_inference_jobs WHERE tenant_id = $1
           AND status IN ('COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED')
           AND updated_at < now() - ($2::text)::interval`, tenantId, IDEMPOTENCY_RETENTION,
      );
      const existingRows = await tx.$queryRawUnsafe<JobRow[]>(
        `SELECT id::text, request_id::text, request_hash, tenant_id, user_id::text, creator_id::text, session_id::text,
                instance_id::text, job_type, request, status, lease_token::text, deadline_at, response_text, error_code
         FROM engine.eros_inference_jobs WHERE request_id = $1::uuid`, identity.requestId,
      );
      if (existingRows[0]) return existingRows[0];
      const capacity = await tx.$queryRawUnsafe<Array<{ outstanding: bigint; retained: bigint }>>(
        `SELECT count(*) FILTER (WHERE status IN ('PENDING', 'RUNNING'))::bigint AS outstanding,
                count(*)::bigint AS retained FROM engine.eros_inference_jobs WHERE tenant_id = $1`, tenantId,
      );
      if (Number(capacity[0]?.outstanding ?? 0) >= MAX_OUTSTANDING_JOBS) {
        throw new HttpException({ code: 'EROS_QUEUE_FULL' }, 429);
      }
      if (Number(capacity[0]?.retained ?? 0) >= MAX_RETAINED_JOBS) {
        throw new HttpException({ code: 'EROS_QUEUE_RETENTION_FULL' }, 429);
      }
      const jobType = request.identity ? 'CHAT_COMPLETION' : 'SYSTEM_COMPLETION';
      const queueIdentity: ErosQueueIdentity = {
        id, request_id: identity.requestId, tenant_id: tenantId, user_id: identity.userId,
        creator_id: identity.creatorId, session_id: identity.sessionId, instance_id: identity.instanceId,
      };
      await tx.$executeRawUnsafe(
        `INSERT INTO engine.eros_inference_jobs
          (id, request_id, request_hash, tenant_id, user_id, creator_id, session_id, instance_id, job_type, request, deadline_at)
         VALUES ($1::uuid, $2::uuid, $3, $4, $5::uuid, $6::uuid, $7::uuid, $8::uuid, $9, $10::jsonb, $11::timestamptz)`,
        id, identity.requestId, fingerprint, tenantId, identity.userId, identity.creatorId,
        identity.sessionId, identity.instanceId, jobType,
        JSON.stringify(encryptQueueRequest(request, queueIdentity)), deadline,
      );
      const rows = await tx.$queryRawUnsafe<JobRow[]>(
        `SELECT id::text, request_id::text, request_hash, tenant_id, user_id::text, creator_id::text, session_id::uuid::text,
                instance_id::text, job_type, request, status, lease_token::text, deadline_at, response_text, error_code
         FROM engine.eros_inference_jobs WHERE request_id = $1::uuid`, identity.requestId,
      );
      return rows[0];
    });
    if (!row) throw new HttpException({ code: 'EROS_BRIDGE_UNAVAILABLE' }, 503);
    if (row.request_hash !== fingerprint || row.user_id !== identity.userId || row.creator_id !== identity.creatorId ||
        row.session_id !== identity.sessionId || row.instance_id !== identity.instanceId ||
        row.job_type !== (request.identity ? 'CHAT_COMPLETION' : 'SYSTEM_COMPLETION') ||
        row.tenant_id !== tenantId) throw new HttpException({ code: 'EROS_IDEMPOTENCY_CONFLICT' }, 409);
    return decodeJobResponse(row);
  }

  private async expireJob(id: string): Promise<void> {
    await withWorkerTenant((tx) => tx.$executeRawUnsafe(
      `UPDATE engine.eros_inference_jobs SET status = 'EXPIRED', lease_token = NULL, lease_expires_at = NULL,
         error_code = 'deadline_expired', updated_at = now()
       WHERE id = $1::uuid AND tenant_id = $2 AND status IN ('PENDING', 'RUNNING') AND deadline_at <= now()`, id, workerTenant(),
    ));
  }

  private async cancelJob(id: string): Promise<void> {
    await withWorkerTenant((tx) => tx.$executeRawUnsafe(
      `UPDATE engine.eros_inference_jobs SET status = 'CANCELLED', lease_token = NULL, lease_expires_at = NULL,
         error_code = 'client_cancelled', updated_at = now()
       WHERE id = $1::uuid AND tenant_id = $2 AND status IN ('PENDING', 'RUNNING')`, id, workerTenant(),
    ));
  }

  protected async readJob(id: string): Promise<JobRow | undefined> {
    const rows = await withWorkerTenant((tx) => tx.$queryRawUnsafe<JobRow[]>(
      `SELECT id::text, request_id::text, request_hash, tenant_id, user_id::text, creator_id::text, session_id::text,
              instance_id::text, job_type, request, status, lease_token::text, deadline_at, response_text, error_code
       FROM engine.eros_inference_jobs WHERE id = $1::uuid AND tenant_id = $2`, id, workerTenant(),
    ));
    return rows[0] ? decodeJobResponse(rows[0]) : undefined;
  }

  private async waitForJob(id: string, disconnected: () => boolean, keepAlive: () => void): Promise<JobRow> {
    let lastKeepAlive = Date.now();
    while (true) {
      if (disconnected()) {
        await this.cancelJob(id);
        throw new HttpException({ code: 'EROS_CLIENT_DISCONNECTED' }, 499);
      }
      await this.expireJob(id);
      const row = await this.readJob(id);
      if (!row) throw new HttpException({ code: 'EROS_BRIDGE_UNAVAILABLE' }, 503);
      if (row.status === 'COMPLETED' && typeof row.response_text === 'string') return row;
      if (['FAILED', 'CANCELLED', 'EXPIRED'].includes(row.status)) {
        throw new HttpException({ code: row.error_code ?? 'EROS_WORKER_FAILED' }, 503);
      }
      if (Date.now() - lastKeepAlive >= 15_000) {
        keepAlive();
        lastKeepAlive = Date.now();
      }
      await sleep(POLL_MS);
    }
  }

  @Post('v1/chat/completions')
  async complete(
    @Headers() headers: Record<string, string | string[] | undefined>,
    @Body() body: unknown,
    @Req() request: FastifyRequest,
    @Res() reply: FastifyReply,
  ) {
    bearer(headers, 'EROS_BRIDGE_API_KEY');
    let parsed: ParsedErosBridgeRequest;
    try {
      parsed = await parseBridgeRequest(body, signingSecret());
    } catch (error) {
      if (error instanceof HttpException) throw error;
      if (error instanceof Error && error.message === 'eros_bridge_identity_invalid') throw new UnauthorizedException();
      throw new BadRequestException({ code: 'EROS_CHAT_REQUEST_INVALID' });
    }
    let connected = !request.raw.aborted && !reply.raw.destroyed;
    const onAbort = () => { connected = false; };
    const onClose = () => { if (!reply.raw.writableFinished) connected = false; };
    request.raw.once('aborted', onAbort);
    reply.raw.once('close', onClose);
    try {
      const job = await this.createOrReadJob(parsed);
      if (job.status === 'FAILED' || job.status === 'CANCELLED' || job.status === 'EXPIRED') {
        throw new HttpException({ code: job.error_code ?? 'EROS_WORKER_FAILED' }, 503);
      }
      if (!connected) {
        await this.cancelJob(job.id);
        return reply.raw.destroy();
      }
      if (parsed.stream) {
        reply.raw.statusCode = 200;
        reply.raw.setHeader('content-type', 'text/event-stream; charset=utf-8');
        reply.raw.setHeader('cache-control', 'no-cache, no-transform');
        reply.raw.setHeader('connection', 'keep-alive');
        reply.raw.flushHeaders();
      }
      const result = job.status === 'COMPLETED' && job.response_text !== null
        ? job
        : await this.waitForJob(job.id, () => !connected, () => {
            if (parsed.stream && connected) reply.raw.write(': keep-alive\n\n');
          });
      const text = result.response_text;
      if (typeof text !== 'string' || !text.trim() || text.length > REPLY_LIMIT) throw new HttpException({ code: 'EROS_WORKER_RESPONSE_INVALID' }, 502);
      if (!parsed.stream) {
        return reply.status(200).send({
          id: `chatcmpl-${job.id}`, object: 'chat.completion', created: Math.floor(Date.now() / 1000), model: parsed.model,
          choices: [{ index: 0, message: { role: 'assistant', content: text }, finish_reason: 'stop' }],
        });
      }
      reply.raw.write(buildChatCompletionSse({ id: `chatcmpl-${job.id}`, model: parsed.model, text }));
      return reply.raw.end();
    } catch (error) {
      if (error instanceof HttpException && error.getStatus() === 499) return reply.raw.destroy();
      if (parsed.stream && reply.raw.headersSent && !reply.raw.writableEnded && connected) {
        reply.raw.write(safeErrorFrame('worker_request_failed'));
        return reply.raw.end();
      }
      throw error;
    } finally {
      request.raw.off('aborted', onAbort);
      reply.raw.off('close', onClose);
    }
  }

  @Post('v1/embeddings')
  async embeddings(
    @Headers() headers: Record<string, string | string[] | undefined>,
    @Body() body: unknown,
  ) {
    bearer(headers, 'EROS_EMBEDDING_API_KEY');
    let parsed;
    try { parsed = parseErosEmbeddingRequest(body); }
    catch { throw new BadRequestException({ code: 'EROS_EMBEDDING_REQUEST_INVALID' }); }
    const endpoint = process.env.EROS_OLLAMA_EMBEDDING_URL ?? '';
    const localMode = process.env.EROS_WORKER_LOCAL_TEST === 'true';
    if (!isAllowedOllamaEmbeddingEndpoint(endpoint, { localTest: localMode })) {
      throw new HttpException({ code: 'EROS_EMBEDDINGS_UNAVAILABLE' }, 503);
    }
    const url = new URL(endpoint);
    let result: unknown;
    try {
      const response = await fetch(url, {
        method: 'POST', redirect: 'error', signal: AbortSignal.timeout(60_000),
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ model: 'qwen3-embedding:0.6b', input: parsed.input, dimensions: 512, truncate: false }),
      });
      if (!response.ok) throw new Error('ollama_embedding_status');
      result = await response.json();
    } catch {
      throw new HttpException({ code: 'EROS_EMBEDDINGS_UNAVAILABLE' }, 502);
    }
    const embeddings = result && typeof result === 'object' ? (result as { embeddings?: unknown }).embeddings : undefined;
    try { return buildOpenAiEmbeddingResponse(parsed.model, embeddings); }
    catch { throw new HttpException({ code: 'EROS_EMBEDDING_RESPONSE_INVALID' }, 502); }
  }

}
