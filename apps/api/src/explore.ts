import { Controller, Get, Query } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import type { ExploreResponseDTO, ExploreTab } from '../../../packages/contracts/src/index.js';

const prisma = new PrismaClient();
const allowedTabs = new Set<ExploreTab>(['new', 'popular', 'trending']);

export async function queryExplore(tab: ExploreTab, limit = 24): Promise<ExploreResponseDTO> {
  const order = tab === 'new' ? 'MAX(p."publishedAt") DESC NULLS LAST, c.id' : tab === 'popular' ? 'COUNT(DISTINCT f."userId") * 4 + COUNT(DISTINCT e.id) DESC, c.id' : 'COUNT(DISTINCT CASE WHEN e."createdAt" >= NOW() - INTERVAL \'24 hours\' AND e."eventType" = \'CREATOR_PROFILE_VIEW\' THEN e.id END) + COUNT(DISTINCT CASE WHEN e."createdAt" >= NOW() - INTERVAL \'24 hours\' AND e."eventType" = \'FOLLOW_CREATOR\' THEN e.id END) * 4 + COUNT(DISTINCT CASE WHEN e."createdAt" >= NOW() - INTERVAL \'24 hours\' AND e."eventType" = \'VIP_CTA_CLICK\' THEN e.id END) * 8 DESC, c.id';
  const rows = await prisma.$queryRawUnsafe<Array<{id:string;slug:string;displayName:string;bio:string|null;publishedAt:Date|null;vipPriceStars:number|null}>>(`SELECT c.id,c.slug,c."displayName",cp.bio,MAX(p."publishedAt") AS "publishedAt",MIN(v."priceStars") AS "vipPriceStars" FROM "Creator" c JOIN "CreatorProfile" cp ON cp."creatorId"=c.id AND cp.status='APPROVED' LEFT JOIN "Post" p ON p."creatorId"=c.id AND p.status='PUBLISHED' LEFT JOIN "Follow" f ON f."creatorId"=c.id LEFT JOIN "ProductEvent" e ON e."creatorId"=c.id LEFT JOIN "VipOffer" v ON v."creatorId"=c.id AND v.status='APPROVED' GROUP BY c.id,c.slug,c."displayName",cp.bio ORDER BY ${order} LIMIT ${Math.max(1, Math.min(limit, 50))}`);
  return { tab, items: rows.map(row => ({ id:row.id, slug:row.slug, displayName:row.displayName, bio:row.bio, avatarUrl:null, coverUrl:null, verified:false, vipPriceStars:row.vipPriceStars })), nextCursor:null };
}

@Controller('explore')
export class ExploreController { @Get() async get(@Query('tab') tab = 'trending'): Promise<ExploreResponseDTO> { if (!allowedTabs.has(tab as ExploreTab)) throw new Error('invalid explore tab'); return queryExplore(tab as ExploreTab); } }
