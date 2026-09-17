import { randomInt } from 'node:crypto';
import { BadRequestException, Body, Controller, Delete, Get, Headers, Param, Patch, Post, UnauthorizedException } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { resolveSession } from './session.js';

const prisma = new PrismaClient();
const modes = new Set(['AUTO', 'HASHTAG', 'MANUAL']);
const challengeLifetimeMs = 10 * 60_000;
function userId(h: Record<string, string | undefined>) { const id = resolveSession(h.authorization?.replace(/^Bearer\s+/i, '')); if (!id) throw new UnauthorizedException(); return id; }
async function owned(h: Record<string, string | undefined>) { const id = userId(h); const owner = await prisma.creatorOwner.findFirst({ where: { userId: id }, select: { creatorId: true } }); if (!owner) throw new UnauthorizedException(); return { creatorId: owner.creatorId, userId: id }; }
function botToken() { const value = process.env.TELEGRAM_BOT_TOKEN; if (!value) throw new BadRequestException('Telegram bot is not configured'); return value; }
async function telegramApi<T>(method: string, body: Record<string, unknown>): Promise<T> { const response = await fetch(`https://api.telegram.org/bot${botToken()}/${method}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }); const data = await response.json() as { ok?: boolean; result?: T; description?: string }; if (!response.ok || !data.ok || data.result === undefined) throw new BadRequestException(data.description ?? `Telegram ${method} failed`); return data.result; }
function publicChannel(c: any) { if (!c) return null; return { id: c.id, title: c.title, username: c.username, status: c.status, importMode: c.importMode, importHashtag: c.importHashtag, connectedAt: c.connectedAt, disconnectedAt: c.disconnectedAt, lastPostReceivedAt: c.lastPostReceivedAt, lastSuccessfulIngestAt: c.lastSuccessfulIngestAt, lastError: c.lastError }; }

@Controller('creator/channel')
export class TelegramConnectionController {
  @Post('connect/start')
  async start(@Headers() h: Record<string, string | undefined>) {
    const { userId: id } = await owned(h); const user = await prisma.user.findUnique({ where: { id }, select: { telegramUserId: true } });
    if (!user || user.telegramUserId <= 0n) throw new BadRequestException('A real Telegram account is required for channel selection');
    const requestId = randomInt(1, 2_147_483_647); const challenge = await prisma.telegramConnectionChallenge.create({ data: { requestId, userId: id, expiresAt: new Date(Date.now() + challengeLifetimeMs) } });
    const keyboard = { keyboard: [[{ text: 'Select Telegram channel', request_chat: { request_id: requestId, chat_is_channel: true, user_administrator_rights: { can_post_messages: true }, bot_administrator_rights: { can_post_messages: true }, bot_is_member: true, request_title: true, request_username: true } }]], resize_keyboard: true, one_time_keyboard: true };
    await telegramApi('sendMessage', { chat_id: user.telegramUserId.toString(), text: 'Select the channel you administer and where GPoint is already an administrator. The request expires in 10 minutes.', reply_markup: keyboard });
    return { challengeId: challenge.id, requestId, expiresAt: challenge.expiresAt, botUsername: process.env.TELEGRAM_BOT_USERNAME ?? null, instructions: 'Open the GPoint bot chat and tap Select Telegram channel.' };
  }
}

@Controller('creator/me/telegram-channel')
export class TelegramChannelController {
  @Get() async get(@Headers() h: Record<string, string | undefined>) { const { creatorId } = await owned(h); return publicChannel(await prisma.telegramChannel.findUnique({ where: { creatorId } })); }
  @Patch() async update(@Headers() h: Record<string, string | undefined>, @Body() body: { importMode?: string; importHashtag?: string }) { const { creatorId } = await owned(h); if (!body.importMode || !modes.has(body.importMode)) throw new BadRequestException('invalid import mode'); return publicChannel(await prisma.telegramChannel.update({ where: { creatorId }, data: { importMode: body.importMode, importHashtag: body.importHashtag?.trim().replace(/^#/, '') || null } })); }
  @Delete() async disconnect(@Headers() h: Record<string, string | undefined>) { const { creatorId } = await owned(h); return publicChannel(await prisma.telegramChannel.update({ where: { creatorId }, data: { status: 'DISCONNECTED', disconnectedAt: new Date() } })); }
  @Post('posts/:id/archive') async archive(@Headers() h: Record<string, string | undefined>, @Param('id') id: string) { const { creatorId } = await owned(h); const post = await prisma.post.findFirst({ where: { id, creatorId, source: 'TELEGRAM_CHANNEL' } }); if (!post) throw new BadRequestException('imported post not found'); return prisma.post.update({ where: { id }, data: { status: 'HIDDEN', ingestStatus: 'ARCHIVED' } }); }
}

@Controller('internal/telegram')
export class TelegramChannelVerificationController {
  @Post('chat-shared')
  async chatShared(@Headers() headers: Record<string, string | undefined>, @Body() body: { from?: { id?: number | string }; chat_shared?: { request_id?: number; chat_id?: number | string; title?: string; username?: string } }) {
    const expected = process.env.TELEGRAM_WEBHOOK_SECRET; if (!expected || headers['x-telegram-webhook-secret'] !== expected) throw new UnauthorizedException();
    const senderId = body.from?.id?.toString(); const shared = body.chat_shared; if (!senderId || !shared?.request_id || shared.chat_id === undefined) throw new BadRequestException('chat_shared data required');
    const challenge = await prisma.telegramConnectionChallenge.findUnique({ where: { requestId: shared.request_id }, include: { user: true } });
    if (!challenge || challenge.usedAt || challenge.expiresAt.getTime() <= Date.now()) throw new UnauthorizedException('challenge expired, used or unknown');
    if (challenge.user.telegramUserId.toString() !== senderId) throw new UnauthorizedException('challenge belongs to another Telegram user');
    const chatId = shared.chat_id.toString(); const chat = await telegramApi<{ id: number; type: string; title?: string; username?: string }>('getChat', { chat_id: chatId }); if (chat.type !== 'channel' || chat.id.toString() !== chatId) throw new BadRequestException('selected chat is not the selected channel');
    const bot = await telegramApi<{ id: number }>('getMe', {}); const [member, botMember] = await Promise.all([telegramApi<{ status: string; can_post_messages?: boolean }>('getChatMember', { chat_id: chatId, user_id: senderId }), telegramApi<{ status: string; can_post_messages?: boolean }>('getChatMember', { chat_id: chatId, user_id: bot.id })]);
    const userIsAdmin = member.status === 'creator' || (member.status === 'administrator' && member.can_post_messages === true); const botCanPost = botMember.status === 'administrator' && botMember.can_post_messages === true; if (!userIsAdmin || !botCanPost) throw new UnauthorizedException('channel ownership or bot posting permission could not be verified');
    const owner = await prisma.creatorOwner.findFirstOrThrow({ where: { userId: challenge.userId }, select: { creatorId: true } }); const existing = await prisma.telegramChannel.findUnique({ where: { telegramChatId: chatId } }); if (existing && existing.creatorId !== owner.creatorId) throw new BadRequestException('channel is already connected to another creator');
    const connected = await prisma.$transaction(async tx => { await tx.telegramConnectionChallenge.update({ where: { id: challenge.id }, data: { usedAt: new Date(), telegramChatId: chatId } }); return tx.telegramChannel.upsert({ where: { creatorId: owner.creatorId }, create: { creatorId: owner.creatorId, telegramChatId: chatId, title: chat.title ?? shared.title, username: chat.username ?? shared.username, status: 'CONNECTED', importMode: 'AUTO', connectedAt: new Date(), disconnectedAt: null, lastError: null }, update: { telegramChatId: chatId, title: chat.title ?? shared.title, username: chat.username ?? shared.username, status: 'CONNECTED', connectedAt: new Date(), disconnectedAt: null, lastError: null } }); });
    return { status: 'CONNECTED', channel: publicChannel(connected) };
  }
}
