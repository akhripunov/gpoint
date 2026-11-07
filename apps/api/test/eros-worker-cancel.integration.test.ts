import assert from 'node:assert/strict';
import { createHmac, hkdfSync, randomUUID } from 'node:crypto';
import test from 'node:test';
import { PrismaClient } from '@prisma/client';
import { erosIdentityFor } from '../src/eros-adapter.js';
import { deriveBridgeInferenceIdentity, parseBridgeRequest, signBridgeIdentity, type ParsedErosBridgeRequest } from '../src/eros-worker-contract.js';

const databaseUrl = process.env.DATABASE_URL;
const parsedDatabaseUrl = databaseUrl ? new URL(databaseUrl) : undefined;
const disposableLoopbackDatabase = Boolean(
  process.env.EROS_WORKER_DISPOSABLE_TEST_DB === '1' &&
    parsedDatabaseUrl &&
    ['localhost', '127.0.0.1', '::1'].includes(parsedDatabaseUrl.hostname) &&
    /(test|synth|chain|disposable)/i.test(parsedDatabaseUrl.pathname),
);

test('cancel-check terminalizes a running lease whose deadline has elapsed', { skip: !disposableLoopbackDatabase }, async () => {
  const prior = { token: process.env.GPOINT_AGATA_WORKER_TOKEN, enabled: process.env.EROS_ENABLED, tenant: process.env.EROS_WORKER_TENANT_ID, agataTenant: process.env.AGATA_TENANT_ID };
  process.env.GPOINT_AGATA_WORKER_TOKEN = `local-test-${randomUUID()}`;
  process.env.EROS_ENABLED = 'true';
  const tenantId = '7508f36f-774a-48aa-8c6b-42f6f160415d';
  process.env.EROS_WORKER_TENANT_ID = tenantId;
  process.env.AGATA_TENANT_ID = tenantId;

  const [{ AgataWorkerGatewayController }, prisma] = await Promise.all([
    import('../src/agata-worker-gateway.js'),
    Promise.resolve(new PrismaClient()),
  ]);
  const jobId = randomUUID();
  const leaseToken = randomUUID();
  const requestId = randomUUID();
  const userId = randomUUID();
  const creatorId = randomUUID();
  const sessionId = randomUUID();
  const instanceId = randomUUID();
  const controller = new AgataWorkerGatewayController();

  try {
    const [databaseRole] = await prisma.$queryRawUnsafe<Array<{ rolsuper: boolean; rolbypassrls: boolean }>>(
      `SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user`,
    );
    assert.deepEqual(databaseRole, { rolsuper: false, rolbypassrls: false });

    const [withoutTenantContext] = await prisma.$queryRawUnsafe<Array<{ visible: bigint }>>(
      `SELECT count(*)::bigint AS visible FROM engine.eros_inference_jobs`,
    );
    assert.equal(withoutTenantContext.visible, 0n);

    await assert.rejects(
      prisma.$executeRawUnsafe(
        `INSERT INTO engine.eros_inference_jobs
           (id, request_id, request_hash, tenant_id, user_id, creator_id, session_id, instance_id,
            job_type, request, status, deadline_at)
         VALUES ($1::uuid, $2::uuid, repeat('d', 64), $3, $4::uuid, $5::uuid, $6::uuid, $7::uuid,
            'CHAT_COMPLETION', '{}'::jsonb, 'QUEUED', now() + interval '1 minute')`,
        randomUUID(), randomUUID(), tenantId, randomUUID(), randomUUID(), randomUUID(), randomUUID(),
      ),
      (error: unknown) => {
        const prismaError = error as { code?: string; meta?: { code?: string } };
        assert.equal(prismaError.meta?.code ?? prismaError.code, '42501');
        return true;
      },
      'RLS WITH CHECK must reject inserts without transaction-local tenant context',
    );

    await prisma.$transaction(async (tx) => {
      await tx.$queryRawUnsafe('SELECT set_config(\'app.tenant_id\', $1, true)', tenantId);
      await tx.$executeRawUnsafe(
        `INSERT INTO engine.eros_inference_jobs
           (id, request_id, request_hash, tenant_id, user_id, creator_id, session_id, instance_id,
            job_type, request, status, lease_token, lease_expires_at, deadline_at)
         VALUES ($1::uuid, $2::uuid, repeat('c', 64), $3, $4::uuid, $5::uuid, $6::uuid, $7::uuid,
            'CHAT_COMPLETION', '{}'::jsonb, 'RUNNING', $8::uuid, now() + interval '1 minute', now() - interval '1 second')`,
        jobId, requestId, tenantId, userId, creatorId, sessionId, instanceId, leaseToken,
      );
    });

    const result = await controller.cancelCheck(
      { authorization: `Bearer ${process.env.GPOINT_AGATA_WORKER_TOKEN}` },
      { job_id: jobId, lease_token: leaseToken },
    );
    assert.deepEqual(result, { active: false, terminal_status: 'EXPIRED' });

    const state = await prisma.$transaction(async (tx) => {
      await tx.$queryRawUnsafe('SELECT set_config(\'app.tenant_id\', $1, true)', tenantId);
      return tx.$queryRawUnsafe<Array<{ status: string; error_code: string }>>(
        `SELECT status, error_code FROM engine.eros_inference_jobs WHERE id = $1::uuid`, jobId,
      );
    });
    assert.deepEqual(state, [{ status: 'EXPIRED', error_code: 'deadline_expired' }]);

    const crossTenantUpdate = await prisma.$transaction(async (tx) => {
      await tx.$queryRawUnsafe('SELECT set_config(\'app.tenant_id\', $1, true)', randomUUID());
      return tx.$executeRawUnsafe(
        `UPDATE engine.eros_inference_jobs SET status = 'CANCELLED' WHERE id = $1::uuid`, jobId,
      );
    });
    assert.equal(crossTenantUpdate, 0);
  } finally {
    await prisma.$transaction(async (tx) => {
      await tx.$queryRawUnsafe('SELECT set_config(\'app.tenant_id\', $1, true)', tenantId);
      await tx.$executeRawUnsafe('DELETE FROM engine.eros_inference_jobs WHERE id = $1::uuid', jobId);
    });
    await prisma.$disconnect();
    if (prior.token === undefined) delete process.env.GPOINT_AGATA_WORKER_TOKEN; else process.env.GPOINT_AGATA_WORKER_TOKEN = prior.token;
    if (prior.enabled === undefined) delete process.env.EROS_ENABLED; else process.env.EROS_ENABLED = prior.enabled;
    if (prior.tenant === undefined) delete process.env.EROS_WORKER_TENANT_ID; else process.env.EROS_WORKER_TENANT_ID = prior.tenant;
    if (prior.agataTenant === undefined) delete process.env.AGATA_TENANT_ID; else process.env.AGATA_TENANT_ID = prior.agataTenant;
  }
});


test('GPOINT claim refreshes queued Eros identity after token expiry and completes one lease', { skip: !disposableLoopbackDatabase }, async () => {
  const tenantId = '7508f36f-774a-48aa-8c6b-42f6f160415d';
  const prior = {
    token: process.env.GPOINT_AGATA_WORKER_TOKEN,
    enabled: process.env.EROS_ENABLED,
    dataMode: process.env.EROS_DATA_MODE,
    testUsers: process.env.EROS_TEST_USER_IDS,
    testCreators: process.env.EROS_TEST_CREATOR_IDS,
    tenant: process.env.EROS_WORKER_TENANT_ID,
    agataTenant: process.env.AGATA_TENANT_ID,
    signing: process.env.EROS_WORKER_SIGNING_SECRET,
  };
  process.env.GPOINT_AGATA_WORKER_TOKEN = `local-test-${randomUUID()}`;
  process.env.EROS_ENABLED = 'true';
  process.env.EROS_DATA_MODE = 'synthetic';
  process.env.EROS_TEST_USER_IDS = '10000000-0000-4000-8000-000000000001';
  process.env.EROS_TEST_CREATOR_IDS = '20000000-0000-4000-8000-000000000002';
  process.env.EROS_WORKER_TENANT_ID = tenantId;
  process.env.AGATA_TENANT_ID = tenantId;
  process.env.EROS_WORKER_SIGNING_SECRET = 'synthetic-only-signing-secret-32-bytes-minimum';
  const [{ AgataWorkerGatewayController }, { claimErosForGpointWorker, ErosWorkerGatewayController }, prisma] = await Promise.all([
    import('../src/agata-worker-gateway.js'),
    import('../src/eros-worker-gateway.js'),
    Promise.resolve(new PrismaClient()),
  ]);
  const requestId = randomUUID();
  const mappedIdentity = erosIdentityFor({
    userId: process.env.EROS_TEST_USER_IDS,
    creatorId: process.env.EROS_TEST_CREATOR_IDS,
  });
  const userId = mappedIdentity.userId;
  const creatorId = mappedIdentity.creatorId;
  const sessionId = randomUUID();
  const instanceId = randomUUID();
  const requestBody = {
    model: 'gpoint-codex',
    messages: [{ role: 'user', content: 'synthetic queued prompt' }],
    max_tokens: 32,
    temperature: 1,
  };
  const signingSecret = process.env.EROS_WORKER_SIGNING_SECRET;
  assert.ok(signingSecret);
  const key = Buffer.from(hkdfSync(
    'sha256', Buffer.from(signingSecret),
    Buffer.from('gpoint-eros-queue-v1'), Buffer.from('request-payload-aead'), 32,
  ));
  const bridgeIdentity = {
    tenantId, userId, creatorId, sessionId, instanceId, requestId,
  };
  const signedRequest = {
    ...requestBody,
    user: signBridgeIdentity(bridgeIdentity, new TextEncoder().encode(signingSecret)),
  };
  const parsedRequest = await parseBridgeRequest(signedRequest, new TextEncoder().encode(signingSecret));
  const requestFingerprint = createHmac('sha256', key).update(JSON.stringify({
    model: parsedRequest.model, messages: parsedRequest.messages,
    max_tokens: parsedRequest.maxTokens, temperature: parsedRequest.temperature,
  })).digest('hex');
  const chatIdentity = deriveBridgeInferenceIdentity(bridgeIdentity, requestFingerprint);
  let createdJobId: string | undefined;
  try {
    const erosGateway = new ErosWorkerGatewayController();
    const internalGateway = erosGateway as unknown as {
      createOrReadJob(request: ParsedErosBridgeRequest): Promise<{
        id: string; request_id: string; request_hash: string; tenant_id: string; user_id: string;
        creator_id: string | null; session_id: string; instance_id: string; job_type: string;
        request: Record<string, unknown>; status: string; lease_token: string | null;
        deadline_at: Date; response_text: string | null; error_code: string | null;
      }>;
    };
    const createdJob = await internalGateway.createOrReadJob(parsedRequest);
    createdJobId = createdJob.id;
    assert.equal(createdJob.id.length, 36);
    assert.equal(createdJob.request_id, chatIdentity.requestId);
    class TestGateway extends AgataWorkerGatewayController {
      protected override async claimAgataJob() { return { job: null }; }
      protected override claimErosJob() { return claimErosForGpointWorker(); }
    }
    const controller = new TestGateway();
    const realDateNow = Date.now;
    const claimNowMs = realDateNow() + 121_000;
    let concurrentClaims: unknown[];
    Date.now = () => claimNowMs;
    try {
      concurrentClaims = await Promise.all(Array.from({ length: 2 }, () =>
        controller.claim({ authorization: `Bearer ${process.env.GPOINT_AGATA_WORKER_TOKEN}` }),
      ));
    } finally {
      Date.now = realDateNow;
    }
    const claimedJobs = concurrentClaims
      .map((claim) => (claim as { job: { id: string; leaseToken: string; attempt: number; target: string; tenant_id: string; request: unknown; purpose: string } | null }).job)
      .filter((job): job is NonNullable<typeof job> => job !== null);
    assert.equal(claimedJobs.length, 1);
    const job = claimedJobs[0]!;
    assert.equal(job.id, createdJob.id);
    assert.match(job.leaseToken, /^[0-9a-f-]{36}$/);
    assert.equal(job.attempt, 1);
    assert.equal(job.target, 'GPOINT');
    assert.equal(job.tenant_id, tenantId);
    assert.deepEqual(await parseBridgeRequest(
      job.request, new TextEncoder().encode(signingSecret), Math.floor(claimNowMs / 1000),
    ), {
      ...parsedRequest,
      stream: false,
    });
    assert.equal(job.purpose, 'eros_chat_completion');
    assert.deepEqual(await controller.claim({ authorization: `Bearer ${process.env.GPOINT_AGATA_WORKER_TOKEN}` }), { job: null });
    assert.deepEqual(await controller.complete(
      { authorization: `Bearer ${process.env.GPOINT_AGATA_WORKER_TOKEN}` },
      { job_id: job.id, lease_token: job.leaseToken, text: 'synthetic reply', model: 'codex-default', purpose: 'eros_chat_completion' },
    ), { completed: true });
    const state = await prisma.$transaction(async (tx) => {
      await tx.$queryRawUnsafe("SELECT set_config('app.tenant_id', $1, true)", tenantId);
      return tx.$queryRawUnsafe<Array<{ status: string; attempts: number; lease_token: string | null; response_text: string | null }>>(
        `SELECT status, attempts, lease_token::text, response_text FROM engine.eros_inference_jobs WHERE id = $1::uuid`, createdJob.id,
      );
    });
    assert.equal(state[0]?.status, 'COMPLETED');
    assert.equal(state[0]?.attempts, 1);
    assert.equal(state[0]?.lease_token, null);
    assert.notEqual(state[0]?.response_text, 'synthetic reply');
    assert.equal(JSON.parse(state[0]!.response_text!).alg, 'A256GCM');
    class TestErosReader extends ErosWorkerGatewayController {
      async readCompletedJob(jobId: string) { return this.readJob(jobId); }
    }
    assert.equal((await new TestErosReader().readCompletedJob(createdJob.id))?.response_text, 'synthetic reply');
  } finally {
    if (createdJobId) await prisma.$transaction(async (tx) => {
      await tx.$queryRawUnsafe("SELECT set_config('app.tenant_id', $1, true)", tenantId);
      await tx.$executeRawUnsafe('DELETE FROM engine.eros_inference_jobs WHERE id = $1::uuid', createdJobId);
    });
    await prisma.$disconnect();
    if (prior.token === undefined) delete process.env.GPOINT_AGATA_WORKER_TOKEN; else process.env.GPOINT_AGATA_WORKER_TOKEN = prior.token;
    if (prior.enabled === undefined) delete process.env.EROS_ENABLED; else process.env.EROS_ENABLED = prior.enabled;
    if (prior.dataMode === undefined) delete process.env.EROS_DATA_MODE; else process.env.EROS_DATA_MODE = prior.dataMode;
    if (prior.testUsers === undefined) delete process.env.EROS_TEST_USER_IDS; else process.env.EROS_TEST_USER_IDS = prior.testUsers;
    if (prior.testCreators === undefined) delete process.env.EROS_TEST_CREATOR_IDS; else process.env.EROS_TEST_CREATOR_IDS = prior.testCreators;
    if (prior.tenant === undefined) delete process.env.EROS_WORKER_TENANT_ID; else process.env.EROS_WORKER_TENANT_ID = prior.tenant;
    if (prior.agataTenant === undefined) delete process.env.AGATA_TENANT_ID; else process.env.AGATA_TENANT_ID = prior.agataTenant;
    if (prior.signing === undefined) delete process.env.EROS_WORKER_SIGNING_SECRET; else process.env.EROS_WORKER_SIGNING_SECRET = prior.signing;
  }
});
