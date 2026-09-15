import { BadRequestException, Controller, Get, Query } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import type { ExploreResponseDTO, ExploreTab } from '../../../packages/contracts/src/index.js';
import { DISCOVERY_RANKING_CONFIG } from './discovery-ranking.js';

const prisma = new PrismaClient();
const allowedTabs = new Set<ExploreTab>(['new', 'popular', 'trending']);
const DEFAULT_LIMIT = DISCOVERY_RANKING_CONFIG.defaultLimit;
const MAX_LIMIT = DISCOVERY_RANKING_CONFIG.maxLimit;
type Cursor = { score: number; id: string };
function decodeCursor(value?: string): Cursor | undefined { if (!value) return undefined; try { const parsed = JSON.parse(Buffer.from(value, 'base64url').toString('utf8')) as Cursor; if (!parsed.id || !Number.isFinite(parsed.score)) throw new Error(); return parsed; } catch { throw new BadRequestException('invalid explore cursor'); } }
function encodeCursor(cursor: Cursor) { return Buffer.from(JSON.stringify(cursor)).toString('base64url'); }

export async function queryExplore(tab: ExploreTab, limit = DEFAULT_LIMIT, cursorValue?: string): Promise<ExploreResponseDTO> {
  const safeLimit = Math.max(1, Math.min(Number(limit) || DEFAULT_LIMIT, MAX_LIMIT));
  const cursor = decodeCursor(cursorValue);
  const score = tab === 'new' ? `EXTRACT(EPOCH FROM c."createdAt")` : tab === 'popular' ? `(
    COALESCE(COUNT(DISTINCT f."userId") FILTER (WHERE f."createdAt" >= NOW() - INTERVAL '30 days'), 0) * 4
    + COALESCE(COUNT(DISTINCT e.id) FILTER (WHERE e."createdAt" >= NOW() - INTERVAL '30 days' AND e."eventType" IN ('CREATOR_PROFILE_VIEW','POST_OPEN','REEL_START')), 0)
    + COALESCE(COUNT(DISTINCT e.id) FILTER (WHERE e."createdAt" >= NOW() - INTERVAL '30 days' AND e."eventType" = 'REEL_COMPLETE'), 0) * 2
    + COALESCE(COUNT(DISTINCT e.id) FILTER (WHERE e."createdAt" >= NOW() - INTERVAL '30 days' AND e."eventType" = 'VIP_CTA_CLICK'), 0) * 2
  )` : `(
    COALESCE(COUNT(DISTINCT e.id) FILTER (WHERE e."createdAt" >= NOW() - INTERVAL '24 hours' AND e."eventType" = 'CREATOR_PROFILE_VIEW'), 0)
    + COALESCE(COUNT(DISTINCT e.id) FILTER (WHERE e."createdAt" >= NOW() - INTERVAL '24 hours' AND e."eventType" IN ('FOLLOW_CREATOR','REEL_COMPLETE')), 0) * 4
    + COALESCE(COUNT(DISTINCT e.id) FILTER (WHERE e."createdAt" >= NOW() - INTERVAL '24 hours' AND e."eventType" = 'VIP_CTA_CLICK'), 0) * 2
    + COALESCE(COUNT(DISTINCT e.id) FILTER (WHERE e."createdAt" >= NOW() - INTERVAL '24 hours' AND e."eventType" IN ('CREATOR_IMPRESSION','POST_IMPRESSION','REEL_IMPRESSION')), 0) * 0.25
    + EXTRACT(EPOCH FROM c."createdAt") / 86400 / 1000
  )`;
  const cursorWhere = cursor ? `WHERE (score < ${cursor.score} OR (score = ${cursor.score} AND id > '${cursor.id.replaceAll("'", "''")}'))` : '';
  const rows = await prisma.$queryRawUnsafe<Array<{ id: string; slug: string; displayName: string; bio: string | null; vipPriceStars: number | null; score: number }>>(`
    WITH ranked AS (
      SELECT c.id, c.slug, c."displayName", cp.bio, MIN(v."priceStars") FILTER (WHERE v.status = 'ACTIVE') AS "vipPriceStars", ${score} AS score
      FROM "Creator" c JOIN "CreatorProfile" cp ON cp."creatorId" = c.id AND cp.status = 'APPROVED'
      LEFT JOIN "Follow" f ON f."creatorId" = c.id LEFT JOIN "ProductEvent" e ON e."creatorId" = c.id LEFT JOIN "VipOffer" v ON v."creatorId" = c.id
      GROUP BY c.id, c.slug, c."displayName", cp.bio
    ) SELECT * FROM ranked ${cursorWhere} ORDER BY score DESC, id ASC LIMIT ${safeLimit + 1}`);
  const items = rows.slice(0, safeLimit).map(row => ({ id: row.id, slug: row.slug, displayName: row.displayName, bio: row.bio, avatarUrl: null, coverUrl: null, verified: false, vipPriceStars: row.vipPriceStars }));
  const last = rows.length > safeLimit ? rows[safeLimit - 1] : undefined;
  return { tab, items, nextCursor: last ? encodeCursor({ score: Number(last.score), id: last.id }) : null };
}

@Controller('explore')
export class ExploreController {
  @Get() async get(@Query('tab') tab = 'trending', @Query('mode') mode?: string, @Query('limit') limit?: string, @Query('cursor') cursor?: string): Promise<ExploreResponseDTO> { const selected = (mode ?? tab) as ExploreTab; if (!allowedTabs.has(selected)) throw new BadRequestException('invalid explore mode'); return queryExplore(selected, Number(limit) || DEFAULT_LIMIT, cursor); }
}
