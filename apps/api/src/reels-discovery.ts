import { Controller, Get, Headers, Query } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { resolveSession } from './session.js';
import { diversifyCandidates } from './discovery-ranking.js';
const prisma = new PrismaClient();
@Controller('reels') export class PersonalizedReelsController {
  @Get() async get(@Headers() h: Record<string, string | undefined>, @Query('cursor') cursor?: string) {
    const userId = resolveSession(h.authorization?.replace(/^Bearer\s+/i, ''));
    const prefs = userId ? await prisma.userPreference.findMany({ where: { userId }, select: { preferenceType: true, entityType: true, entityId: true } }) : [];
    const blocked = new Set(prefs.filter(p => p.preferenceType === 'DO_NOT_RECOMMEND_CREATOR' && p.entityType === 'CREATOR').map(p => p.entityId));
    const hidden = new Set(prefs.filter(p => p.preferenceType === 'NOT_INTERESTED').map(p => `${p.entityType}:${p.entityId}`));
    const rows = await prisma.post.findMany({ where: { isReel: true, status: 'PUBLISHED', creator: { profile: { status: 'APPROVED' } } }, orderBy: [{ publishedAt: 'desc' }, { id: 'desc' }], take: 13, cursor: cursor ? { id: cursor } : undefined, skip: cursor ? 1 : 0, include: { media: { include: { media: true } }, creator: { include: { profile: true, vipOffers: { where: { status: 'ACTIVE' }, take: 1 } } } } });
    const eligible = rows.filter(p => !blocked.has(p.creatorId) && !hidden.has(`REEL:${p.id}`) && !hidden.has(`POST:${p.id}`));
    let ordered: any[] = eligible;
    if (userId) {
      const events = await prisma.productEvent.findMany({ where: { userId, creatorId: { in: eligible.map(p => p.creatorId) }, createdAt: { gte: new Date(Date.now() - 90 * 86400000) } }, select: { creatorId: true, eventType: true, createdAt: true } });
      const scores = new Map<string, number>(); for (const e of events) { const age = (Date.now() - e.createdAt.getTime()) / 86400000; const weight = e.eventType === 'REEL_COMPLETE' ? 3 : e.eventType === 'FOLLOW_CREATOR' ? 4 : e.eventType === 'VIP_CTA_CLICK' ? 5 : e.eventType === 'REEL_START' ? 1 : 0; scores.set(e.creatorId!, (scores.get(e.creatorId!) ?? 0) + weight * Math.pow(.5, age / 14)); }
      ordered = diversifyCandidates([...eligible].sort((a, b) => (scores.get(b.creatorId) ?? 0) - (scores.get(a.creatorId) ?? 0) || a.id.localeCompare(b.id)).map((p: any) => ({ ...p, score: scores.get(p.creatorId) ?? 0, tags: JSON.parse(p.creator.profile?.tagsJson ?? '[]') })));
    }
    return { items: ordered.slice(0, 12).map((p: any) => ({ id: p.id, type: p.type, caption: p.caption ?? null, publishedAt: p.publishedAt?.toISOString() ?? null, media: p.media.map((m: any) => ({ id: m.media.id, kind: m.media.kind, mimeType: m.media.mimeType, width: m.media.width ?? null, height: m.media.height ?? null, aspectRatio: m.media.aspectRatio ?? null, blurHash: m.media.blurHash ?? null, durationMs: m.media.durationMs ?? null, readUrl: `/media/assets/${encodeURIComponent(m.media.id)}` })), creator: { id: p.creator.id, slug: p.creator.slug, displayName: p.creator.displayName, bio: p.creator.profile?.bio ?? null, avatarUrl: null, coverUrl: null, verified: false, vipPriceStars: p.creator.vipOffers[0]?.priceStars ?? null, vipOffer: p.creator.vipOffers[0] ? { id: p.creator.vipOffers[0].id, title: p.creator.vipOffers[0].title, description: p.creator.vipOffers[0].description, priceStars: p.creator.vipOffers[0].priceStars, billingLabel: p.creator.vipOffers[0].billingLabel } : undefined } })), nextCursor: rows.length > 12 ? rows[11].id : null };
  }
}
