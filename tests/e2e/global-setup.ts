import { PrismaClient } from '@prisma/client';

/**
 * Remove only data owned by the reserved browser-E2E namespace.
 * This is suite hygiene, not a business-state mutation: every moderation
 * transition in a scenario is still performed through the UI/API.
 */
export default async function globalSetup() {
  const apiUrl = process.env.GPOINT_E2E_API_URL ?? 'http://127.0.0.1:3013';
  const authResponse = await fetch(`${apiUrl}/auth/browser`, { method: 'POST', headers: { origin: process.env.GPOINT_MINIAPP_URL ?? 'http://127.0.0.1:5183' } });
  if (authResponse.status !== 201) throw new Error(`E2E browser auth readiness failed: ${authResponse.status}`);
  const authData = await authResponse.json() as { accessToken?: string };
  if (!authData.accessToken) throw new Error('E2E browser auth readiness returned no session');
  const feedResponse = await fetch(`${apiUrl}/feed/for-you`, { headers: { authorization: `Bearer ${authData.accessToken}` } });
  if (feedResponse.status !== 200) throw new Error(`E2E feed readiness failed: ${feedResponse.status}`);
  const prisma = new PrismaClient();
  try {
    const creators = await prisma.creator.findMany({
      where: { slug: { startsWith: 'e2e-creator-' } },
      select: { id: true },
    });
    const creatorIds = creators.map(({ id }) => id);
    if (creatorIds.length === 0) return;

    const posts = await prisma.post.findMany({
      where: { creatorId: { in: creatorIds } },
      select: { id: true },
    });
    const postIds = posts.map(({ id }) => id);
    const offers = await prisma.vipOffer.findMany({
      where: { creatorId: { in: creatorIds } },
      select: { id: true },
    });
    const offerIds = offers.map(({ id }) => id);
    const bots = await prisma.creatorBot.findMany({
      where: { creatorId: { in: creatorIds } },
      select: { id: true },
    });
    const botIds = bots.map(({ id }) => id);
    const links = await prisma.affiliateLink.findMany({
      where: { creatorBotId: { in: botIds } },
      select: { id: true },
    });
    const linkIds = links.map(({ id }) => id);

    await prisma.$transaction(async (tx) => {
      await tx.affiliateClick.deleteMany({
        where: {
          OR: [
            { creatorId: { in: creatorIds } },
            { vipOfferId: { in: offerIds } },
            { creatorBotId: { in: botIds } },
            { affiliateLinkId: { in: linkIds } },
          ],
        },
      });
      await tx.follow.deleteMany({ where: { creatorId: { in: creatorIds } } });
      await tx.productEvent.deleteMany({ where: { creatorId: { in: creatorIds } } });
      await tx.moderationAction.deleteMany({
        where: {
          OR: [
            { targetType: 'CREATOR', targetId: { in: creatorIds } },
            { targetType: 'POST', targetId: { in: postIds } },
            { targetType: 'VIP_OFFER', targetId: { in: offerIds } },
          ],
        },
      });
      await tx.postMedia.deleteMany({ where: { postId: { in: postIds } } });
      await tx.post.deleteMany({ where: { id: { in: postIds } } });
      await tx.mediaAsset.deleteMany({ where: { creatorId: { in: creatorIds } } });
      await tx.vipOffer.deleteMany({ where: { id: { in: offerIds } } });
      await tx.affiliateLink.deleteMany({ where: { id: { in: linkIds } } });
      await tx.affiliateProgram.deleteMany({ where: { creatorBotId: { in: botIds } } });
      await tx.creatorBot.deleteMany({ where: { id: { in: botIds } } });
      await tx.creatorProfile.deleteMany({ where: { creatorId: { in: creatorIds } } });
      await tx.creatorOwner.deleteMany({ where: { creatorId: { in: creatorIds } } });
      await tx.creator.deleteMany({ where: { id: { in: creatorIds } } });
    });
  } finally {
    await prisma.$disconnect();
  }
}
