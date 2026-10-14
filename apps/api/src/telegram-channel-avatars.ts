type ChannelAvatarDatabase = {
  telegramChannel: { findMany: (args: any) => Promise<Array<{ creatorId: string; avatarMediaId: string | null }>> };
  mediaAsset: { findMany: (args: any) => Promise<Array<{ id: string; creatorId: string }>> };
};

export function connectedChannelAvatarFilter(mediaId: string, creatorId: string) {
  return { avatarMediaId: mediaId, creatorId, status: 'CONNECTED', creator: { profile: { status: 'APPROVED' } } };
}

/** Return URLs only for ready image assets owned by their connected channel's creator. */
export async function connectedTelegramChannelAvatarUrls(creatorIds: readonly string[], db: ChannelAvatarDatabase) {
  const ids = [...new Set(creatorIds.filter((id): id is string => typeof id === 'string' && id.length > 0))];
  const result = new Map<string, string>();
  if (!ids.length) return result;

  const channels = await db.telegramChannel.findMany({
    where: { creatorId: { in: ids }, status: 'CONNECTED', avatarMediaId: { not: null } },
    select: { creatorId: true, avatarMediaId: true },
  });
  const channelAvatarIds = [...new Set(channels.flatMap(channel => channel.avatarMediaId ? [channel.avatarMediaId] : []))];
  if (!channelAvatarIds.length) return result;

  const assets = await db.mediaAsset.findMany({
    where: { id: { in: channelAvatarIds }, creatorId: { in: ids }, status: 'READY', kind: 'IMAGE' },
    select: { id: true, creatorId: true },
  });
  const readyAssets = new Set(assets.map(asset => `${asset.creatorId}\0${asset.id}`));
  for (const channel of channels) {
    if (channel.avatarMediaId && readyAssets.has(`${channel.creatorId}\0${channel.avatarMediaId}`)) {
      result.set(channel.creatorId, `/media/assets/${encodeURIComponent(channel.avatarMediaId)}`);
    }
  }
  return result;
}
