import { createHash, randomUUID, timingSafeEqual } from 'node:crypto';
import { BadRequestException, Body, Controller, Get, Headers, Header, HttpException, OnModuleDestroy, OnModuleInit, Post, Req, Res, UnauthorizedException } from '@nestjs/common';
import type { FastifyReply, FastifyRequest } from 'fastify';
import { PrismaClient, type Prisma } from '@prisma/client';
import { buildChatCompletionSse, buildOpenAiEmbeddingResponse, parseBridgeRequest, parseErosEmbeddingRequest, type ParsedErosBridgeRequest } from './eros-worker-contract.js';

const prisma = new PrismaClient();
const REPLY_LIMIT = 4_096;
const POLL_MS = 500;
const MAX_OUTSTANDING_JOBS = 16;
const MAX_RETAINED_JOBS = 272;
const IDEMPOTENCY_RETENTION = '10 minutes';
type WorkerTx = Prisma.TransactionClient;
type JobRow = {
  id: string; request_id: string; request_hash: string; tenant_id: string; user_id: string; creator_id: string;
  session_id: string; instance_id: string; job_type: string; request: Record<string, unknown>; status: string;
  lease_token: string | null; deadline_at: Date; response_text: string | null; error_code: string | null;
};

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
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    throw new HttpException({ code: 'EROS_BRIDGE_UNAVAILABLE' }, 503);
  }
  return value;
}

async function withWorkerTenant<T>(operation: (tx: WorkerTx) => Promise<T>): Promise<T> {
  const tenantId = workerTenant();
  return prisma.$transaction(async (tx) => {
    await tx.$queryRawUnsafe('SELECT set_config(\'app.tenant_id\', $1, true)', tenantId);
    return operation(tx);
  });
}

function requestFingerprint(request: ParsedErosBridgeRequest): string {
  return createHash('sha256').update(JSON.stringify({
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
    const identity = request.identity ?? systemTaskIdentity(request, tenantId);
    if (identity.tenantId !== tenantId) throw new HttpException({ code: 'EROS_IDENTITY_MISMATCH' }, 403);
    const id = randomUUID();
    const timeoutRaw = Number(process.env.EROS_BRIDGE_TIMEOUT_SECONDS ?? '210');
    if (!Number.isInteger(timeoutRaw) || timeoutRaw < 30 || timeoutRaw > 240) throw new HttpException({ code: 'EROS_BRIDGE_UNAVAILABLE' }, 503);
    const deadline = new Date(Date.now() + timeoutRaw * 1000);
    const fingerprint = requestFingerprint(request);
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
      await tx.$executeRawUnsafe(
        `INSERT INTO engine.eros_inference_jobs
          (id, request_id, request_hash, tenant_id, user_id, creator_id, session_id, instance_id, job_type, request, deadline_at)
         VALUES ($1::uuid, $2::uuid, $3, $4, $5::uuid, $6::uuid, $7::uuid, $8::uuid, $9, $10::jsonb, $11::timestamptz)`,
        id, identity.requestId, fingerprint, tenantId, identity.userId, identity.creatorId,
        identity.sessionId, identity.instanceId, jobType,
        JSON.stringify({ model: request.model, messages: request.messages, max_tokens: request.maxTokens, temperature: request.temperature }), deadline,
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
    return row;
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

  private async readJob(id: string): Promise<JobRow | undefined> {
    const rows = await withWorkerTenant((tx) => tx.$queryRawUnsafe<JobRow[]>(
      `SELECT id::text, request_id::text, request_hash, tenant_id, user_id::text, creator_id::text, session_id::text,
              instance_id::text, job_type, request, status, lease_token::text, deadline_at, response_text, error_code
       FROM engine.eros_inference_jobs WHERE id = $1::uuid AND tenant_id = $2`, id, workerTenant(),
    ));
    return rows[0];
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
    bearer(headers, 'GPOINT_API_KEY');
    let parsed;
    try { parsed = parseErosEmbeddingRequest(body); }
    catch { throw new BadRequestException({ code: 'EROS_EMBEDDING_REQUEST_INVALID' }); }
    const endpoint = process.env.EROS_OLLAMA_EMBEDDING_URL ?? '';
    let url: URL;
    try { url = new URL(endpoint); }
    catch { throw new HttpException({ code: 'EROS_EMBEDDINGS_UNAVAILABLE' }, 503); }
    const localMode = process.env.EROS_WORKER_LOCAL_TEST === 'true';
    if (url.username || url.password || url.search || url.hash ||
        !(url.protocol === 'https:' || (localMode && url.protocol === 'http:' && url.hostname === '127.0.0.1')) ||
        !url.pathname.endsWith('/api/embed')) {
      throw new HttpException({ code: 'EROS_EMBEDDINGS_UNAVAILABLE' }, 503);
    }
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

  @Post('health')
  @Header('Cache-Control', 'no-store')
  health(@Headers() headers: Record<string, string | string[] | undefined>) {
    bearer(headers, 'EROS_WORKER_TOKEN');
    return { status: 'ready', lane: 'EROS', source_id: 'gpoint-eros-worker-v1' };
  }

  @Get('queue-status')
  @Header('Cache-Control', 'no-store')
  async queueStatus(@Headers() headers: Record<string, string | string[] | undefined>) {
    bearer(headers, 'EROS_WORKER_TOKEN');
    const rows = await withWorkerTenant((tx) => tx.$queryRawUnsafe<Array<{ pending: bigint; running: bigint; snapshot: Date }>>(
      `SELECT count(*) FILTER (WHERE status = 'PENDING')::bigint AS pending,
              count(*) FILTER (WHERE status = 'RUNNING')::bigint AS running, now() AS snapshot
       FROM engine.eros_inference_jobs WHERE tenant_id = $1`, workerTenant(),
    ));
    return {
      source_id: 'eros-worker-queue-status-v1:eros', lane: 'EROS',
      pending_jobs: Number(rows[0]?.pending ?? 0), running_jobs: Number(rows[0]?.running ?? 0),
      snapshot_at: rows[0]?.snapshot.toISOString() ?? new Date().toISOString(),
    };
  }

  @Post('claim')
  async claim(@Headers() headers: Record<string, string | string[] | undefined>) {
    bearer(headers, 'EROS_WORKER_TOKEN');
    const tenantId = workerTenant();
    const job = await withWorkerTenant(async (tx) => {
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
         ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1
       )
       UPDATE engine.eros_inference_jobs AS job SET status = 'RUNNING', lease_token = $2::uuid,
         lease_expires_at = job.deadline_at, attempts = attempts + 1, updated_at = now()
       FROM candidate WHERE job.id = candidate.id
       RETURNING job.id::text, job.request_id::text, job.request_hash, job.tenant_id,
         job.user_id::text, job.creator_id::text, job.session_id::text, job.instance_id::text, job.job_type, job.request,
         job.status, job.lease_token::text, job.deadline_at, job.response_text, job.error_code`,
      tenantId, lease,
    );
    return rows[0];
    });
    if (!job) return { job: null };
    return { job: {
      id: job.id, leaseToken: job.lease_token, target: 'EROS', tenant_id: job.tenant_id,
      creator_id: job.creator_id, job_type: job.job_type,
      request: job.request, purpose: 'response',
    } };
  }

  @Post('cancel-check')
  async cancelCheck(@Headers() headers: Record<string, string | string[] | undefined>, @Body() body: { job_id?: unknown; lease_token?: unknown }) {
    bearer(headers, 'EROS_WORKER_TOKEN');
    if (typeof body?.job_id !== 'string' || typeof body.lease_token !== 'string') throw new HttpException({ code: 'INVALID_WORKER_REQUEST' }, 400);
    const row = await withWorkerTenant(async (tx) => {
      const tenantId = workerTenant();
      await tx.$executeRawUnsafe(
        `UPDATE engine.eros_inference_jobs SET status = 'EXPIRED', lease_token = NULL,
           lease_expires_at = NULL, error_code = 'deadline_expired', updated_at = now()
         WHERE id = $1::uuid AND lease_token = $2::uuid AND tenant_id = $3
           AND status = 'RUNNING' AND deadline_at <= now()`, body.job_id, body.lease_token, tenantId,
      );
      const rows = await tx.$queryRawUnsafe<Array<{ active: boolean; status: string }>>(
        `SELECT (status = 'RUNNING' AND lease_token = $2::uuid AND deadline_at > now()) AS active, status
         FROM engine.eros_inference_jobs WHERE id = $1::uuid AND tenant_id = $3`, body.job_id, body.lease_token, tenantId,
      );
      return rows[0];
    });
    return {
      active: row?.active === true,
      terminal_status: row && ['CANCELLED', 'EXPIRED'].includes(row.status) ? row.status : null,
    };
  }

  @Post('complete')
  async completeJob(@Headers() headers: Record<string, string | string[] | undefined>, @Body() body: { job_id?: unknown; lease_token?: unknown; text?: unknown; model?: unknown }) {
    bearer(headers, 'EROS_WORKER_TOKEN');
    if (typeof body?.job_id !== 'string' || typeof body.lease_token !== 'string' || typeof body.text !== 'string' ||
        !body.text.trim() || body.text.length > REPLY_LIMIT || typeof body.model !== 'string' || body.model.length > 128) {
      throw new HttpException({ code: 'INVALID_WORKER_RESULT' }, 400);
    }
    const count = await withWorkerTenant((tx) => tx.$executeRawUnsafe(
      `UPDATE engine.eros_inference_jobs SET status = 'COMPLETED', response_text = $3, lease_token = NULL,
         lease_expires_at = NULL, updated_at = now()
       WHERE id = $1::uuid AND lease_token = $2::uuid AND tenant_id = $4 AND status = 'RUNNING'
         AND deadline_at > now()`, body.job_id, body.lease_token, body.text, workerTenant(),
    ));
    if (count !== 1) throw new HttpException({ code: 'WORKER_LEASE_REJECTED' }, 409);
    return { completed: true };
  }

  @Post('fail')
  async failJob(@Headers() headers: Record<string, string | string[] | undefined>, @Body() body: { job_id?: unknown; lease_token?: unknown; error_code?: unknown }) {
    bearer(headers, 'EROS_WORKER_TOKEN');
    if (typeof body?.job_id !== 'string' || typeof body.lease_token !== 'string' ||
        typeof body.error_code !== 'string' || !/^[a-z0-9_]{1,64}$/.test(body.error_code)) {
      throw new HttpException({ code: 'INVALID_WORKER_FAILURE' }, 400);
    }
    const count = await withWorkerTenant((tx) => tx.$executeRawUnsafe(
      `UPDATE engine.eros_inference_jobs SET status = 'FAILED', error_code = $3, lease_token = NULL,
         lease_expires_at = NULL, updated_at = now()
       WHERE id = $1::uuid AND lease_token = $2::uuid AND tenant_id = $4 AND status = 'RUNNING'`,
      body.job_id, body.lease_token, body.error_code, workerTenant(),
    ));
    if (count !== 1) throw new HttpException({ code: 'WORKER_LEASE_REJECTED' }, 409);
    return { failed: true };
  }
}
