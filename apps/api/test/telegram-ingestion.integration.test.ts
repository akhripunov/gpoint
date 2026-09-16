import test from 'node:test';
import assert from 'node:assert/strict';
import { PrismaClient } from '@prisma/client';

test('fixture channel post is idempotent and edited content updates one GPoint post', async () => {
  const prisma = new PrismaClient();
  const stamp = Date.now().toString();
  const creator = await prisma.creator.create({ data: { displayName: `Telegram fixture ${stamp}`, slug: `telegram-fixture-${stamp}`, profile: { create: { status: 'APPROVED', bio: '', tagsJson: '[]' } } } });
  const channel = await prisma.telegramChannel.create({ data: { creatorId: creator.id, telegramChatId: `-${stamp}`, title: 'Fixture', status: 'CONNECTED', importMode: 'AUTO' } });
  try {
    const { TelegramIngestionService } = await import('../src/telegram-ingestion.js');
    const service = new TelegramIngestionService();
    const first = await service.ingest({ channel_post: { chat: { id: channel.telegramChatId }, message_id: 77, date: 1700000000, caption: 'Обычный текст\nБольше фото: @source https://t.me/source', photo: [{}], mediaBytesBase64: Buffer.from('fixture-image').toString('base64'), mediaMimeType: 'image/png' } });
    const duplicate = await service.ingest({ channel_post: { chat: { id: channel.telegramChatId }, message_id: 77, date: 1700000000, caption: 'Обычный текст\nБольше фото: @source https://t.me/source' } });
    const edited = await service.ingest({ edited_channel_post: { chat: { id: channel.telegramChatId }, message_id: 77, edit_date: 1700000100, caption: 'Изменённый текст\n#nogpoint' } });
    assert.equal(first.status, 'CREATED'); assert.equal(duplicate.status, 'DUPLICATE'); assert.equal(edited.status, 'UPDATED');
    const posts = await prisma.post.findMany({ where: { telegramChannelId: channel.id } });
    assert.equal(posts.length, 1); assert.equal(posts[0].caption, 'Изменённый текст'); assert.equal(posts[0].sourceText, 'Изменённый текст\n#nogpoint');
    assert.equal(await prisma.postMedia.count({ where: { postId: posts[0].id } }), 1);
    assert.equal(await prisma.mediaAsset.count({ where: { creatorId: creator.id, status: 'READY' } }), 1);
    assert.equal(await prisma.postTranslation.count({ where: { postId: posts[0].id } }), 0);
  } finally {
    await prisma.postTranslation.deleteMany({ where: { post: { telegramChannelId: channel.id } } });
    const importedPosts = await prisma.post.findMany({ where: { telegramChannelId: channel.id }, select: { id: true } });
    const importedMedia = await prisma.postMedia.findMany({ where: { postId: { in: importedPosts.map(({ id }) => id) } }, select: { mediaAssetId: true } });
    await prisma.postMedia.deleteMany({ where: { postId: { in: importedPosts.map(({ id }) => id) } } });
    await prisma.post.deleteMany({ where: { telegramChannelId: channel.id } });
    await prisma.mediaAsset.deleteMany({ where: { id: { in: importedMedia.map(({ mediaAssetId }) => mediaAssetId) } } });
    await prisma.telegramChannel.delete({ where: { id: channel.id } });
    await prisma.creatorProfile.delete({ where: { creatorId: creator.id } });
    await prisma.creator.delete({ where: { id: creator.id } });
    await prisma.$disconnect();
  }
});
