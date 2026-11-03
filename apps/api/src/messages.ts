import { BadRequestException, Body, Controller, Get, Headers, HttpException, NotFoundException, Param, Post, Query, Req, UnauthorizedException } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { publicCreator } from './core-v2.js';
import { FixedWindowRateLimiter } from './rate-limit.js';
import { resolveSession } from './session.js';
import { connectedTelegramChannelAvatarUrls } from './telegram-channel-avatars.js';
import { isAllowedCodexMessageRequest, isCodexOwnerAllowed, parseCodexOwnerUserIds, resolveCodexMessageSession } from './codex-chat-auth.js';
import { decodeErosCursor, encodeErosCursor, ErosAdapterError, createErosEngineClient, erosIdentityFor, erosTestCreatorIds, isErosChatAllowed, isSyntheticErosPair, prepareErosRelationship, markErosAssistantMessagesRead, erosInboxLastMessageSummary, unreadErosAssistantCounts, resolveErosRelationshipInstanceId, recordErosAssistantMessage, syncErosAssistantMessageStates, erosAssistantReadAtById } from './eros-adapter.js';
import { isGpointChatPermitted, loadGpointAgeAssurance, loadGpointChatState, recordGpointChatConsent, recordGpointSelfAttestation } from './gpoint-chat-store.js';

const prisma = new PrismaClient();
const sendLimiter = new FixedWindowRateLimiter(30, 60_000);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const EROS_MESSAGE_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
const CREATOR_INCLUDE = { profile: true, vipOffers: { where: { status: 'ACTIVE' }, take: 1 } } as const;
const erosRelationshipStore = {
  async getInstanceId(userId: string, creatorId: string): Promise<string | null> {
    const row = await prisma.erosRelationshipMapping.findUnique({
      where: { userId_creatorId: { userId, creatorId } },
      select: { instanceId: true },
    });
    return row?.instanceId ?? null;
  },
  async saveInstanceId(userId: string, creatorId: string, instanceId: string): Promise<void> {
    await prisma.erosRelationshipMapping.upsert({
      where: { userId_creatorId: { userId, creatorId } },
      create: { userId, creatorId, instanceId },
      update: { instanceId },
    });
  },
};

async function prepareErosChat(userId: string, creator: { id: string; displayName: string; profile?: { bio?: string | null; tagsJson?: string | null; erosGenomeId?: string | null } | null }) {
  const client = createErosEngineClient();
  if (!client) throw new ErosAdapterError('eros_disabled');
  return prepareErosRelationship(client, erosRelationshipStore, {
    userId,
    creator: { id: creator.id, displayName: creator.displayName, genomeId: creator.profile?.erosGenomeId },
  });
}

async function canRouteErosChat(userId: string, creator: { id: string; slug: string; profile?: { status?: string | null } | null }): Promise<boolean> {
  if (!isErosChatAllowed(userId, creator.id)) return false;
  const user = await prisma.user.findUnique({ where: { id: userId }, select: { telegramUsername: true } });
  if (!user || !isSyntheticErosPair(user, { slug: creator.slug, profileStatus: creator.profile?.status ?? '' })) return false;
  const state = await loadGpointChatState(prisma, userId);
  if (!isGpointChatPermitted(state)) return false;
  const legacy = await prisma.gpointLegacyConversationFence.findFirst({ where: { userId, creatorId: creator.id }, select: { id: true } });
  return !legacy;
}

async function findErosSession(userId: string, creatorId: string): Promise<{ userId: string; sessionId: string } | null> {
  const identity = erosIdentityFor({ userId, creatorId });
  const client = createErosEngineClient();
  if (!client) return null;
  const instanceId = await resolveErosRelationshipInstanceId(erosRelationshipStore, userId, creatorId);
  if (!instanceId) return null;
  const sessions = await client.sessions(identity.userId);
  const session = sessions
    .filter((item) => item.channel === 'text' && item.instanceId === instanceId)
    .sort((a, b) => Date.parse(b.lastActiveAt) - Date.parse(a.lastActiveAt))[0];
  return session ? { userId: identity.userId, sessionId: session.sessionId } : null;
}

async function localErosConversations(userId: string) {
  const creatorIds = erosTestCreatorIds().filter((creatorId) => isErosChatAllowed(userId, creatorId));
  if (!creatorIds.length) return [];
  const user = await prisma.user.findUnique({ where: { id: userId }, select: { telegramUsername: true } });
  if (!user) return [];
  const consent = await loadGpointChatState(prisma, userId);
  if (!isGpointChatPermitted(consent)) return [];
  const client = createErosEngineClient();
  if (!client) return [];
  const mappedUser = erosIdentityFor({ userId, creatorId: creatorIds[0] }).userId;
  const sessions = await client.sessions(mappedUser);
  const creators = (await prisma.creator.findMany({ where: { id: { in: creatorIds }, profile: { status: 'APPROVED' } }, include: CREATOR_INCLUDE }))
    .filter((creator) => isSyntheticErosPair(user, { slug: creator.slug, profileStatus: creator.profile?.status ?? '' }));
  const priorHistory = await prisma.gpointLegacyConversationFence.findMany({ where: { userId, creatorId: { in: creators.map((creator) => creator.id) } }, select: { creatorId: true } });
  const fenced = new Set(priorHistory.map((row) => row.creatorId));
  const routedCreators = creators.filter((creator) => !fenced.has(creator.id));
  const relationshipMappings = await prisma.erosRelationshipMapping.findMany({
    where: { userId, creatorId: { in: routedCreators.map((creator) => creator.id) } },
    select: { creatorId: true, instanceId: true },
  });
  const routedCreatorById = new Map(routedCreators.map((creator) => [creator.id, creator]));
  const creatorByInstance = new Map(relationshipMappings.flatMap((mapping) => {
    const creator = routedCreatorById.get(mapping.creatorId);
    return creator ? [[mapping.instanceId, creator] as const] : [];
  }));
  const matchingSessions = sessions.filter((session) => session.channel === 'text' && session.instanceId !== null && creatorByInstance.has(session.instanceId));
  const newestByCreator = new Map<string, { sessionId: string; lastActiveAt: string }>();
  for (const session of matchingSessions) {
    if (session.instanceId === null) continue;
    const creator = creatorByInstance.get(session.instanceId)!;
    const previous = newestByCreator.get(creator.id);
    if (!previous || Date.parse(session.lastActiveAt) > Date.parse(previous.lastActiveAt)) {
      newestByCreator.set(creator.id, { sessionId: session.sessionId, lastActiveAt: session.lastActiveAt });
    }
  }
  const selected = routedCreators.filter((creator) => newestByCreator.has(creator.id));
  const avatars = await connectedTelegramChannelAvatarUrls(selected.map((creator) => creator.id), prisma);
  const selectedSessions = selected.map((creator) => ({ creator, ...newestByCreator.get(creator.id)! }));
  const histories = await Promise.all(selectedSessions.map(async ({ creator, sessionId }) => {
    const identity = erosIdentityFor({ userId, creatorId: creator.id });
    const history = await client.history(identity.userId, sessionId, 50);
    await syncErosAssistantMessageStates(prisma, userId, creator.id, sessionId, history.messages);
    return { creator, sessionId, history };
  }));
  const unreadCounts = await unreadErosAssistantCounts(prisma, userId, selectedSessions.map(({ creator, sessionId }) => ({ creatorId: creator.id, sessionId })));
  return Promise.all(histories.map(async ({ creator, sessionId, history }) => {
    const latest = history.messages.at(-1);
    if (!latest) return null;
    const dto = await publicCreator(creator);
    const summary = erosInboxLastMessageSummary(latest);
    return {
      creator: messageThreadCreatorDto(dto, avatars.get(creator.id) ?? null),
      ...summary,
      lastMessageAt: latest.sent_at,
      unreadCount: unreadCounts.get(sessionId) ?? 0,
    };
  })).then((rows) => rows.filter((row) => row !== null));
}

function mapErosError(error: unknown): HttpException {
  if (error instanceof ErosAdapterError) {
    const status = error.status >= 400 && error.status < 500 ? error.status : 503;
    return new HttpException({ code: 'EROS_UNAVAILABLE', reason: error.code }, status);
  }
  return new HttpException({ code: 'EROS_UNAVAILABLE' }, 503);
}

async function requireErosConsent(userId: string): Promise<void> {
  const current = await loadGpointChatState(prisma, userId);
  if (current.ageAssuranceRequired) throw new HttpException({ code: 'AGE_ASSURANCE_REQUIRED' }, 403);
  if (!isGpointChatPermitted(current)) throw new HttpException({ code: 'CHAT_CONSENT_REQUIRED' }, 403);
}

export function messageThreadCreatorDto(creator: { id: string; slug: string; displayName: string; verified: boolean; avatarUrl?: string | null }, channelAvatarUrl: string | null = null) {
  return { id: creator.id, slug: creator.slug, displayName: creator.displayName, avatarUrl: channelAvatarUrl, verified: creator.verified };
}

export function validateReadBatch(body: unknown, allowErosMessageIds = false): string[] {
  if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).some(key => key !== 'messageIds')) throw new BadRequestException({ code: 'invalid_read_batch' });
  const ids = (body as { messageIds?: unknown }).messageIds;
  if (!Array.isArray(ids) || ids.length < 1 || ids.length > 100 || ids.some(id => typeof id !== 'string' ||
      (!UUID_PATTERN.test(id) && !(allowErosMessageIds && EROS_MESSAGE_ID_PATTERN.test(id))))) throw new BadRequestException({ code: 'invalid_read_batch' });
  return [...new Set(ids.map((id: string) => UUID_PATTERN.test(id) ? id.toLowerCase() : id))];
}

export function publicUnreadSummary(rows: Array<{ gpointCharacterId: string; unreadCount: number }>, eligible: ReadonlySet<string>) {
  const items = rows.filter(row => eligible.has(row.gpointCharacterId)).map(row => {
    if (!Number.isSafeInteger(row.unreadCount) || row.unreadCount < 0) throw new HttpException({ code: 'MESSAGES_TEMPORARILY_UNAVAILABLE' }, 503);
    return { creatorId: row.gpointCharacterId, unreadCount: row.unreadCount };
  });
  const totalUnread = items.reduce((sum, row) => sum + row.unreadCount, 0);
  if (!Number.isSafeInteger(totalUnread)) throw new HttpException({ code: 'MESSAGES_TEMPORARILY_UNAVAILABLE' }, 503);
  return { totalUnread, items };
}

function authenticatedUser(headers: Record<string, string | undefined>): string {
  const id = resolveSession(headers.authorization?.replace(/^Bearer\s+/i, ''));
  if (!id) throw new UnauthorizedException();
  return id;
}

async function authenticatedMessageUser(headers: Record<string, string | undefined>, creatorId: string, action: 'read' | 'send', method: string, path: string): Promise<string> {
  const token = headers.authorization?.replace(/^Bearer\s+/i, '');
  const sessionUserId = resolveSession(token);
  if (sessionUserId) return sessionUserId;
  if (!isAllowedCodexMessageRequest(method, path, creatorId)) throw new UnauthorizedException();
  const ownerUserIds = parseCodexOwnerUserIds(process.env.GPOINT_CODEX_OWNER_USER_IDS);
  const scoped = await resolveCodexMessageSession(token, creatorId, action, {
    now: Date.now,
    ownerUserIds,
    findByAccessTokenHash: async hash => {
      const row = await prisma.codexChatConnection.findUnique({ where: { accessTokenHash: hash } });
      return row ? { id: row.id, userId: row.userId, creatorId: row.creatorId, accessTokenHash: row.accessTokenHash, accessExpiresAt: row.accessExpiresAt, revokedAt: row.revokedAt } : null;
    },
    isCurrentCreatorOwner: async (userId, currentCreatorId) => Boolean(await prisma.creatorOwner.findFirst({ where: { userId, creatorId: currentCreatorId }, select: { creatorId: true } })),
  });
  if (scoped && isCodexOwnerAllowed(scoped.userId, ownerUserIds)) return scoped.userId;
  throw new UnauthorizedException();
}

async function findApprovedCreator(id: string) {
  const creator = await prisma.creator.findFirst({
    where: { id, profile: { status: 'APPROVED' } },
    include: CREATOR_INCLUDE,
  });
  if (!creator) throw new NotFoundException({ code: 'CREATOR_UNAVAILABLE' });
  return creator;
}

function localeFor(value: string | null): 'ru-RU' | 'en-US' | 'en-GB' {
  if (value?.toLowerCase() === 'en-gb') return 'en-GB';
  if (value?.toLowerCase() === 'en' || value?.toLowerCase() === 'en-us') return 'en-US';
  return value?.toLowerCase().startsWith('ru') ? 'ru-RU' : 'ru-RU';
}

async function recordChatEvent(userId: string, creatorId: string, eventType: string, dedupeKey?: string) {
  if (dedupeKey) {
    const existing = await prisma.productEvent.findFirst({
      where: { userId, creatorId, eventType, entityType: 'CREATOR', entityId: creatorId, metadataJson: { path: ['dedupeKey'], equals: dedupeKey } },
    });
    if (existing) return;
  }
  await prisma.productEvent.create({
    data: {
      userId, creatorId, eventType, entityType: 'CREATOR', entityId: creatorId,
      ...(dedupeKey ? { metadataJson: { dedupeKey } } : {}),
    },
  });
}

@Controller('messages')
export class MessagesController {
  @Get('unread')
  async unread(@Headers() headers: Record<string, string | undefined>) {
    const userId = authenticatedUser(headers);
    try {
      const conversations = await localErosConversations(userId);
      return publicUnreadSummary(conversations.map((item) => ({ gpointCharacterId: item.creator.id, unreadCount: item.unreadCount })), new Set(conversations.map((item) => item.creator.id)));
    } catch (error) { if (error instanceof HttpException) throw error; throw mapErosError(error); }
  }

  @Post('creator/:creatorId/read')
  async read(@Param('creatorId') creatorId: string, @Headers() headers: Record<string, string | undefined>, @Body() body: unknown) {
    const userId = authenticatedUser(headers);
    const ids = validateReadBatch(body, true);
    try {
      const creator = await findApprovedCreator(creatorId);
      await requireErosConsent(userId);
      if (!await canRouteErosChat(userId, creator)) throw new HttpException({ code: 'CHAT_UNAVAILABLE' }, 403);
      const session = await findErosSession(userId, creatorId);
      if (!session) return { acknowledgedMessageIds: [] };
      return { acknowledgedMessageIds: await markErosAssistantMessagesRead(prisma, userId, creatorId, session.sessionId, ids) };
    } catch (error) { if (error instanceof HttpException) throw error; throw mapErosError(error); }
  }

  @Get('state')
  async state(@Headers() headers: Record<string, string | undefined>) {
    return loadGpointChatState(prisma, authenticatedUser(headers));
  }

  @Post('consent')
  async consent(@Headers() headers: Record<string, string | undefined>, @Body() body: { chatHistory?: unknown; longTermMemory?: unknown; disclosureVersion?: unknown }) {
    const userId = authenticatedUser(headers);
    return recordGpointChatConsent(prisma, userId, body ?? {});
  }

  @Post('consent/revoke')
  async revokeConsent(@Headers() headers: Record<string, string | undefined>, @Body() body: { purpose?: unknown }) {
    const userId = authenticatedUser(headers);
    if (body?.purpose !== 'chat_history' && body?.purpose !== 'long_term_memory') throw new BadRequestException({ code: 'invalid_consent_purpose' });
    await prisma.gpointChatConsent.updateMany({ where: { userId, purpose: body.purpose, revokedAt: null }, data: { revokedAt: new Date() } });
    return loadGpointChatState(prisma, userId);
  }

  @Get()
  async inbox(@Headers() headers: Record<string, string | undefined>) {
    const userId = authenticatedUser(headers);
    try { return { items: await localErosConversations(userId), nextCursor: null }; }
    catch (error) { if (error instanceof HttpException) throw error; throw mapErosError(error); }
  }

  @Get('creator/:creatorId')
  async history(
    @Param('creatorId') creatorId: string,
    @Headers() headers: Record<string, string | undefined>,
    @Query('cursor') cursor?: string,
    @Query('limit') limitValue?: string,
    @Req() request?: { method?: string; url?: string },
  ) {
    const userId = await authenticatedMessageUser(headers, creatorId, 'read', request?.method ?? 'GET', request?.url ?? `/messages/creator/${encodeURIComponent(creatorId)}`);
    const limit = limitValue === undefined ? 50 : Number(limitValue);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new BadRequestException({ code: 'invalid_limit' });
    try {
      const creator = await findApprovedCreator(creatorId);
      await requireErosConsent(userId);
      if (!await canRouteErosChat(userId, creator)) throw new HttpException({ code: 'CHAT_UNAVAILABLE' }, 403);
      const dto = await publicCreator(creator);
      const session = await findErosSession(userId, creatorId);
      if (!session) return { creator: dto, items: [], nextCursor: null };
      const client = createErosEngineClient();
      if (!client) throw new ErosAdapterError('eros_disabled');
      const history = await client.history(session.userId, session.sessionId, limit, decodeErosCursor(cursor));
      await syncErosAssistantMessageStates(prisma, userId, creatorId, session.sessionId, history.messages);
      const readAtById = await erosAssistantReadAtById(prisma, userId, creatorId, session.sessionId, history.messages.filter((item) => item.role === 'assistant').map((item) => item.id));
      const pageSize = Math.min(limit, 50);
      await recordChatEvent(userId, creatorId, 'CHAT_OPEN', 'chat-open-' + creatorId + '-' + Math.floor(Date.now() / (30 * 60_000)));
      return {
        creator: dto,
        items: history.messages.filter(item => ['user', 'assistant', 'system_error'].includes(item.role)).map(item => ({
          id: item.id, role: item.role === 'system_error' ? 'system' : item.role,
          text: item.role === 'system_error' ? 'Сообщение не удалось обработать. Попробуйте ещё раз.' : item.content,
          ...(item.role === 'user' && item.client_msg_id ? { clientMessageId: item.client_msg_id } : {}),
          occurredAt: item.sent_at, incomingReadAt: item.role === 'assistant' ? readAtById.get(item.id) ?? null : null,
        })),
        nextCursor: history.messages.length === pageSize ? encodeErosCursor(history.messages[0].id) : null,
      };
    } catch (error) { if (error instanceof HttpException) throw error; throw mapErosError(error); }
  }

  @Post('creator/:creatorId')
  async send(
    @Param('creatorId') creatorId: string,
    @Headers() headers: Record<string, string | undefined>,
    @Body() body: { clientMessageId?: unknown; text?: unknown },
    @Req() request?: { method?: string; url?: string },
  ) {
    const userId = await authenticatedMessageUser(headers, creatorId, 'send', request?.method ?? 'POST', request?.url ?? `/messages/creator/${encodeURIComponent(creatorId)}`);
    if (typeof body?.clientMessageId !== 'string' || !UUID_PATTERN.test(body.clientMessageId) || typeof body.text !== 'string') throw new BadRequestException({ code: 'invalid_message' });
    const text = body.text.trim();
    if (!text || text.length > 4000) throw new BadRequestException({ code: 'invalid_message' });
    if (!sendLimiter.allow(`messages:${userId}`)) throw new HttpException({ code: 'RATE_LIMITED' }, 429);
    try {
      const creator = await findApprovedCreator(creatorId);
      await requireErosConsent(userId);
      if (!await canRouteErosChat(userId, creator)) throw new HttpException({ code: 'CHAT_UNAVAILABLE' }, 403);
      const { identity, session } = await prepareErosChat(userId, creator);
      const eros = createErosEngineClient();
      if (!eros) throw new ErosAdapterError('eros_disabled');
      const response = await eros.sendMessage(identity.userId, session.sessionId, body.clientMessageId, text, {
        creatorId: identity.creatorId, instanceId: identity.relationshipId,
      });
      await recordErosAssistantMessage(prisma, userId, creatorId, session.sessionId, response.id);
      await recordChatEvent(userId, creatorId, 'CHAT_MESSAGE_SENT', body.clientMessageId);
      await recordChatEvent(userId, creatorId, 'CHAT_RESPONSE_RECEIVED', body.clientMessageId);
      return { status: 'completed' as const, userMessageId: body.clientMessageId,
        response: { id: response.id, role: 'assistant' as const, text: response.text, occurredAt: new Date().toISOString(), incomingReadAt: null } };
    } catch (error) { if (error instanceof HttpException) throw error; throw mapErosError(error); }
  }
}

@Controller('age-assurance')
export class AgeAssuranceController {
  @Get('state')
  async state(@Headers() headers: Record<string, string | undefined>) {
    return loadGpointAgeAssurance(prisma, authenticatedUser(headers));
  }

  @Post('self-attest')
  async selfAttest(@Headers() headers: Record<string, string | undefined>, @Body() body: { confirmed18Plus?: unknown }) {
    return recordGpointSelfAttestation(prisma, authenticatedUser(headers), body?.confirmed18Plus);
  }
}
