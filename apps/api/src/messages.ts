import { randomUUID } from 'node:crypto';
import {
  AgataServiceError,
  type AgataChatState,
  type AgataCharacterProfile,
  type AgataConsentSnapshot,
} from '@gpoint/agata-client';
import { BadRequestException, Body, Controller, Get, Headers, HttpException, NotFoundException, Param, Post, Query, UnauthorizedException } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { createAgataIntegration } from './agata-integration.js';
import { publicCreator } from './core-v2.js';
import { FixedWindowRateLimiter } from './rate-limit.js';
import { resolveSession } from './session.js';

const prisma = new PrismaClient();
const sendLimiter = new FixedWindowRateLimiter(30, 60_000);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CREATOR_INCLUDE = { profile: true, vipOffers: { where: { status: 'ACTIVE' }, take: 1 } } as const;

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

function authenticatedUser(headers: Record<string, string | undefined>): string {
  const id = resolveSession(headers.authorization?.replace(/^Bearer\s+/i, ''));
  if (!id) throw new UnauthorizedException();
  return id;
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
  @Get('state')
  async state(@Headers() headers: Record<string, string | undefined>) {
    const userId = authenticatedUser(headers);
    const agata = createAgataIntegration();
    if (!agata) return { enabled: false, chatHistory: false, longTermMemory: false, disclosureRequired: true, aiAvailable: false };
    try {
      const state = await agata.chatState(userId);
      return { enabled: true, chatHistory: state.chatHistory, longTermMemory: state.longTermMemory, disclosureRequired: state.disclosureRequired, ageAssuranceRequired: state.ageAssuranceRequired, aiAvailable: process.env.AGATA_LLM_PROVIDER !== 'disabled' && process.env.AGATA_LLM_PROVIDER !== 'none' };
    } catch (error) { throw mapAgataError(error); }
  }

  @Post('consent')
  async consent(@Headers() headers: Record<string, string | undefined>, @Body() body: { longTermMemory?: unknown }) {
    const userId = authenticatedUser(headers);
    if (typeof body?.longTermMemory !== 'boolean') throw new BadRequestException({ code: 'invalid_consent' });
    const agata = agataOrUnavailable();
    try {
      const current = await agata.chatState(userId);
      const consent: AgataConsentSnapshot = {
        chat_history: true,
        long_term_memory: body.longTermMemory,
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
      return { enabled: true, chatHistory: updated.chatHistory, longTermMemory: updated.longTermMemory, disclosureRequired: updated.disclosureRequired, ageAssuranceRequired: updated.ageAssuranceRequired, aiAvailable: process.env.AGATA_LLM_PROVIDER !== 'disabled' && process.env.AGATA_LLM_PROVIDER !== 'none' };
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
      const byId = new Map(creators.map((creator) => [creator.id, creator]));
      const hydrated = await Promise.all(page.items.map(async (item) => {
        const creator = byId.get(item.gpointCharacterId);
        if (!creator) return null;
        const dto = await publicCreator(creator);
        return { creator: { id: dto.id, slug: dto.slug, displayName: dto.displayName, avatarUrl: dto.avatarUrl, verified: dto.verified }, lastMessagePreview: item.lastMessagePreview, lastMessageRole: item.lastMessageRole, lastMessageAt: item.lastMessageAt };
      }));
      const items = hydrated.filter((item) => item !== null);
      return { items, nextCursor: page.nextCursor };
    } catch (error) { throw mapAgataError(error); }
  }

  @Get('creator/:creatorId')
  async history(
    @Param('creatorId') creatorId: string,
    @Headers() headers: Record<string, string | undefined>,
    @Query('cursor') cursor?: string,
    @Query('limit') limitValue?: string,
  ) {
    const userId = authenticatedUser(headers);
    const limit = limitValue === undefined ? 50 : Number(limitValue);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new BadRequestException({ code: 'invalid_limit' });
    const [creator, agata] = [await findApprovedCreator(creatorId), agataOrUnavailable()];
    try {
      const page = await agata.listMessages(userId, creatorId, { limit, ...(cursor ? { cursor } : {}) });
      const dto = await publicCreator(creator);
      await recordChatEvent(userId, creatorId, 'CHAT_OPEN', 'chat-open-' + creatorId + '-' + Math.floor(Date.now() / (30 * 60_000)));
      return { creator: dto, items: page.items, nextCursor: page.nextCursor };
    } catch (error) { throw mapAgataError(error); }
  }

  @Post('creator/:creatorId')
  async send(
    @Param('creatorId') creatorId: string,
    @Headers() headers: Record<string, string | undefined>,
    @Body() body: { clientMessageId?: unknown; text?: unknown },
  ) {
    const userId = authenticatedUser(headers);
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
        response: { id: response.message_id, role: 'assistant' as const, text: response.text, occurredAt: new Date().toISOString() },
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
