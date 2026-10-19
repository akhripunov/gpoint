import assert from 'node:assert/strict';
import { createCipheriv, hkdfSync, randomBytes, randomUUID } from 'node:crypto';
import test from 'node:test';
import { PrismaClient } from '@prisma/client';

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


test('GPOINT claim round-trips encrypted Eros job and completes it with a single lease', { skip: !disposableLoopbackDatabase }, async () => {
  const tenantId = '7508f36f-774a-48aa-8c6b-42f6f160415d';
  const prior = {
    token: process.env.GPOINT_AGATA_WORKER_TOKEN,
    enabled: process.env.EROS_ENABLED,
    tenant: process.env.EROS_WORKER_TENANT_ID,
    agataTenant: process.env.AGATA_TENANT_ID,
    signing: process.env.EROS_WORKER_SIGNING_SECRET,
  };
  process.env.GPOINT_AGATA_WORKER_TOKEN = `local-test-${randomUUID()}`;
  process.env.EROS_ENABLED = 'true';
  process.env.EROS_WORKER_TENANT_ID = tenantId;
  process.env.AGATA_TENANT_ID = tenantId;
  process.env.EROS_WORKER_SIGNING_SECRET = 'synthetic-only-signing-secret-32-bytes-minimum';
  const [{ AgataWorkerGatewayController }, { claimErosForGpointWorker, ErosWorkerGatewayController }, prisma] = await Promise.all([
    import('../src/agata-worker-gateway.js'),
    import('../src/eros-worker-gateway.js'),
    Promise.resolve(new PrismaClient()),
  ]);
  const id = randomUUID();
  const requestId = randomUUID();
  const userId = randomUUID();
  const creatorId = randomUUID();
  const sessionId = randomUUID();
  const instanceId = randomUUID();
  const request = {
    model: 'gpoint-codex',
    messages: [{ role: 'user', content: 'synthetic queued prompt' }],
    max_tokens: 32,
    temperature: 1,
  };
  const key = Buffer.from(hkdfSync(
    'sha256', Buffer.from(process.env.EROS_WORKER_SIGNING_SECRET),
    Buffer.from('gpoint-eros-queue-v1'), Buffer.from('request-payload-aead'), 32,
  ));
  const nonce = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, nonce);
  cipher.setAAD(Buffer.from([
    tenantId, id, requestId, userId, creatorId, sessionId, instanceId, 'request',
  ].join('\0')));
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(request), 'utf8'), cipher.final()]);
  const envelope = {
    v: 1, alg: 'A256GCM', nonce: nonce.toString('base64url'),
    ciphertext: ciphertext.toString('base64url'), tag: cipher.getAuthTag().toString('base64url'),
  };
  try {
    await prisma.$transaction(async (tx) => {
      await tx.$queryRawUnsafe("SELECT set_config('app.tenant_id', $1, true)", tenantId);
      await tx.$executeRawUnsafe(
        `INSERT INTO engine.eros_inference_jobs
           (id, request_id, request_hash, tenant_id, user_id, creator_id, session_id, instance_id,
            job_type, request, status, deadline_at)
         VALUES ($1::uuid, $2::uuid, repeat('e', 64), $3, $4::uuid, $5::uuid, $6::uuid, $7::uuid,
            'CHAT_COMPLETION', $8::jsonb, 'PENDING', now() + interval '2 minutes')`,
        id, requestId, tenantId, userId, creatorId, sessionId, instanceId, JSON.stringify(envelope),
      );
    });
    class TestGateway extends AgataWorkerGatewayController {
      protected override async claimAgataJob() { return { job: null }; }
      protected override claimErosJob() { return claimErosForGpointWorker(); }
    }
    const controller = new TestGateway();
    const concurrentClaims = await Promise.all(Array.from({ length: 2 }, () =>
      controller.claim({ authorization: `Bearer ${process.env.GPOINT_AGATA_WORKER_TOKEN}` }),
    ));
    const claimedJobs = concurrentClaims
      .map((claim) => (claim as { job: { id: string; leaseToken: string; attempt: number; target: string; tenant_id: string; request: unknown; purpose: string } | null }).job)
      .filter((job): job is NonNullable<typeof job> => job !== null);
    assert.equal(claimedJobs.length, 1);
    const job = claimedJobs[0]!;
    assert.equal(job.id, id);
    assert.match(job.leaseToken, /^[0-9a-f-]{36}$/);
    assert.equal(job.attempt, 1);
    assert.equal(job.target, 'GPOINT');
    assert.equal(job.tenant_id, tenantId);
    assert.deepEqual(job.request, request);
    assert.equal(job.purpose, 'eros_chat_completion');
    assert.deepEqual(await controller.claim({ authorization: `Bearer ${process.env.GPOINT_AGATA_WORKER_TOKEN}` }), { job: null });
    assert.deepEqual(await controller.complete(
      { authorization: `Bearer ${process.env.GPOINT_AGATA_WORKER_TOKEN}` },
      { job_id: job.id, lease_token: job.leaseToken, text: 'synthetic reply', model: 'codex-default', purpose: 'eros_chat_completion' },
    ), { completed: true });
    const state = await prisma.$transaction(async (tx) => {
      await tx.$queryRawUnsafe("SELECT set_config('app.tenant_id', $1, true)", tenantId);
      return tx.$queryRawUnsafe<Array<{ status: string; attempts: number; lease_token: string | null; response_text: string | null }>>(
        `SELECT status, attempts, lease_token::text, response_text FROM engine.eros_inference_jobs WHERE id = $1::uuid`, id,
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
    assert.equal((await new TestErosReader().readCompletedJob(id))?.response_text, 'synthetic reply');
  } finally {
    await prisma.$transaction(async (tx) => {
      await tx.$queryRawUnsafe("SELECT set_config('app.tenant_id', $1, true)", tenantId);
      await tx.$executeRawUnsafe('DELETE FROM engine.eros_inference_jobs WHERE id = $1::uuid', id);
    });
    await prisma.$disconnect();
    if (prior.token === undefined) delete process.env.GPOINT_AGATA_WORKER_TOKEN; else process.env.GPOINT_AGATA_WORKER_TOKEN = prior.token;
    if (prior.enabled === undefined) delete process.env.EROS_ENABLED; else process.env.EROS_ENABLED = prior.enabled;
    if (prior.tenant === undefined) delete process.env.EROS_WORKER_TENANT_ID; else process.env.EROS_WORKER_TENANT_ID = prior.tenant;
    if (prior.agataTenant === undefined) delete process.env.AGATA_TENANT_ID; else process.env.AGATA_TENANT_ID = prior.agataTenant;
    if (prior.signing === undefined) delete process.env.EROS_WORKER_SIGNING_SECRET; else process.env.EROS_WORKER_SIGNING_SECRET = prior.signing;
  }
});
