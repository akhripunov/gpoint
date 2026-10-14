import assert from 'node:assert/strict';
import test from 'node:test';
import { connectedTelegramChannelAvatarUrls, connectedChannelAvatarFilter } from '../src/telegram-channel-avatars.js';
import { postDto } from '../src/feed.js';
import { exploreCreatorDto } from '../src/explore.js';
import { messageThreadCreatorDto } from '../src/messages.js';

const avatarId = 'media-channel-a';
const url = `/media/assets/${avatarId}`;

function avatarDatabase() {
  const calls: Record<string, any> = {};
  return {
    calls,
    prisma: {
      telegramChannel: {
        async findMany(args: any) {
          calls.channels = args;
          return [
            { creatorId: 'creator-a', avatarMediaId: avatarId },
            { creatorId: 'creator-b', avatarMediaId: 'media-channel-b' },
            { creatorId: 'creator-c', avatarMediaId: 'media-failed' },
            { creatorId: 'creator-d', avatarMediaId: 'media-foreign' },
            { creatorId: 'creator-e', avatarMediaId: null },
          ];
        },
      },
      mediaAsset: {
        async findMany(args: any) {
          calls.assets = args;
          return [
            { id: avatarId, creatorId: 'creator-a' },
            { id: 'media-channel-b', creatorId: 'creator-b' },
            { id: 'media-foreign', creatorId: 'creator-z' },
          ];
        },
      },
    },
  };
}

test('avatar lookup returns only ready images owned by their connected channel creator', async () => {
  const { prisma, calls } = avatarDatabase();
  const avatars = await connectedTelegramChannelAvatarUrls(
    ['creator-a', 'creator-b', 'creator-c', 'creator-d', 'creator-e'],
    prisma as any,
  );

  assert.deepEqual([...avatars], [
    ['creator-a', `/media/assets/${avatarId}`],
    ['creator-b', '/media/assets/media-channel-b'],
  ]);
  assert.deepEqual(calls.channels.where, {
    creatorId: { in: ['creator-a', 'creator-b', 'creator-c', 'creator-d', 'creator-e'] },
    status: 'CONNECTED',
    avatarMediaId: { not: null },
  });
  assert.deepEqual(calls.assets.where, {
    id: { in: [avatarId, 'media-channel-b', 'media-failed', 'media-foreign'] },
    creatorId: { in: ['creator-a', 'creator-b', 'creator-c', 'creator-d', 'creator-e'] },
    status: 'READY',
    kind: 'IMAGE',
  });
});

test('an empty creator set does not query storage', async () => {
  const { prisma, calls } = avatarDatabase();
  assert.deepEqual(await connectedTelegramChannelAvatarUrls([], prisma as any), new Map());
  assert.deepEqual(calls, {});
});

test('public media access binds connected channel avatars to the same creator', () => {
  assert.deepEqual(connectedChannelAvatarFilter(avatarId, 'creator-a'), {
    avatarMediaId: avatarId,
    creatorId: 'creator-a',
    status: 'CONNECTED',
    creator: { profile: { status: 'APPROVED' } },
  });
});

test('feed, explore and messages serialize the connected channel avatar, never a different profile avatar', async () => {
  const { prisma } = avatarDatabase();
  const avatars = await connectedTelegramChannelAvatarUrls(['creator-a', 'creator-e'], prisma as any);
  const channelAvatar = avatars.get('creator-a') ?? null;

  const post = await postDto({
    id: 'post-1', type: 'IMAGE', caption: null, publishedAt: null, creatorId: 'creator-a',
    creator: { id: 'creator-a', slug: 'models-hub', displayName: 'Models Hub', profile: { avatarMediaId: 'unrelated-profile-media' } },
    media: [],
  }, 'viewer-a', channelAvatar);
  const explore = exploreCreatorDto({ id: 'creator-a', slug: 'models-hub', displayName: 'Models Hub', bio: null, vipPriceStars: null }, channelAvatar);
  const messageCreator = messageThreadCreatorDto({
    id: 'creator-a', slug: 'models-hub', displayName: 'Models Hub', avatarUrl: '/media/assets/unrelated-profile-media', verified: false,
  }, channelAvatar);

  assert.equal(post.creator?.avatarUrl, url);
  assert.equal(explore.avatarUrl, url);
  assert.equal(messageCreator.avatarUrl, url);

  const withoutChannelAvatar = avatars.get('creator-e') ?? null;
  assert.equal((await postDto({ ...post, creatorId: 'creator-e' }, 'viewer-a', withoutChannelAvatar)).creator?.avatarUrl, null);
  assert.equal(exploreCreatorDto({ id: 'creator-e', slug: 'persona', displayName: 'Persona', bio: null, vipPriceStars: null }, withoutChannelAvatar).avatarUrl, null);
  assert.equal(messageThreadCreatorDto({ ...messageCreator, avatarUrl: '/media/assets/unrelated-profile-media' }, withoutChannelAvatar).avatarUrl, null);
});
