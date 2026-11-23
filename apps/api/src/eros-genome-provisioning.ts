import { randomUUID } from 'node:crypto';
import { createErosEngineClient, erosGenomeCardFor, erosGenomeIdentityFor, ErosAdapterError } from './eros-adapter.js';

let prismaClient: ProvisioningDb | undefined;
const LEASE_MS = 120_000;
const TICK_MS = 30_000;
const MAX_BACKFILL_PER_TICK = 100;

type ProvisioningJob = {
  creatorId: string;
  genomeId: string;
  syncRequested: boolean;
  attempts: number;
  generation: number;
  leaseToken: string;
};

type ProvisioningDb = any;

export async function enqueueErosGenomeProvisioning(db: ProvisioningDb, creatorId: string, syncRequested = false): Promise<void> {
  const profile = await db.creatorProfile.findUnique({ where: { creatorId }, select: { erosGenomeId: true } });
  const genomeId = erosGenomeIdentityFor(creatorId);
  if (profile?.erosGenomeId && (!syncRequested || profile.erosGenomeId !== genomeId)) return;
  await db.erosGenomeProvisioningJob.upsert({
    where: { creatorId },
    create: { creatorId, genomeId, syncRequested },
    update: syncRequested
      ? { generation: { increment: 1 }, nextAttemptAt: new Date(), syncRequested: true }
      : {},
  });
}

async function claimNextJob(db: ProvisioningDb, onlyCreatorId?: string): Promise<ProvisioningJob | null> {
  const token = randomUUID();
  const now = new Date();
  const expires = new Date(now.getTime() + LEASE_MS);
  const selector = onlyCreatorId === undefined ? '' : 'AND job."creatorId" = $3';
  const values = onlyCreatorId === undefined ? [token, expires] : [token, expires, onlyCreatorId];
  const creatorParam = onlyCreatorId === undefined ? '' : '$3';
  const rows = await db.$queryRawUnsafe(
    `WITH candidate AS (
       SELECT job."creatorId" FROM "ErosGenomeProvisioningJob" AS job
       WHERE job."nextAttemptAt" <= now()
         AND (job."leaseExpiresAt" IS NULL OR job."leaseExpiresAt" <= now())
         ${selector}
       ORDER BY job."nextAttemptAt", job."createdAt"
       LIMIT 1 FOR UPDATE SKIP LOCKED
     )
     UPDATE "ErosGenomeProvisioningJob" AS job
       SET "leaseToken" = $1::uuid, "leaseExpiresAt" = $2, "updatedAt" = now()
       FROM candidate WHERE job."creatorId" = candidate."creatorId"
       ${onlyCreatorId === undefined ? '' : `AND job."creatorId" = ${creatorParam}`}
       RETURNING job."creatorId", job."genomeId"::text, job."syncRequested", job."attempts", job."generation", job."leaseToken"::text`,
    ...values,
  ) as Array<ProvisioningJob>;
  return rows[0] ?? null;
}

function safeErrorCode(error: unknown): string {
  if (error instanceof ErosAdapterError && /^[a-z0-9_-]{1,64}$/.test(error.code)) return error.code;
  return 'provisioning_failed';
}

async function rescheduleJob(db: ProvisioningDb, job: ProvisioningJob, error: unknown): Promise<void> {
  const delayMs = Math.min(60 * 60_000, 30_000 * (2 ** Math.min(job.attempts, 7)));
  await db.erosGenomeProvisioningJob.updateMany({
    where: { creatorId: job.creatorId, leaseToken: job.leaseToken },
    data: { attempts: { increment: 1 }, nextAttemptAt: new Date(Date.now() + delayMs), leaseToken: null, leaseExpiresAt: null, lastErrorCode: safeErrorCode(error) },
  });
}

export async function runErosGenomeProvisioningOnce(
  db: ProvisioningDb,
  client = createErosEngineClient(),
  onlyCreatorId?: string,
): Promise<'IDLE' | 'READY' | 'RETRY'> {
  const job = await claimNextJob(db, onlyCreatorId);
  if (!job) return 'IDLE';
  try {
    const creator = await db.creator.findUnique({
      where: { id: job.creatorId },
      include: { profile: true, telegramChannel: { select: { status: true } } },
    });
    if (!creator || creator.telegramChannel?.status !== 'CONNECTED' || !creator.profile) {
      await db.erosGenomeProvisioningJob.deleteMany({ where: { creatorId: job.creatorId, leaseToken: job.leaseToken } });
      return 'IDLE';
    }
    const storedGenomeId = creator.profile.erosGenomeId as string | null;
    if (storedGenomeId && storedGenomeId !== job.genomeId) {
      // Existing mapped personas belong to older/approved identity flows; never replace them.
      await db.erosGenomeProvisioningJob.deleteMany({ where: { creatorId: job.creatorId, leaseToken: job.leaseToken } });
      return 'READY';
    }
    if (!client) throw new ErosAdapterError('eros_not_configured');
    const card = erosGenomeCardFor(creator.displayName);
    if (storedGenomeId && job.syncRequested) await client.updateGenome(job.genomeId, card);
    else if (!storedGenomeId) await client.createGenome(job.genomeId, card);
    const finalized = await db.$transaction(async (tx: ProvisioningDb) => {
      const saved = await tx.creatorProfile.updateMany({
        where: { creatorId: job.creatorId, OR: [{ erosGenomeId: null }, { erosGenomeId: job.genomeId }] },
        data: { erosGenomeId: job.genomeId },
      });
      if (saved.count !== 1) throw new ErosAdapterError('eros_genome_mapping_conflict', 409);
      const deleted = await tx.erosGenomeProvisioningJob.deleteMany({ where: { creatorId: job.creatorId, leaseToken: job.leaseToken, generation: job.generation } });
      if (deleted.count === 1) return true;
      await tx.erosGenomeProvisioningJob.updateMany({
        where: { creatorId: job.creatorId, leaseToken: job.leaseToken },
        data: { nextAttemptAt: new Date(), leaseToken: null, leaseExpiresAt: null },
      });
      return false;
    });
    return finalized ? 'READY' : 'RETRY';
  } catch (error) {
    await rescheduleJob(db, job, error);
    return 'RETRY';
  }
}

async function backfillConnectedCreators(db: ProvisioningDb): Promise<void> {
  const profiles = await db.creatorProfile.findMany({
    where: { erosGenomeId: null, creator: { telegramChannel: { is: { status: 'CONNECTED' } } } },
    select: { creatorId: true }, take: MAX_BACKFILL_PER_TICK, orderBy: { creatorId: 'asc' },
  });
  for (const { creatorId } of profiles) await enqueueErosGenomeProvisioning(db, creatorId);
}

async function productionDb(): Promise<ProvisioningDb> {
  if (!prismaClient) {
    const { PrismaClient } = await import('@prisma/client');
    prismaClient = new PrismaClient();
  }
  return prismaClient;
}

export async function runErosGenomeProvisioningTick(database?: ProvisioningDb): Promise<void> {
  const db = database ?? await productionDb();
  await backfillConnectedCreators(db);
  await runErosGenomeProvisioningOnce(db);
}

export async function erosGenomeProvisioningState(db: ProvisioningDb, creatorId: string): Promise<'READY' | 'PENDING'> {
  const profile = await db.creatorProfile.findUnique({ where: { creatorId }, select: { erosGenomeId: true } });
  if (profile?.erosGenomeId) return 'READY';
  await enqueueErosGenomeProvisioning(db, creatorId);
  return 'PENDING';
}

export function startErosGenomeProvisioning(): () => void {
  const timer = setInterval(() => { void runErosGenomeProvisioningTick().catch(() => undefined); }, TICK_MS);
  timer.unref();
  void runErosGenomeProvisioningTick().catch(() => undefined);
  return () => clearInterval(timer);
}
