import { BadRequestException, Body, Controller, Get, Headers, Post, UnauthorizedException } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { resolveSession } from './session.js';

const prisma = new PrismaClient();
const eventTypes = new Set(['CREATOR_IMPRESSION','CREATOR_PROFILE_VIEW','POST_IMPRESSION','POST_OPEN','REEL_IMPRESSION','REEL_START','REEL_COMPLETE','FOLLOW_CREATOR','UNFOLLOW_CREATOR','VIP_CTA_CLICK']);
function auth(h: Record<string, string | undefined>) { const id = resolveSession(h.authorization?.replace(/^Bearer\s+/i, '')); if (!id) throw new UnauthorizedException(); return id; }

@Controller('events')
export class EngagementController {
  @Post() async record(@Headers() h: Record<string, string | undefined>, @Body() body: { eventType?: string; entityType?: string; entityId?: string; surface?: string }) {
    const userId = auth(h); const eventType = body.eventType;
    if (!eventType || !eventTypes.has(eventType)) throw new BadRequestException('unsupported engagement event');
    let creatorId = body.entityType === 'CREATOR' ? body.entityId : undefined;
    if (body.entityId && (body.entityType === 'POST' || body.entityType === 'REEL')) {
      const post = await prisma.post.findUnique({ where: { id: body.entityId }, select: { creatorId: true } });
      creatorId = post?.creatorId;
    } else if (body.entityType === 'VIP_OFFER') {
      const offer = await prisma.vipOffer.findUnique({ where: { id: body.entityId }, select: { creatorId: true } });
      creatorId = offer?.creatorId;
    }
    if (creatorId) { const owner = await prisma.creatorOwner.findFirst({ where: { userId, creatorId } }); if (owner) return { recorded: false, deduped: true }; }
    const deduped = eventType.endsWith('IMPRESSION') || eventType === 'CREATOR_PROFILE_VIEW';
    if (deduped) {
      const existing = await prisma.productEvent.findFirst({ where: { userId, eventType, entityType: body.entityType, entityId: body.entityId, createdAt: { gte: new Date(Date.now() - 30 * 60 * 1000) } } });
      if (existing) return { recorded: false, deduped: true };
    }
    await prisma.productEvent.create({ data: { userId, eventType, entityType: body.entityType, entityId: body.entityId, creatorId, metadataJson: body.surface ? { surface: body.surface, rankingVersion: body.surface === 'FOR_YOU' ? 'personalized-v2' : 'global-v1' } : undefined } });
    return { recorded: true, deduped: false };
  }
}

@Controller('discovery')
export class DiscoveryHealthController {
  @Get('health') async health(@Headers() h: Record<string, string | undefined>) {
    const userId = auth(h);
    const admin = await prisma.userRole.findUnique({ where: { userId_role: { userId, role: 'ADMIN' } } });
    if (!admin) throw new UnauthorizedException('admin required');
    const now = Date.now();
    const day = new Date(now - 24 * 60 * 60 * 1000);
    const week = new Date(now - 7 * 24 * 60 * 60 * 1000);
    const [dau, eventCounts, returning] = await Promise.all([
      prisma.productEvent.findMany({ where: { createdAt: { gte: day }, userId: { not: null } }, distinct: ['userId'], select: { userId: true } }),
      prisma.productEvent.groupBy({ by: ['eventType'], where: { createdAt: { gte: week } }, _count: { _all: true } }),
      prisma.productEvent.findMany({ where: { createdAt: { gte: day }, userId: { not: null } }, distinct: ['userId'], select: { userId: true } }),
    ]);
    const returningUsers = returning.length ? await prisma.productEvent.findMany({ where: { userId: { in: returning.flatMap(x => x.userId ? [x.userId] : []) }, createdAt: { lt: day } }, distinct: ['userId'], select: { userId: true } }) : [];
    const counts = Object.fromEntries(eventCounts.map(x => [x.eventType, x._count._all]));
    const [forYou] = await prisma.$queryRaw<Array<{ impressions: bigint; profileOpens: bigint; follows: bigint; reelCompletions: bigint; vipClicks: bigint }>>`
      SELECT COUNT(*) FILTER (WHERE "eventType" LIKE '%IMPRESSION') AS impressions,
        COUNT(*) FILTER (WHERE "eventType" = 'CREATOR_PROFILE_VIEW') AS "profileOpens",
        COUNT(*) FILTER (WHERE "eventType" = 'FOLLOW_CREATOR') AS follows,
        COUNT(*) FILTER (WHERE "eventType" = 'REEL_COMPLETE') AS "reelCompletions",
        COUNT(*) FILTER (WHERE "eventType" = 'VIP_CTA_CLICK') AS "vipClicks"
      FROM "ProductEvent" WHERE "createdAt" >= ${week} AND "metadataJson"->>'surface' = 'FOR_YOU'`;
    const rate = (n: bigint, d: bigint) => Number(d) ? Math.min(1, Number(n) / Number(d)) : 0;
    return { window: '7d', dau24h: dau.length, returning24h: returningUsers.length, eventCounts: counts, forYou: { impressions: Number(forYou.impressions), profileOpenRate: rate(forYou.profileOpens, forYou.impressions), followConversionRate: rate(forYou.follows, forYou.impressions), reelCompletionRate: rate(forYou.reelCompletions, forYou.impressions), vipCtaRate: rate(forYou.vipClicks, forYou.impressions) } };
  }
}

@Controller('following')
export class FollowingController {
  @Get() async feed(@Headers() h: Record<string, string | undefined>) {
    const userId = auth(h);
    const rows = await prisma.post.findMany({ where: { status: 'PUBLISHED', creator: { profile: { status: 'APPROVED' }, follows: { some: { userId } } } }, orderBy: [{ publishedAt: 'desc' }, { id: 'desc' }], take: 24, include: { media: { include: { media: true } }, creator: { include: { profile: true, vipOffers: { where: { status: 'ACTIVE' }, take: 1 } } } } });
    return { items: rows.map(p => ({ id: p.id, type: p.type, isReel: p.isReel, caption: p.caption, publishedAt: p.publishedAt?.toISOString() ?? null, creator: { id: p.creator.id, slug: p.creator.slug, displayName: p.creator.displayName }, media: p.media.map(m => ({ id: m.media.id, kind: m.media.kind, readUrl: `/media/assets/${encodeURIComponent(m.media.id)}` })) })), nextCursor: null };
  }
}
