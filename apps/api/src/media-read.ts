import { Controller, Get, NotFoundException, Param, Req, Res, StreamableFile } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { readFile } from 'node:fs/promises';
import { join, normalize, relative } from 'node:path';
import { mediaStorageFromEnv } from './media-storage.js';
import { resolveSession } from './session.js';
import { connectedChannelAvatarFilter } from './telegram-channel-avatars.js';

const prisma = new PrismaClient();
const storage = mediaStorageFromEnv();
const mediaRoot = process.env.GPOINT_MEDIA_ROOT ?? '/tmp/gpoint-media';

@Controller()
export class MediaReadController {
  @Get(['media/assets/:id', 'assets/:id'])
  async read(@Param('id') id: string, @Req() req: any, @Res({ passthrough: true }) res: any) {
    const asset = await prisma.mediaAsset.findUnique({ where: { id }, include: { postMedia: { include: { post: { include: { creator: { include: { profile: true, owners: true } } } } } } } });
    if (!asset) throw new NotFoundException('media not found');
    const session = resolveSession(req.headers?.authorization?.replace(/^Bearer\s+/i, ''));
    const isOwner = !!asset.postMedia.some((x: any) => x.post.creator.owners.some((o: any) => o.userId === session));
    const isPublicPost = asset.postMedia.some((x: any) => x.post.status === 'PUBLISHED' && x.post.creator.profile?.status === 'APPROVED');
    const profile = await prisma.creatorProfile.findFirst({ where: { OR: [{ avatarMediaId: id }, { coverMediaId: id }] }, select: { creator: { select: { profile: { select: { status: true } } } } } });
    const isPublicProfile = profile?.creator.profile?.status === 'APPROVED';
    const isConnectedChannelAvatar = !!await prisma.telegramChannel.findFirst({ where: connectedChannelAvatarFilter(id, asset.creatorId), select: { id: true } });
    const isAdmin = session ? !!await prisma.userRole.findUnique({ where: { userId_role: { userId: session, role: 'ADMIN' } } }) : false;
    if (asset.status !== 'READY' || (!isOwner && !isAdmin && !isPublicPost && !isPublicProfile && !isConnectedChannelAvatar)) throw new NotFoundException('media unavailable');
    if (asset.storageProvider === 'local') {
      const path = normalize(join(mediaRoot, asset.storageKey));
      if (relative(mediaRoot, path).startsWith('..') || !relative(mediaRoot, path)) throw new NotFoundException('media unavailable');
      try { return new StreamableFile(await readFile(path), { type: asset.mimeType }); } catch { throw new NotFoundException('media unavailable'); }
    }
    res.redirect(await storage.getReadUrl(asset.storageKey), 302);
    return;
  }
}
