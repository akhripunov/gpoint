import { randomUUID } from 'node:crypto';
import {
  AgataServiceError,
  type AgataChatState,
  type AgataCharacterProfile,
  type AgataConsentSnapshot,
} from '@gpoint/agata-client';
import { BadRequestException, Body, Controller, Get, Headers, HttpException, NotFoundException, Param, Post, Query, Req, UnauthorizedException } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { createAgataIntegration } from './agata-integration.js';
import { publicCreator } from './core-v2.js';
import { FixedWindowRateLimiter } from './rate-limit.js';
import { resolveSession } from './session.js';
import { connectedTelegramChannelAvatarUrls } from './telegram-channel-avatars.js';
import { isAllowedCodexMessageRequest, isCodexOwnerAllowed, parseCodexOwnerUserIds, resolveCodexMessageSession } from './codex-chat-auth.js';
import { decodeErosCursor, encodeErosCursor, ErosAdapterError, createErosEngineClient, erosIdentityFor, erosTestCreatorIds, isErosChatAllowed, isSyntheticErosPair, prepareErosRelationship, markErosAssistantMessagesRead, mergeErosInboxItems, erosInboxLastMessageSummary, unreadErosAssistantCounts, resolveErosRelationshipInstanceId } from './eros-adapter.js';

const prisma = new PrismaClient();
const sendLimiter = new FixedWindowRateLimiter(30, 60_000);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CREATOR_INCLUDE = { profile: true, vipOffers: { where: { status: 'ACTIVE' }, take: 1 } } as const;
let erosAdapterPrisma: PrismaClient | undefined;

function erosAdapterDatabase(): PrismaClient {
  const url = process.env.EROS_ADAPTER_DATABASE_URL;
  if (!url) throw new ErosAdapterError('eros_adapter_database_unconfigured');
  if (!erosAdapterPrisma) erosAdapterPrisma = new PrismaClient({ datasources: { db: { url } } });
  return erosAdapterPrisma;
}

function erosTags(tagsJson: string | null | undefined): string[] {
  if (!tagsJson) return [];
  try {
    const tags: unknown = JSON.parse(tagsJson);
    return Array.isArray(tags) ? tags.filter((tag): tag is string => typeof tag === 'string').slice(0, 20) : [];
  } catch { return []; }
}

async function prepareErosChat(userId: string, creator: { id: string; displayName: string; profile?: { bio?: string | null; tagsJson?: string | null } | null }) {
  const client = createErosEngineClient();
  if (!client) throw new ErosAdapterError('eros_disabled');
  return prepareErosRelationship(erosAdapterDatabase(), client, {
    userId,
    creator: { id: creator.id, displayName: creator.displayName, bio: creator.profile?.bio, tags: erosTags(creator.profile?.tagsJson) },
  });
}

async function canRouteErosChat(userId: string, creator: { id: string; slug: string; profile?: { status?: string | null } | null }, agata: ReturnType<typeof agataOrUnavailable>): Promise<boolean> {
  if (!isErosChatAllowed(userId, creator.id)) return false;
  const user = await prisma.user.findUnique({ where: { id: userId }, select: { telegramUsername: true } });
  if (!user || !isSyntheticErosPair(user, { slug: creator.slug, profileStatus: creator.profile?.status ?? '' })) return false;
  const priorAgataHistory = await agata.listMessages(userId, creator.id, { limit: 1 });
  return priorAgataHistory.items.length === 0;
}

async function findErosSession(userId: string, creatorId: string): Promise<{ userId: string; sessionId: string } | null> {
  const identity = erosIdentityFor({ userId, creatorId });
  const client = createErosEngineClient();
  if (!client) return null;
  const instanceId = await resolveErosRelationshipInstanceId(erosAdapterDatabase(), userId, creatorId);
  if (!instanceId) return null;
  const sessions = await client.sessions(identity.userId);
  const session = sessions
    .filter((item) => item.channel === 'text' && item.instanceId === instanceId)
    .sort((a, b) => Date.parse(b.lastActiveAt) - Date.parse(a.lastActiveAt))[0];
  return session ? { userId: identity.userId, sessionId: session.sessionId } : null;
}

async function localErosConversations(userId: string, agata: ReturnType<typeof agataOrUnavailable>) {
  const creatorIds = erosTestCreatorIds().filter((creatorId) => isErosChatAllowed(userId, creatorId));
  if (!creatorIds.length) return [];
  const user = await prisma.user.findUnique({ where: { id: userId }, select: { telegramUsername: true } });
  if (!user) return [];
  const consent = await agata.chatState(userId);
  assertMessageAgeAssurance(consent);
  if (!consent.chatHistory || consent.disclosureRequired) return [];
  const client = createErosEngineClient();
  if (!client) return [];
  const mappedUser = erosIdentityFor({ userId, creatorId: creatorIds[0] }).userId;
  const sessions = await client.sessions(mappedUser);
  const creators = (await prisma.creator.findMany({ where: { id: { in: creatorIds }, profile: { status: 'APPROVED' } }, include: CREATOR_INCLUDE }))
    .filter((creator) => isSyntheticErosPair(user, { slug: creator.slug, profileStatus: creator.profile?.status ?? '' }));
  const noPriorAgataHistory = await Promise.all(creators.map(async (creator) => {
    const history = await agata.listMessages(userId, creator.id, { limit: 1 });
    return history.items.length === 0 ? creator : null;
  }));
  const routedCreators = noPriorAgataHistory.filter((creator) => creator !== null);
  const creatorInstancePairs = await Promise.all(routedCreators.map(async (creator) => {
    const instanceId = await resolveErosRelationshipInstanceId(erosAdapterDatabase(), userId, creator.id);
    return instanceId ? [instanceId, creator] as const : null;
  }));
  const creatorByInstance = new Map(creatorInstancePairs.filter((pair) => pair !== null));
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
  const unreadCounts = await unreadErosAssistantCounts(erosAdapterDatabase(), mappedUser, selectedSessions.map((session) => session.sessionId));
  return Promise.all(selectedSessions.map(async ({ creator, sessionId, lastActiveAt }) => {
    const identity = erosIdentityFor({ userId, creatorId: creator.id });
    const history = await client.history(identity.userId, sessionId, 50);
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

async function requireErosConsent(agata: ReturnType<typeof agataOrUnavailable>, userId: string): Promise<void> {
  const current = await agata.chatState(userId);
  assertMessageAgeAssurance(current);
  if (!current.chatHistory || current.disclosureRequired) throw new HttpException({ code: 'CHAT_CONSENT_REQUIRED' }, 403);
}

export function messageThreadCreatorDto(creator: { id: string; slug: string; displayName: string; verified: boolean; avatarUrl?: string | null }, channelAvatarUrl: string | null = null) {
  return { id: creator.id, slug: creator.slug, displayName: creator.displayName, avatarUrl: channelAvatarUrl, verified: creator.verified };
}

export function buildAgataCharacterProfile(creator: { displayName: string; profile?: { bio?: string | null } | null }): AgataCharacterProfile {
  return {
    name: creator.displayName,
    personality: { identity_core: { source: 'gpoint_creator' } },
    speech_style: {},
    backstory: { public_bio: creator.profile?.bio ?? '' },
    boundaries: {},
  };
}

export function mapAgataError(error: unknown): HttpException {
  if (error instanceof AgataServiceError) {
    if (error.status === 404 && error.code === 'messages_not_found') return new NotFoundException({ code: 'CHAT_MESSAGES_NOT_FOUND' });
    if (error.status === 404 && error.code === 'character_unavailable') return new NotFoundException({ code: 'CHAT_CHARACTER_UNAVAILABLE' });
    if (error.status === 409 && error.code === 'conversation_closed') return new HttpException({ code: 'CHAT_CONVERSATION_CLOSED' }, 409);
    if (error.status === 409 && error.code === 'idempotency_conflict') return new HttpException({ code: 'CHAT_IDEMPOTENCY_CONFLICT' }, 409);
    if (error.status === 403 && ['consent_required', 'chat_history_consent_required'].includes(error.code)) return new HttpException({ code: 'CHAT_CONSENT_REQUIRED' }, 403);
    if (error.status === 503 && error.code === 'ai_unavailable') return new HttpException({ code: 'AI_UNAVAILABLE' }, 503);
    if (error.status === 503 && ['provider_unavailable', 'inference_retry_exhausted'].includes(error.code)) return new HttpException({ code: 'AI_UNAVAILABLE' }, 503);
  }
  return new HttpException({ code: 'MESSAGES_TEMPORARILY_UNAVAILABLE' }, 503);
}

export function assertMessageAgeAssurance(state: Pick<AgataChatState, 'ageAssuranceRequired'>): void {
  if (state.ageAssuranceRequired) throw new HttpException({ code: 'AGE_ASSURANCE_REQUIRED' }, 403);
}

function publicMessagesState(state: AgataChatState) {
  return {
    enabled: true,
    chatHistory: state.chatHistory,
    longTermMemory: state.longTermMemory,
    disclosureRequired: state.disclosureRequired,
    onboardingCompleted: state.onboardingCompleted,
    disclosureVersion: state.disclosureVersion,
    onboardingCompletedAt: state.onboardingCompletedAt,
    memoryOptIn: state.memoryOptIn,
    memoryOptInAt: state.memoryOptInAt,
    ageAssuranceRequired: state.ageAssuranceRequired,
    aiAvailable: process.env.AGATA_LLM_PROVIDER !== 'disabled' && process.env.AGATA_LLM_PROVIDER !== 'none',
  };
}

export function validateReadBatch(body: unknown): string[] {
  if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).some(key => key !== 'messageIds')) throw new BadRequestException({ code: 'invalid_read_batch' });
  const ids = (body as { messageIds?: unknown }).messageIds;
  if (!Array.isArray(ids) || ids.length < 1 || ids.length > 100 || ids.some(id => typeof id !== 'string' || !UUID_PATTERN.test(id))) throw new BadRequestException({ code: 'invalid_read_batch' });
  return [...new Set(ids.map((id: string) => id.toLowerCase()))];
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

function agataOrUnavailable() {
  const agata = createAgataIntegration();
  if (!agata) throw new HttpException({ code: 'MESSAGES_UNAVAILABLE' }, 503);
  return agata;
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
    const agata = createAgataIntegration();
    if (!agata) return { totalUnread: 0, items: [] };
    try {
      const summary = await agata.unreadMessages(userId);
      const ids = summary.items.map(item => item.gpointCharacterId);
      const eligible = ids.length ? await prisma.creator.findMany({ where: { id: { in: ids }, profile: { status: 'APPROVED' } }, select: { id: true } }) : [];
      const erosConversations = await localErosConversations(userId, agata);
      const merged = new Map<string, number>();
      for (const item of summary.items) merged.set(item.gpointCharacterId, (merged.get(item.gpointCharacterId) ?? 0) + item.unreadCount);
      for (const item of erosConversations) merged.set(item.creator.id, item.unreadCount);
      const rows = [...merged].map(([gpointCharacterId, unreadCount]) => ({ gpointCharacterId, unreadCount }));
      return publicUnreadSummary(rows, new Set([...eligible.map(creator => creator.id), ...erosConversations.map(item => item.creator.id)]));
    } catch (error) { if (error instanceof ErosAdapterError) throw mapErosError(error); throw mapAgataError(error); }
  }

  @Post('creator/:creatorId/read')
  async read(@Param('creatorId') creatorId: string, @Headers() headers: Record<string, string | undefined>, @Body() body: unknown) {
    const userId = authenticatedUser(headers);
    const ids = validateReadBatch(body);
    try {
      const creator = await findApprovedCreator(creatorId);
      const agata = agataOrUnavailable();
      if (isErosChatAllowed(userId, creatorId)) {
        await requireErosConsent(agata, userId);
        if (await canRouteErosChat(userId, creator, agata)) {
          const session = await findErosSession(userId, creatorId);
          if (!session) return { acknowledgedMessageIds: [] };
          const acknowledgedMessageIds = await markErosAssistantMessagesRead(erosAdapterDatabase(), session.userId, session.sessionId, ids);
          return { acknowledgedMessageIds };
        }
      }
      return await agataOrUnavailable().markMessagesRead(userId, creatorId, ids);
    } catch (error) {
      if (error instanceof ErosAdapterError) throw mapErosError(error);
      throw mapAgataError(error);
    }
  }

  @Get('state')
  async state(@Headers() headers: Record<string, string | undefined>) {
    const userId = authenticatedUser(headers);
    const agata = createAgataIntegration();
    if (!agata) return { enabled: false, chatHistory: false, longTermMemory: false, disclosureRequired: true, onboardingCompleted: false, disclosureVersion: null, onboardingCompletedAt: null, memoryOptIn: false, memoryOptInAt: null, aiAvailable: false };
    try {
      const state = await agata.chatState(userId);
      return publicMessagesState(state);
    } catch (error) { throw mapAgataError(error); }
  }

  @Post('consent')
  async consent(@Headers() headers: Record<string, string | undefined>, @Body() body: { longTermMemory?: unknown; disclosureVersion?: unknown }) {
    const userId = authenticatedUser(headers);
    if (body?.longTermMemory !== undefined && typeof body.longTermMemory !== 'boolean') throw new BadRequestException({ code: 'invalid_consent' });
    if (body?.disclosureVersion !== undefined && (!Number.isSafeInteger(body.disclosureVersion) || Number(body.disclosureVersion) < 1)) {
      throw new BadRequestException({ code: 'invalid_disclosure_version' });
    }
    const agata = agataOrUnavailable();
    try {
      const current = await agata.chatState(userId);
      if (current.disclosureRequired && body.disclosureVersion !== current.disclosureVersion) {
        throw new HttpException({ code: 'CHAT_DISCLOSURE_UPDATE_REQUIRED' }, 409);
      }
      const consent: AgataConsentSnapshot = {
        chat_history: true,
        long_term_memory: true,
        disclosure_version: typeof body.disclosureVersion === 'number' ? body.disclosureVersion : undefined,
        age_band: current.ageBand,
        age_status: current.ageStatus,
        age_method: current.ageMethod,
        jurisdiction: current.jurisdiction,
        verified_at: current.verifiedAt,
        expires_at: current.expiresAt,
        disclosure_accepted: true,
        disclosure_receipt_id: randomUUID(),
      };
      await agata.acceptConsent({ userId, consent });
      const updated = await agata.chatState(userId);
      return publicMessagesState(updated);
    } catch (error) { throw mapAgataError(error); }
  }

  @Get()
  async inbox(@Headers() headers: Record<string, string | undefined>, @Query('cursor') cursor?: string) {
    const userId = authenticatedUser(headers);
    const agata = agataOrUnavailable();
    try {
      const page = await agata.listConversations(userId, { limit: 30, ...(cursor ? { cursor } : {}) });
      const creatorIds = [...new Set(page.items.map((item) => item.gpointCharacterId))];
      const creators = creatorIds.length ? await prisma.creator.findMany({ where: { id: { in: creatorIds }, profile: { status: 'APPROVED' } }, include: CREATOR_INCLUDE }) : [];
      const channelAvatarUrls = await connectedTelegramChannelAvatarUrls(creators.map(creator => creator.id), prisma);
      const byId = new Map(creators.map((creator) => [creator.id, creator]));
      const hydrated = await Promise.all(page.items.map(async (item) => {
        const creator = byId.get(item.gpointCharacterId);
        if (!creator) return null;
        const dto = await publicCreator(creator);
        return { creator: messageThreadCreatorDto(dto, channelAvatarUrls.get(dto.id) ?? null), lastMessagePreview: item.lastMessagePreview, lastMessageRole: item.lastMessageRole, lastMessageAt: item.lastMessageAt, unreadCount: item.unreadCount };
      }));
      const items = hydrated.filter((item) => item !== null);
      const erosConversations = await localErosConversations(userId, agata);
      return { items: mergeErosInboxItems(items, erosConversations, !cursor), nextCursor: page.nextCursor };
    } catch (error) { if (error instanceof ErosAdapterError) throw mapErosError(error); throw mapAgataError(error); }
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
    const [creator, agata] = [await findApprovedCreator(creatorId), agataOrUnavailable()];
    try {
      if (isErosChatAllowed(userId, creatorId)) {
        await requireErosConsent(agata, userId);
        if (await canRouteErosChat(userId, creator, agata)) {
          const session = await findErosSession(userId, creatorId);
          const dto = await publicCreator(creator);
          if (!session) return { creator: dto, items: [], nextCursor: null };
          const before = decodeErosCursor(cursor);
          const history = await createErosEngineClient()!.history(session.userId, session.sessionId, limit, before);
          const pageSize = Math.min(limit, 50);
          return {
            creator: dto,
            items: history.messages.filter(item => item.role === 'user' || item.role === 'assistant' || item.role === 'system_error').map(item => ({
              id: item.id,
              role: item.role === 'system_error' ? 'system' : item.role,
              text: item.role === 'system_error' ? 'Сообщение не удалось обработать. Попробуйте ещё раз.' : item.content,
              ...(item.role === 'user' && item.client_msg_id ? { clientMessageId: item.client_msg_id } : {}),
              occurredAt: item.sent_at,
              incomingReadAt: item.read_at ?? null,
            })),
            nextCursor: history.messages.length === pageSize
              ? encodeErosCursor(history.messages[0].id)
              : null,
          };
        }
      }
      const page = await agata.listMessages(userId, creatorId, { limit, ...(cursor ? { cursor } : {}) });
      const dto = await publicCreator(creator);
      await recordChatEvent(userId, creatorId, 'CHAT_OPEN', 'chat-open-' + creatorId + '-' + Math.floor(Date.now() / (30 * 60_000)));
      return { creator: dto, items: page.items, nextCursor: page.nextCursor };
    } catch (error) { if (error instanceof ErosAdapterError) throw mapErosError(error); throw mapAgataError(error); }
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
    const creator = await findApprovedCreator(creatorId);
    const user = await prisma.user.findUnique({ where: { id: userId }, select: { id: true, languageCode: true } });
    if (!user) throw new UnauthorizedException();
    const agata = agataOrUnavailable();
    try {
      const current = await agata.chatState(userId);
      assertMessageAgeAssurance(current);
      if (!current.chatHistory || current.disclosureRequired) throw new HttpException({ code: 'CHAT_CONSENT_REQUIRED' }, 403);
      if (await canRouteErosChat(userId, creator, agata)) {
        const { identity, session } = await prepareErosChat(userId, creator);
        const eros = createErosEngineClient();
        if (!eros) throw new ErosAdapterError('eros_disabled');
        const response = await eros.sendMessage(identity.userId, session.sessionId, body.clientMessageId, text, {
          creatorId: identity.creatorId,
          instanceId: identity.relationshipId,
        });
        return {
          status: 'completed' as const,
          userMessageId: body.clientMessageId,
          response: { id: response.id, role: 'assistant' as const, text: response.text, occurredAt: new Date().toISOString(), incomingReadAt: null },
        };
      }
      const consent: AgataConsentSnapshot = {
        chat_history: current.chatHistory,
        long_term_memory: current.longTermMemory,
        age_band: current.ageBand,
        age_status: current.ageStatus,
        age_method: current.ageMethod,
        jurisdiction: current.jurisdiction,
        verified_at: current.verifiedAt,
        expires_at: current.expiresAt,
        disclosure_accepted: false,
        disclosure_receipt_id: body.clientMessageId,
      };
      const response = await agata.respond({
        userId, characterId: creator.id,
        requestId: body.clientMessageId, messageId: body.clientMessageId,
        conversationId: `gpoint-chat:${userId}:${creator.id}`,
        text, locale: localeFor(user.languageCode),
        profile: buildAgataCharacterProfile(creator), consent,
      });
      await recordChatEvent(userId, creator.id, 'CHAT_MESSAGE_SENT', body.clientMessageId);
      if (response.status === 'pending' || !response.message_id || !response.text) {
        return { userMessageId: body.clientMessageId, status: 'pending' as const };
      }
      await recordChatEvent(userId, creator.id, 'CHAT_RESPONSE_RECEIVED', body.clientMessageId);
      return {
        status: 'completed' as const,
        userMessageId: body.clientMessageId,
        response: { id: response.message_id, role: 'assistant' as const, text: response.text, occurredAt: new Date().toISOString(), incomingReadAt: null },
      };
    } catch (error) {
      if (error instanceof HttpException) throw error;
      if (error instanceof AgataServiceError && error.status === 403 && error.code === 'compliance_not_permitted') {
        try {
          const refreshed = await agata.chatState(userId);
          if (refreshed.ageAssuranceRequired) throw new HttpException({ code: 'AGE_ASSURANCE_REQUIRED' }, 403);
          if (!refreshed.chatHistory || refreshed.disclosureRequired) throw new HttpException({ code: 'CHAT_CONSENT_REQUIRED' }, 403);
        } catch (stateError) {
          if (stateError instanceof HttpException) throw stateError;
        }
      }
      if (error instanceof ErosAdapterError) throw mapErosError(error);
      throw mapAgataError(error);
    }
  }
}

@Controller('age-assurance')
export class AgeAssuranceController {
  @Get('state')
  async state(@Headers() headers: Record<string, string | undefined>) {
    const userId = authenticatedUser(headers);
    const agata = agataOrUnavailable();
    try { return await agata.ageAssurance(userId); }
    catch (error) { throw mapAgataError(error); }
  }

  @Post('self-attest')
  async selfAttest(
    @Headers() headers: Record<string, string | undefined>,
    @Body() body: { confirmed18Plus?: unknown },
  ) {
    const userId = authenticatedUser(headers);
    if (body?.confirmed18Plus !== true) throw new BadRequestException({ code: 'explicit_age_confirmation_required' });
    const agata = agataOrUnavailable();
    try {
      await agata.selfAttestAge(userId);
      return await agata.ageAssurance(userId);
    } catch (error) { throw mapAgataError(error); }
  }
}
