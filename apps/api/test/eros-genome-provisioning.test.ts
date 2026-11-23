import assert from 'node:assert/strict';
import test from 'node:test';
import { erosGenomeCardFor, erosGenomeIdentityFor } from '../src/eros-adapter.js';
import { enqueueErosGenomeProvisioning, runErosGenomeProvisioningOnce } from '../src/eros-genome-provisioning.js';

function fakeDb(options: { savedGenomeId?: string | null; failSaveOnce?: boolean } = {}) {
  let attempts = 0;
  let failSave = options.failSaveOnce ?? false;
  const calls: string[] = [];
  const db: any = {
    creatorProfile: { async findUnique() { return { erosGenomeId: options.savedGenomeId ?? null }; } },
    erosGenomeProvisioningJob: {
      async upsert(args: any) { calls.push(`enqueue:${args.create.genomeId}`); return args.create; },
      async updateMany(args: any) { calls.push(`retry:${args.data.lastErrorCode}`); return { count: 1 }; },
      async deleteMany() { calls.push('delete-job'); return { count: 1 }; },
    },
    creator: { async findUnique() { return { displayName: 'Synthetic Creator', profile: { erosGenomeId: null }, telegramChannel: { status: 'CONNECTED' } }; } },
    async $queryRawUnsafe(_sql: string, leaseToken: string) {
      if (attempts >= 2) return [];
      attempts += 1;
      return [{ creatorId: 'creator_cuid_1', genomeId: erosGenomeIdentityFor('creator_cuid_1'), syncRequested: false, attempts: attempts - 1, generation: 1, leaseToken }];
    },
    async $transaction(callback: (tx: any) => Promise<void>) {
      return callback({
        creatorProfile: { async updateMany(args: any) {
          calls.push(`save:${args.data.erosGenomeId}`);
          if (failSave) { failSave = false; throw new Error('simulated_database_write_failure'); }
          return { count: 1 };
        } },
        erosGenomeProvisioningJob: { async deleteMany() { calls.push('delete-job'); return { count: 1 }; } },
      });
    },
  };
  return { db, calls };
}

test('verified Creator provisioning enqueues a stable genome UUID and persists only after exact Eros confirmation', async () => {
  const { db, calls } = fakeDb();
  let apiCalls = 0;
  const client = { async createGenome(id: string, card: unknown) { apiCalls += 1; assert.equal(id, erosGenomeIdentityFor('creator_cuid_1')); assert.deepEqual(card, erosGenomeCardFor('Synthetic Creator')); return id; } };
  await enqueueErosGenomeProvisioning(db, 'creator_cuid_1');
  assert.equal(await runErosGenomeProvisioningOnce(db, client as any), 'READY');
  assert.equal(apiCalls, 1);
  assert.deepEqual(calls, [`enqueue:${erosGenomeIdentityFor('creator_cuid_1')}`, `save:${erosGenomeIdentityFor('creator_cuid_1')}`, 'delete-job']);
});

test('lost persistence after Eros creation retries the same UUID and recovers idempotently', async () => {
  const { db, calls } = fakeDb({ failSaveOnce: true });
  const created = new Set<string>();
  const client = { async createGenome(id: string) { created.add(id); return id; } };
  assert.equal(await runErosGenomeProvisioningOnce(db, client as any), 'RETRY');
  assert.equal(await runErosGenomeProvisioningOnce(db, client as any), 'READY');
  assert.deepEqual([...created], [erosGenomeIdentityFor('creator_cuid_1')]);
  assert.equal(calls.filter((call) => call.startsWith('save:')).length, 2);
  assert.ok(calls.includes('retry:provisioning_failed'));
});

test('profile changes request a durable genome sync only for a connected Creator with a managed genome', async () => {
  const { db, calls } = fakeDb();
  let syncRequested = false;
    db.erosGenomeProvisioningJob.upsert = async (args: any) => { syncRequested = args.update.syncRequested === true; calls.push(`enqueue:${args.create.genomeId}`); };
  await enqueueErosGenomeProvisioning(db, 'creator_cuid_1', true);
  assert.equal(syncRequested, true);
  assert.deepEqual(calls, [`enqueue:${erosGenomeIdentityFor('creator_cuid_1')}`]);
});

test('recovery scans do not reset an existing job retry schedule', async () => {
  const { db } = fakeDb();
  let update: unknown;
  db.erosGenomeProvisioningJob.upsert = async (args: any) => { update = args.update; };
  await enqueueErosGenomeProvisioning(db, 'creator_cuid_1');
  assert.deepEqual(update, {});
});

test('a newer request arriving during Eros API work remains queued for another pass', async () => {
  const { db, calls } = fakeDb();
  let generation = 1;
  db.erosGenomeProvisioningJob.upsert = async () => { generation += 1; };
  db.$queryRawUnsafe = async (_sql: string, leaseToken: string) => [{
    creatorId: 'creator_cuid_1', genomeId: erosGenomeIdentityFor('creator_cuid_1'),
    syncRequested: false, attempts: 0, generation: 1, leaseToken,
  }];
  db.$transaction = async (callback: (tx: any) => Promise<boolean>) => callback({
    creatorProfile: { async updateMany() { calls.push('save-mapping'); return { count: 1 }; } },
    erosGenomeProvisioningJob: {
      async deleteMany(args: any) { return { count: args.where.generation === generation ? 1 : 0 }; },
      async updateMany(args: any) { calls.push(`release:${args.where.leaseToken}`); return { count: 1 }; },
    },
  });
  const client = { async createGenome() { await enqueueErosGenomeProvisioning(db, 'creator_cuid_1', true); return erosGenomeIdentityFor('creator_cuid_1'); } };
  assert.equal(await runErosGenomeProvisioningOnce(db, client as any), 'RETRY');
  assert.ok(calls.some((call) => call.startsWith('release:')));
  assert.ok(calls.includes('save-mapping'));
});

test('Eros calls happen before the short persistence transaction begins', async () => {
  const { db } = fakeDb();
  const order: string[] = [];
  db.$transaction = async (callback: (tx: any) => Promise<void>) => {
    order.push('db_transaction_start');
    return callback({
      creatorProfile: { async updateMany() { order.push('mapping_saved'); return { count: 1 }; } },
      erosGenomeProvisioningJob: { async deleteMany() { order.push('job_deleted'); return { count: 1 }; } },
    });
  };
  const client = { async createGenome() { order.push('eros_api_call'); return erosGenomeIdentityFor('creator_cuid_1'); } };
  await enqueueErosGenomeProvisioning(db, 'creator_cuid_1');
  assert.equal(await runErosGenomeProvisioningOnce(db, client as any), 'READY');
  assert.deepEqual(order, ['eros_api_call', 'db_transaction_start', 'mapping_saved', 'job_deleted']);
});
