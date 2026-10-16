import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
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
  process.env.EROS_WORKER_TOKEN = `local-test-${randomUUID()}`;
  const tenantId = '7508f36f-774a-48aa-8c6b-42f6f160415d';
  process.env.EROS_WORKER_TENANT_ID = tenantId;

  const [{ ErosWorkerGatewayController }, prisma] = await Promise.all([
    import('../src/eros-worker-gateway.js'),
    Promise.resolve(new PrismaClient()),
  ]);
  const jobId = randomUUID();
  const leaseToken = randomUUID();
  const requestId = randomUUID();
  const userId = randomUUID();
  const creatorId = randomUUID();
  const sessionId = randomUUID();
  const instanceId = randomUUID();
  const controller = new ErosWorkerGatewayController();

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
      { authorization: `Bearer ${process.env.EROS_WORKER_TOKEN}` },
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
  }
});
