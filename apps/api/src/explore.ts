import { BadRequestException, Controller, Get, Headers, Query, UnauthorizedException } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import type { ExploreResponseDTO, ExploreTab } from '../../../packages/contracts/src/index.js';
import { DISCOVERY_RANKING_CONFIG } from './discovery-ranking.js';
import { boundedAffinity, diversifyCandidates, PERSONALIZATION_EVENT_WEIGHTS, personalizedScore, recencyDecay } from './discovery-ranking.js';
import { resolveSession } from './session.js';

const prisma = new PrismaClient();
const allowedTabs = new Set<ExploreTab>(['new', 'popular', 'trending', 'for-you']);
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

export async function queryForYou(userId: string, limit = DEFAULT_LIMIT, cursorValue?: string): Promise<ExploreResponseDTO> {
  const safeLimit = Math.max(1, Math.min(Number(limit) || DEFAULT_LIMIT, MAX_LIMIT));
  const cursor = decodeCursor(cursorValue);
  const candidates = await prisma.$queryRaw<Array<{ id: string; slug: string; displayName: string; bio: string | null; vipPriceStars: number | null; tagsJson: string; globalScore: number; follows: number; impressions: number; opens: number; events: Array<{ eventType: string; createdAt: Date }> }>>`
    SELECT c.id, c.slug, c."displayName", cp.bio, MIN(v."priceStars") FILTER (WHERE v.status = 'ACTIVE') AS "vipPriceStars", cp."tagsJson",
      (COUNT(DISTINCT f."userId") * 4 + COUNT(DISTINCT e.id) FILTER (WHERE e."eventType" IN ('CREATOR_PROFILE_VIEW','POST_OPEN','REEL_START')) + COUNT(DISTINCT e.id) FILTER (WHERE e."eventType" IN ('REEL_COMPLETE','VIP_CTA_CLICK')) * 2) AS "globalScore",
      COUNT(DISTINCT ue.id) FILTER (WHERE ue."eventType" = 'FOLLOW_CREATOR') AS follows,
      COUNT(DISTINCT ue.id) FILTER (WHERE ue."eventType" IN ('CREATOR_IMPRESSION','POST_IMPRESSION','REEL_IMPRESSION')) AS impressions,
      COUNT(DISTINCT ue.id) FILTER (WHERE ue."eventType" IN ('CREATOR_PROFILE_VIEW','POST_OPEN','REEL_START','REEL_COMPLETE','VIP_CTA_CLICK')) AS opens,
      COALESCE(json_agg(json_build_object('eventType', ue."eventType", 'createdAt', ue."createdAt")) FILTER (WHERE ue.id IS NOT NULL), '[]') AS events
    FROM "Creator" c JOIN "CreatorProfile" cp ON cp."creatorId" = c.id AND cp.status = 'APPROVED'
    LEFT JOIN "Follow" f ON f."creatorId" = c.id LEFT JOIN "ProductEvent" e ON e."creatorId" = c.id
    LEFT JOIN "ProductEvent" ue ON ue."userId" = ${userId} AND ue."creatorId" = c.id AND ue."createdAt" >= NOW() - INTERVAL '90 days'
    LEFT JOIN "VipOffer" v ON v."creatorId" = c.id
    GROUP BY c.id, c.slug, c."displayName", cp.bio, cp."tagsJson"`;
  const userEvents = candidates.flatMap(c => c.events);
  const tagWeights = new Map<string, number>();
  for (const event of userEvents) { const creator = candidates.find(c => c.events.some(e => e === event)); if (!creator) continue; for (const tag of safeTags(creator.tagsJson)) tagWeights.set(tag, (tagWeights.get(tag) ?? 0) + (PERSONALIZATION_EVENT_WEIGHTS[event.eventType] ?? 0) * recencyDecay((Date.now() - new Date(event.createdAt).getTime()) / 86400000)); }
  const ranked = candidates.map(c => {
    const affinity = boundedAffinity(c.events.reduce((sum, e) => sum + (PERSONALIZATION_EVENT_WEIGHTS[e.eventType] ?? 0) * recencyDecay((Date.now() - new Date(e.createdAt).getTime()) / 86400000), 0));
    const tagAffinity = Math.min(4, safeTags(c.tagsJson).reduce((sum, tag) => sum + (tagWeights.get(tag) ?? 0) * 0.1, 0));
    const seenPenalty = c.impressions >= 3 && c.opens === 0 ? Math.min(4, c.impressions * 0.25) : 0;
    const explorationBoost = affinity < 1 ? 2 : 0;
    return { ...c, score: personalizedScore(Number(c.globalScore), affinity, tagAffinity, seenPenalty, explorationBoost) };
  }).sort((a, b) => b.score - a.score || a.id.localeCompare(b.id));
  const diversified = diversifyCandidates(ranked.map(c => ({ ...c, creatorId: c.id, tags: safeTags(c.tagsJson) })));
  const filtered = cursor ? diversified.filter(x => x.score < cursor.score || (x.score === cursor.score && x.id > cursor.id)) : diversified;
  const items = filtered.slice(0, safeLimit).map(c => ({ id: c.id, slug: c.slug, displayName: c.displayName, bio: c.bio, avatarUrl: null, coverUrl: null, verified: false, vipPriceStars: c.vipPriceStars }));
  const last = filtered.length > safeLimit ? filtered[safeLimit - 1] : undefined;
  return { tab: 'for-you', items, nextCursor: last ? encodeCursor({ score: last.score, id: last.id }) : null };
}

function safeTags(value: string): string[] { try { const parsed = JSON.parse(value); return Array.isArray(parsed) ? parsed.map(String).map(x => x.trim().toLowerCase()).filter(Boolean) : []; } catch { return []; } }

@Controller('explore')
export class ExploreController {
  @Get() async get(@Headers() h: Record<string, string | undefined>, @Query('tab') tab = 'trending', @Query('mode') mode?: string, @Query('limit') limit?: string, @Query('cursor') cursor?: string): Promise<ExploreResponseDTO> { const selected = (mode ?? tab) as ExploreTab; if (!allowedTabs.has(selected)) throw new BadRequestException('invalid explore mode'); if (selected === 'for-you') { const userId = resolveSession(h.authorization?.replace(/^Bearer\s+/i, '')); if (!userId) throw new UnauthorizedException(); return queryForYou(userId, Number(limit) || DEFAULT_LIMIT, cursor); } return queryExplore(selected, Number(limit) || DEFAULT_LIMIT, cursor); }
}
