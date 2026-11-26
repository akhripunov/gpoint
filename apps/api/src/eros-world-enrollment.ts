import { PrismaClient } from '@prisma/client';

export type ErosWorldEnrollment = (ownerId: string, sessionId: string, instanceId: string) => Promise<void>;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export const DEFAULT_WORLDVIEW = "A coherent contemporary world grounded in each fictional persona's established genome, backstory and conversational evidence. Preserve the persona's canon, language, circumstances and continuity; do not force a city, occupation, era or relationship that contradicts established facts. Invent only fictional persona events, never the real user's actions, words or private facts.";
let pool: { url: string; db: PrismaClient } | undefined;

/** Only a dedicated, narrowly granted world writer on the private Eros DB. */
export function worldEnrollmentConnection(raw: string): string {
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error('eros_world_database_invalid'); }
  if (url.protocol !== 'postgresql:' || url.hostname !== 'gpoint-eros-db' ||
      url.port !== '5432' || url.pathname !== '/eros' ||
      url.username !== 'eros_world_writer' || !url.password || url.hash || url.search) {
    throw new Error('eros_world_database_invalid');
  }
  url.searchParams.set('connection_limit', '2');
  url.searchParams.set('connect_timeout', '3');
  url.searchParams.set('pool_timeout', '2');
  return url.toString();
}

/**
 * Product-owned enrollment, using only stock downstream-managed tables.
 * Called before the first valid outgoing text message, including existing
 * relationships. No changes to persona identity, source or stock SQLx ledger.
 */
export function createErosWorldEnrollment(env: NodeJS.ProcessEnv): ErosWorldEnrollment | undefined {
  const raw = env.EROS_WORLD_ENROLLMENT_DATABASE_URL;
  if (!raw) {
    if (env.EROS_WORLD_ENROLLMENT_REQUIRED === 'true') throw new Error('eros_world_database_missing');
    // Rollout compatibility: the protected deploy must provision credentials
    // before setting REQUIRED. No fake claim of world activation in this mode.
    return undefined;
  }
  const url = worldEnrollmentConnection(raw);
  if (pool && pool.url !== url) throw new Error('eros_world_database_changed');
  if (!pool) pool = { url, db: new PrismaClient({ datasourceUrl: url, log: [] }) };
  const db = pool.db;
  return (ownerId, sessionId, instanceId) => enrollErosWorld(db, ownerId, sessionId, instanceId);
}

/** Exported for isolated PostgreSQL contract tests; never takes caller DSNs. */
export async function enrollErosWorld(db: PrismaClient, ownerId: string, sessionId: string, instanceId: string): Promise<void> {
    if (![ownerId, sessionId, instanceId].every(id => UUID.test(id))) {
      throw new Error('eros_world_identity_invalid');
    }
    await db.$transaction(async tx => {
      // These IDs originate in the server's authenticated session and stock
      // Eros start response. Recheck all three in the isolated database.
      const rows = await tx.$queryRaw<Array<{ ready: boolean }>>`
        SELECT EXISTS (
          SELECT 1 FROM engine.world_enrollments e
          JOIN engine.world_worldviews w USING(owner_uid)
          WHERE e.owner_uid = p.owner_uid AND e.town_enabled AND e.stories_enabled
            AND w.content ~ '[^[:space:]]'
        ) AS ready
        FROM engine.chat_sessions s
        JOIN engine.persona_instances p ON p.id = s.instance_id
        WHERE s.id = ${sessionId}::uuid AND p.id = ${instanceId}::uuid
          AND s.user_id = ${ownerId}::uuid AND p.owner_uid = s.user_id
          AND s.channel = 'text' AND NOT s.archived
          AND p.status = 'active' AND p.genome_id IS NOT NULL
      `;
      if (rows.length !== 1) throw new Error('eros_world_relationship_invalid');
      if (rows[0].ready) return;
      // Unique owner key handles simultaneous first messages. A nonblank
      // worldview must never change: Eros treats it as a reset.
      await tx.$executeRaw`
        INSERT INTO engine.world_worldviews(owner_uid, content)
        VALUES (${ownerId}::uuid, ${DEFAULT_WORLDVIEW})
        ON CONFLICT(owner_uid) DO UPDATE SET content=EXCLUDED.content, updated_at=now()
          WHERE engine.world_worldviews.content !~ '[^[:space:]]'
      `;
      await tx.$executeRaw`
        INSERT INTO engine.world_enrollments(owner_uid, stories_enabled, town_enabled)
        VALUES (${ownerId}::uuid, true, true)
        ON CONFLICT(owner_uid) DO UPDATE SET stories_enabled=true, town_enabled=true
          WHERE NOT engine.world_enrollments.stories_enabled OR NOT engine.world_enrollments.town_enabled
      `;
    }, { maxWait: 2000, timeout: 5000 });
}
