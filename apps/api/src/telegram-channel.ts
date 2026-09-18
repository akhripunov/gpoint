import { randomInt } from 'node:crypto';
import { BadRequestException, Body, Controller, Delete, Get, Headers, Param, Patch, Post, UnauthorizedException } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { resolveSession } from './session.js';

const prisma = new PrismaClient();
const modes = new Set(['AUTO', 'HASHTAG', 'MANUAL']);
const challengeLifetimeMs = 10 * 60_000;
export const telegramApiTimeoutMs = 8_000;
const userChannelAdministratorRights = { is_anonymous: false, can_manage_chat: true, can_delete_messages: false, can_manage_video_chats: false, can_restrict_members: false, can_promote_members: false, can_change_info: false, can_invite_users: false, can_post_stories: false, can_edit_stories: false, can_delete_stories: false, can_send_welcome_messages: false, can_post_messages: false } as const;
export function preparedChannelButton(requestId: number) { if (!Number.isInteger(requestId) || requestId < -2_147_483_648 || requestId > 2_147_483_647) throw new Error('request_id must be a signed 32-bit integer'); return { text: 'Select Telegram channel', request_chat: { request_id: requestId, chat_is_channel: true, request_title: true, request_username: true, request_photo: true, user_administrator_rights: userChannelAdministratorRights } }; }
export function preparedButtonId(result: { id?: unknown }) { if (typeof result.id !== 'string' || result.id.length === 0) throw new Error('Telegram returned an invalid PreparedKeyboardButton'); return result.id; }
function userId(h: Record<string, string | undefined>) { const id = resolveSession(h.authorization?.replace(/^Bearer\s+/i, '')); if (!id) throw new UnauthorizedException(); return id; }
async function authenticated(h: Record<string, string | undefined>) { const id = userId(h); const user = await prisma.user.findUnique({ where: { id }, select: { id: true, telegramUserId: true } }); if (!user) throw new UnauthorizedException(); return { userId: id, user }; }
async function owned(h: Record<string, string | undefined>) { const { userId: id } = await authenticated(h); const owner = await prisma.creatorOwner.findFirst({ where: { userId: id }, select: { creatorId: true } }); if (!owner) throw new UnauthorizedException(); return { creatorId: owner.creatorId, userId: id }; }
function botToken() { const value = process.env.TELEGRAM_BOT_TOKEN; if (!value) throw new BadRequestException('Telegram bot is not configured'); return value; }
async function telegramApi<T>(method: string, body: Record<string, unknown>): Promise<T> { const startedAt = Date.now(); const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), telegramApiTimeoutMs); console.info('[Telegram] Bot API started', { method, startedAt: new Date(startedAt).toISOString() }); try { const response = await fetch(`https://api.telegram.org/bot${botToken()}/${method}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body), signal: controller.signal }); const data = await response.json() as { ok?: boolean; result?: T; error_code?: number; description?: string }; console.info('[Telegram] Bot API finished', { method, finishedAt: new Date().toISOString(), latencyMs: Date.now() - startedAt, httpStatus: response.status, ok: data.ok === true }); if (!response.ok || !data.ok || data.result === undefined) { console.error('[Telegram] Bot API failure', { method, httpStatus: response.status, errorCode: data.error_code ?? null, description: data.description ?? null }); throw new BadRequestException(data.description ?? `Telegram ${method} failed`); } return data.result; } catch (error) { const timedOut = error instanceof DOMException && error.name === 'AbortError'; console.error('[Telegram] Bot API request failed', { method, finishedAt: new Date().toISOString(), latencyMs: Date.now() - startedAt, reason: timedOut ? 'TIMEOUT' : 'ERROR' }); if (timedOut) throw new BadRequestException(`Telegram ${method} timed out`); throw error; } finally { clearTimeout(timer); } }
function channelSlug(value: string) { const normalized = value.toLowerCase().trim().replace(/^@/, '').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40); return normalized || 'creator'; }
async function uniqueSlug(value: string) { const base = channelSlug(value); let candidate = base; for (let index = 2; await prisma.creator.findUnique({ where: { slug: candidate }, select: { id: true } }); index += 1) { const suffix = `-${index}`; candidate = `${base.slice(0, 40 - suffix.length)}${suffix}`; } return candidate; }
function publicChannel(c: any) { if (!c) return null; return { id: c.id, title: c.title, username: c.username, status: c.status, importMode: c.importMode, importHashtag: c.importHashtag, connectedAt: c.connectedAt, disconnectedAt: c.disconnectedAt, lastPostReceivedAt: c.lastPostReceivedAt, lastSuccessfulIngestAt: c.lastSuccessfulIngestAt, lastError: c.lastError }; }
export function userIsChannelAdmin(member: { status: string; can_manage_chat?: boolean; can_post_messages?: boolean; can_promote_members?: boolean }) { return member.status === 'creator' || (member.status === 'administrator' && (member.can_manage_chat === true || member.can_post_messages === true)); }
export function botCanPost(member: { status: string; can_post_messages?: boolean } | null) { return !!member && (member.status === 'administrator' || member.status === 'creator') && member.can_post_messages === true; }
async function optionalBotMember(chatId: string, botId: number) { try { return await telegramApi<{ status: string; can_post_messages?: boolean }>('getChatMember', { chat_id: chatId, user_id: botId }); } catch (error) { const message = error instanceof Error ? error.message : String(error); if (/not found|not a member|chat member|USER_NOT_PARTICIPANT|user not found|member not found/i.test(message)) return null; throw error; } }
async function ensurePendingChannel(challenge: any, chat: { title?: string; username?: string; description?: string }, shared: { title?: string; username?: string }, state: 'WAITING_FOR_BOT' | 'WAITING_FOR_BOT_RIGHTS') {
  const currentOwner = await prisma.creatorOwner.findFirst({ where: { userId: challenge.userId }, select: { creatorId: true } });
  const creatorId = currentOwner?.creatorId ?? (await prisma.creator.create({ data: { displayName: chat.title ?? shared.title ?? 'Telegram Creator', slug: await uniqueSlug(chat.username ?? chat.title ?? 'creator'), profile: { create: { bio: chat.description ?? '', tagsJson: '[]', status: 'DRAFT' } }, owners: { create: { userId: challenge.userId } } } })).id;
  const pending = await prisma.telegramChannel.upsert({ where: { creatorId }, create: { creatorId, telegramChatId: challenge.telegramChatId, title: chat.title ?? shared.title, username: chat.username ?? shared.username, status: 'PENDING', importMode: 'AUTO', connectedAt: null, disconnectedAt: null, lastError: state === 'WAITING_FOR_BOT' ? 'Add GPoint bot to this channel to continue.' : 'Grant GPoint bot permission to post messages to continue.' }, update: { telegramChatId: challenge.telegramChatId, title: chat.title ?? shared.title, username: chat.username ?? shared.username, status: 'PENDING', connectedAt: null, disconnectedAt: null, lastError: state === 'WAITING_FOR_BOT' ? 'Add GPoint bot to this channel to continue.' : 'Grant GPoint bot permission to post messages to continue.' } });
  return { status: state, channel: publicChannel(pending), instructions: state === 'WAITING_FOR_BOT' ? 'Add GPoint bot to the selected channel, then tap Continue.' : 'Grant GPoint bot permission to post messages, then tap Continue.' };
}
async function completeChannelSelection(challenge: any, senderId: string, chatId: string, shared: { title?: string; username?: string }) {
  const chat = await telegramApi<{ id: number; type: string; title?: string; username?: string; description?: string }>('getChat', { chat_id: chatId });
  if (chat.type !== 'channel' || chat.id.toString() !== chatId) throw new BadRequestException('selected chat is not a channel');
  const member = await telegramApi<{ status: string; can_manage_chat?: boolean; can_post_messages?: boolean }>('getChatMember', { chat_id: chatId, user_id: senderId });
  if (!userIsChannelAdmin(member)) throw new UnauthorizedException('selected Telegram user is not a channel administrator');
  const currentOwner = await prisma.creatorOwner.findFirst({ where: { userId: challenge.userId }, select: { creatorId: true } });
  const existing = await prisma.telegramChannel.findUnique({ where: { telegramChatId: chatId } });
  if (existing && (!currentOwner || existing.creatorId !== currentOwner.creatorId)) throw new BadRequestException('channel is already connected to another creator');
  const bot = await telegramApi<{ id: number }>('getMe', {});
  const botMember = await optionalBotMember(chatId, bot.id);
  challenge.telegramChatId = chatId;
  if (!botMember) { await prisma.telegramConnectionChallenge.update({ where: { id: challenge.id }, data: { telegramChatId: chatId } }); return ensurePendingChannel(challenge, chat, shared, 'WAITING_FOR_BOT'); }
  if (!botCanPost(botMember)) { await prisma.telegramConnectionChallenge.update({ where: { id: challenge.id }, data: { telegramChatId: chatId } }); return ensurePendingChannel(challenge, chat, shared, 'WAITING_FOR_BOT_RIGHTS'); }
  const creatorId = currentOwner?.creatorId ?? (await prisma.creator.create({ data: { displayName: chat.title ?? shared.title ?? 'Telegram Creator', slug: await uniqueSlug(chat.username ?? chat.title ?? 'creator'), profile: { create: { bio: chat.description ?? '', tagsJson: '[]', status: 'DRAFT' } }, owners: { create: { userId: challenge.userId } } } })).id;
  const connected = await prisma.$transaction(async tx => { const completedAt = new Date(); await tx.telegramConnectionChallenge.update({ where: { id: challenge.id }, data: { usedAt: completedAt, telegramChatId: chatId } }); await tx.creatorChannelInstallAttempt.upsert({ where: { id: challenge.id }, create: { id: challenge.id, gpointUserId: challenge.userId, telegramUserId: BigInt(senderId), state: 'CONNECTED', expiresAt: challenge.expiresAt, completedAt }, update: { state: 'CONNECTED', completedAt } }); return tx.telegramChannel.upsert({ where: { creatorId }, create: { creatorId, telegramChatId: chatId, title: chat.title ?? shared.title, username: chat.username ?? shared.username, status: 'CONNECTED', importMode: 'AUTO', connectedAt: completedAt, disconnectedAt: null, lastError: null }, update: { telegramChatId: chatId, title: chat.title ?? shared.title, username: chat.username ?? shared.username, status: 'CONNECTED', connectedAt: completedAt, disconnectedAt: null, lastError: null } }); });
  try { await telegramApi('sendMessage', { chat_id: senderId, text: `✅ ${chat.title ?? shared.title ?? 'Telegram channel'} connected to GPoint.`, ...(process.env.PUBLIC_APP_URL ? { reply_markup: { inline_keyboard: [[{ text: 'Open GPoint', web_app: { url: process.env.PUBLIC_APP_URL } }]] } } : {}) }); } catch (error) { console.warn('[GPoint] creator connection confirmation message unavailable', { reason: error instanceof Error ? error.message : 'UNKNOWN' }); }
  return { status: 'CONNECTED', channel: publicChannel(connected) };
}

async function reconcileCreatorChannelConnection(challenge: any, senderId: string, chatId: string, shared: { title?: string; username?: string; description?: string }) {
  const chat = await telegramApi<{ id: number; type: string; title?: string; username?: string; description?: string }>('getChat', { chat_id: chatId });
  if (chat.type !== 'channel' || chat.id.toString() !== chatId) throw new BadRequestException('selected chat is not a channel');
  const userMember = await telegramApi<{ status: string; can_manage_chat?: boolean; can_post_messages?: boolean }>('getChatMember', { chat_id: chatId, user_id: senderId });
  if (userMember.status !== 'creator' && userMember.status !== 'administrator') throw new UnauthorizedException('selected Telegram user is not a channel administrator');
  const currentOwner = await prisma.creatorOwner.findFirst({ where: { userId: challenge.userId }, select: { creatorId: true } });
  const existing = await prisma.telegramChannel.findUnique({ where: { telegramChatId: chatId } });
  if (existing && (!currentOwner || existing.creatorId !== currentOwner.creatorId)) throw new BadRequestException('channel is already connected to another creator');
  const bot = await telegramApi<{ id: number }>('getMe', {});
  const botMember = await optionalBotMember(chatId, bot.id);
  await prisma.telegramConnectionChallenge.update({ where: { id: challenge.id }, data: { telegramChatId: chatId } });
  challenge.telegramChatId = chatId;
  if (!botMember) return ensurePendingChannel(challenge, chat, shared, 'WAITING_FOR_BOT');
  if (!botCanPost(botMember)) return ensurePendingChannel(challenge, chat, shared, 'WAITING_FOR_BOT_RIGHTS');
  const creatorId = currentOwner?.creatorId ?? (await prisma.creator.create({ data: { displayName: chat.title ?? shared.title ?? 'Telegram Creator', slug: await uniqueSlug(chat.username ?? chat.title ?? 'creator'), profile: { create: { bio: chat.description ?? shared.description ?? '', tagsJson: '[]', status: 'DRAFT' } }, owners: { create: { userId: challenge.userId } } } })).id;
  const connected = await prisma.$transaction(async tx => { await tx.telegramConnectionChallenge.update({ where: { id: challenge.id }, data: { usedAt: new Date(), telegramChatId: chatId } }); return tx.telegramChannel.upsert({ where: { creatorId }, create: { creatorId, telegramChatId: chatId, title: chat.title ?? shared.title, username: chat.username ?? shared.username, status: 'CONNECTED', importMode: 'AUTO', connectedAt: new Date(), disconnectedAt: null, lastError: null }, update: { telegramChatId: chatId, title: chat.title ?? shared.title, username: chat.username ?? shared.username, status: 'CONNECTED', connectedAt: new Date(), disconnectedAt: null, lastError: null } }); });
  return { status: 'CONNECTED', channel: publicChannel(connected) };
}

@Controller('creator/channel')
export class TelegramConnectionController {
  @Post('start')
  async startChannel(@Headers() h: Record<string, string | undefined>) {
    const { userId: id, user } = await authenticated(h); if (user.telegramUserId <= 0n) throw new BadRequestException('A real Telegram account is required for channel selection');
    const active = await prisma.telegramConnectionChallenge.findFirst({ where: { userId: id, preparedButtonId: null, usedAt: null, telegramChatId: null, expiresAt: { gt: new Date() } }, orderBy: { createdAt: 'desc' } });
    const challenge = active ?? await prisma.telegramConnectionChallenge.create({ data: { requestId: randomInt(1, 2_147_483_647), userId: id, expiresAt: new Date(Date.now() + challengeLifetimeMs) } });
    await prisma.creatorChannelInstallAttempt.upsert({ where: { id: challenge.id }, create: { id: challenge.id, gpointUserId: id, telegramUserId: user.telegramUserId, expiresAt: challenge.expiresAt }, update: { telegramUserId: user.telegramUserId, state: 'WAITING_FOR_TELEGRAM', expiresAt: challenge.expiresAt, completedAt: null } });
    const evidence = await prisma.telegramChannelInstallEvidence.findFirst({ where: { attemptId: null, actorTelegramUserId: user.telegramUserId, newBotStatus: 'administrator', receivedAt: { gt: new Date(Date.now() - challengeLifetimeMs) } }, orderBy: { receivedAt: 'desc' } });
    if (evidence) {
      await prisma.$transaction([prisma.telegramChannelInstallEvidence.update({ where: { id: evidence.id }, data: { attemptId: challenge.id } }), prisma.telegramConnectionChallenge.update({ where: { id: challenge.id }, data: { telegramChatId: evidence.telegramChatId } })]);
      const recovered = await reconcileCreatorChannelConnection({ ...challenge, telegramChatId: evidence.telegramChatId }, user.telegramUserId.toString(), evidence.telegramChatId, { title: evidence.chatTitle ?? undefined });
      if (recovered.status === 'CONNECTED') return { challengeId: challenge.id, expiresAt: challenge.expiresAt, status: 'CONNECTED', channel: recovered.channel };
    }
    return { challengeId: challenge.id, expiresAt: challenge.expiresAt, status: 'WAITING_FOR_CHANNEL' };
  }

  @Get('start/:challengeId')
  async startChannelStatus(@Headers() h: Record<string, string | undefined>, @Param('challengeId') challengeId: string) {
    const { userId: id, user } = await authenticated(h); const challenge = await prisma.telegramConnectionChallenge.findUnique({ where: { id: challengeId } });
    if (!challenge || challenge.userId !== id || challenge.preparedButtonId) throw new UnauthorizedException();
    if (challenge.usedAt) return { status: 'CONNECTED' }; if (challenge.expiresAt.getTime() <= Date.now() && !challenge.telegramChatId) return { status: 'FAILED', message: 'Telegram channel connection expired.' }; if (!challenge.telegramChatId) return { status: 'WAITING_FOR_CHANNEL' };
    try {
      const result = await reconcileCreatorChannelConnection(challenge, user.telegramUserId.toString(), challenge.telegramChatId, {});
      return result.status === 'CONNECTED' ? result : { status: 'VERIFYING', channel: result.channel ?? null, message: 'Verifying Telegram channel access.' };
    } catch (error) {
      console.error('[GPoint] creator channel reconciliation failed', { reason: error instanceof Error ? error.message : 'UNKNOWN' });
      return { status: 'VERIFYING', message: 'Could not verify the connection yet.' };
    }
  }

  @Post('reconcile')
  async reconcile(@Headers() h: Record<string, string | undefined>, @Body() body: { challengeId?: string }) {
    const { userId: id, user } = await authenticated(h); if (!body.challengeId) throw new BadRequestException('challengeId required');
    const challenge = await prisma.telegramConnectionChallenge.findUnique({ where: { id: body.challengeId } });
    if (!challenge || challenge.userId !== id || challenge.preparedButtonId || !challenge.telegramChatId) return { status: 'WAITING_FOR_CHANNEL' };
    if (challenge.usedAt) return { status: 'CONNECTED' };
    try { return await reconcileCreatorChannelConnection(challenge, user.telegramUserId.toString(), challenge.telegramChatId, {}); }
    catch (error) { console.error('[GPoint] creator channel reconciliation failed', { reason: error instanceof Error ? error.message : 'UNKNOWN' }); return { status: 'FAILED', message: 'Could not verify the connection yet.' }; }
  }

  @Post('selection/prepare')
  async prepare(@Headers() h: Record<string, string | undefined>) {
    const startedAt = Date.now(); console.info('[GPoint] channel prepare handler started', { startedAt: new Date(startedAt).toISOString() });
    const { userId: id, user } = await authenticated(h); if (user.telegramUserId <= 0n) throw new BadRequestException('A real Telegram account is required for channel selection');
    const requestId = randomInt(1, 2_147_483_647); const button = preparedChannelButton(requestId);
    const prepared = preparedButtonId(await telegramApi<{ id?: unknown }>('savePreparedKeyboardButton', { user_id: user.telegramUserId.toString(), button }));
    const challenge = await prisma.telegramConnectionChallenge.create({ data: { requestId, preparedButtonId: prepared, userId: id, expiresAt: new Date(Date.now() + challengeLifetimeMs) } });
    console.info('[GPoint] channel prepare response sent', { finishedAt: new Date().toISOString(), latencyMs: Date.now() - startedAt, status: 201, preparedRequestIdPresent: true });
    return { challengeId: challenge.id, requestId: prepared, expiresAt: challenge.expiresAt, supported: true };
  }

  @Get('selection/:challengeId')
  async selectionStatus(@Headers() h: Record<string, string | undefined>, @Param('challengeId') challengeId: string) {
    const { userId: id } = await authenticated(h); const challenge = await prisma.telegramConnectionChallenge.findUnique({ where: { id: challengeId } });
    if (!challenge || challenge.userId !== id) throw new UnauthorizedException();
    if (challenge.expiresAt.getTime() <= Date.now() && !challenge.usedAt) return { status: 'FAILED', message: 'Channel selection expired. Tap Become a Creator to try again.' };
    if (challenge.usedAt) return { status: 'CONNECTED' };
    if (!challenge.telegramChatId) return { status: 'READY' };
    const channel = await prisma.telegramChannel.findFirst({ where: { telegramChatId: challenge.telegramChatId, creator: { owners: { some: { userId: id } } } } });
    const status = channel?.lastError?.includes('permission') ? 'WAITING_FOR_BOT_RIGHTS' : 'WAITING_FOR_BOT';
    return { status, channel: publicChannel(channel), message: channel?.lastError ?? 'Add GPoint bot to continue.' };
  }

  @Post('selection/complete')
  async complete(@Headers() h: Record<string, string | undefined>, @Body() body: { challengeId?: string }) {
    const { userId: id, user } = await authenticated(h); if (!body.challengeId) throw new BadRequestException('challengeId required');
    const challenge = await prisma.telegramConnectionChallenge.findUnique({ where: { id: body.challengeId } });
    if (!challenge || challenge.userId !== id || challenge.usedAt || challenge.expiresAt.getTime() <= Date.now()) throw new UnauthorizedException('challenge expired, used or unknown');
    if (!challenge.telegramChatId) throw new BadRequestException('select a channel first');
    return completeChannelSelection(challenge, user.telegramUserId.toString(), challenge.telegramChatId, {});
  }

  @Post('connect/start')
  async start(@Headers() h: Record<string, string | undefined>) {
    const { userId: id } = await owned(h); const user = await prisma.user.findUnique({ where: { id }, select: { telegramUserId: true } });
    if (!user || user.telegramUserId <= 0n) throw new BadRequestException('A real Telegram account is required for channel selection');
    const requestId = randomInt(1, 2_147_483_647); const challenge = await prisma.telegramConnectionChallenge.create({ data: { requestId, userId: id, expiresAt: new Date(Date.now() + challengeLifetimeMs) } });
    const selector = preparedChannelButton(requestId);
    const keyboard = { keyboard: [[selector]], resize_keyboard: true, one_time_keyboard: true };
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
  @Post('my-chat-member')
  async myChatMember(@Headers() headers: Record<string, string | undefined>, @Body() body: { updateId?: number; from?: { id?: number | string }; chat?: { id?: number | string; type?: string; title?: string; username?: string }; old_chat_member?: { status?: string }; new_chat_member?: { status?: string; can_post_messages?: boolean; [key: string]: unknown } }) {
    const expected = process.env.TELEGRAM_WEBHOOK_SECRET; if (!expected || headers['x-telegram-webhook-secret'] !== expected) throw new UnauthorizedException();
    const senderId = body.from?.id?.toString(); const chat = body.chat; const next = body.new_chat_member;
    console.info('[GPoint] my_chat_member handler started', { chatType: chat?.type ?? null, newStatus: next?.status ?? null, actorPresent: Boolean(senderId), chatPresent: Boolean(chat?.id) });
    if (!senderId || !chat?.id || chat.type !== 'channel' || !next?.status || body.updateId === undefined) throw new BadRequestException('channel membership evidence required');
    if (!/^\d+$/.test(senderId)) throw new UnauthorizedException('invalid Telegram actor');
    const chatId = chat.id.toString();
    const attempt = await prisma.creatorChannelInstallAttempt.findFirst({ where: { telegramUserId: BigInt(senderId), state: 'WAITING_FOR_TELEGRAM', expiresAt: { gt: new Date() } }, orderBy: { createdAt: 'desc' } });
    const evidence = await prisma.telegramChannelInstallEvidence.upsert({ where: { telegramUpdateId: body.updateId }, create: { telegramUpdateId: body.updateId, attemptId: attempt?.id, telegramChatId: chatId, chatType: chat.type, chatTitle: chat.title, actorTelegramUserId: BigInt(senderId), oldBotStatus: body.old_chat_member?.status, newBotStatus: next.status, botRightsSnapshot: next as any, eventDate: null }, update: {} });
    console.info('[GPoint] my_chat_member evidence persisted', { duplicate: evidence.attemptId !== attempt?.id, state: attempt ? 'MATCHED' : 'UNCLAIMED' });
    if (!attempt) return { status: 'UNCLAIMED', evidenceId: evidence.id };
    const challenge = await prisma.telegramConnectionChallenge.findUnique({ where: { id: attempt.id } });
    if (!challenge || challenge.preparedButtonId || challenge.usedAt || challenge.expiresAt.getTime() <= Date.now() || (challenge.telegramChatId && challenge.telegramChatId !== chatId)) return { status: 'UNCLAIMED', evidenceId: evidence.id };
    console.info('[GPoint] my_chat_member pending connection found', { state: challenge.telegramChatId ? 'BOUND' : 'WAITING_FOR_CHANNEL' });
    try {
      const result = await reconcileCreatorChannelConnection(challenge, senderId, chatId, chat);
      console.info('[GPoint] my_chat_member finalization complete', { status: result.status });
      return result;
    } catch (error) {
      console.error('[GPoint] my_chat_member finalization failed', { reason: error instanceof Error ? error.message : 'UNKNOWN' });
      throw error;
    }
  }

  @Post('chat-shared')
  async chatShared(@Headers() headers: Record<string, string | undefined>, @Body() body: { from?: { id?: number | string }; chat_shared?: { request_id?: number; chat_id?: number | string; title?: string; username?: string } }) {
    const expected = process.env.TELEGRAM_WEBHOOK_SECRET; if (!expected || headers['x-telegram-webhook-secret'] !== expected) throw new UnauthorizedException();
    const senderId = body.from?.id?.toString(); const shared = body.chat_shared; if (!senderId || !shared?.request_id || shared.chat_id === undefined) throw new BadRequestException('chat_shared data required');
    const challenge = await prisma.telegramConnectionChallenge.findUnique({ where: { requestId: shared.request_id }, include: { user: true } });
    if (!challenge || challenge.usedAt || challenge.expiresAt.getTime() <= Date.now()) throw new UnauthorizedException('challenge expired, used or unknown');
    if (challenge.user.telegramUserId.toString() !== senderId) throw new UnauthorizedException('challenge belongs to another Telegram user');
    const chatId = shared.chat_id.toString(); return completeChannelSelection(challenge, senderId, chatId, shared);
  }
}
