import { randomUUID } from 'node:crypto';
import { BadRequestException, HttpException } from '@nestjs/common';
import type { PrismaClient } from '@prisma/client';
import { buildGpointMessagesState, gpointAgeAssuranceState, isGpointChatPermitted } from './gpoint-chat-state.js';

function tenantPinned(): string {
  const policyTenantId = process.env.GPOINT_CHAT_POLICY_TENANT_ID ?? '';
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(policyTenantId)) {
    throw new HttpException({ code: 'CHAT_POLICY_UNAVAILABLE' }, 503);
  }
  return policyTenantId;
}

function policyScope() {
  const jurisdiction = process.env.GPOINT_CHAT_POLICY_JURISDICTION ?? '';
  const market = process.env.GPOINT_CHAT_POLICY_MARKET ?? '';
  if (!/^[A-Z0-9_-]{2,32}$/.test(jurisdiction) || !/^[a-z0-9_-]{2,32}$/.test(market)) {
    throw new HttpException({ code: 'CHAT_POLICY_UNAVAILABLE' }, 503);
  }
  return { jurisdiction, market };
}

function disabledChatState() {
  return buildGpointMessagesState({
    policies: [], consents: [], ages: [], receipts: [],
    policyScope: { jurisdiction: '', market: '' }, erosEnabled: false,
  });
}

export async function loadGpointChatState(db: PrismaClient, userId: string) {
  if (process.env.EROS_ENABLED !== 'true') return disabledChatState();
  const tenantId = tenantPinned();
  const [policies, consents, ages, receipts] = await Promise.all([
    db.gpointChatPolicyProfile.findMany({ where: { sourceTenantId: tenantId }, orderBy: [{ effectiveFrom: 'desc' }, { id: 'desc' }] }),
    db.gpointChatConsent.findMany({ where: { userId, sourceTenantId: tenantId }, orderBy: [{ grantedAt: 'desc' }, { id: 'desc' }] }),
    db.gpointAgeAssurance.findMany({ where: { userId, sourceTenantId: tenantId }, orderBy: [{ version: 'desc' }, { id: 'desc' }] }),
    db.gpointAiDisclosureReceipt.findMany({ where: { userId, sourceTenantId: tenantId }, orderBy: [{ displayedAt: 'desc' }, { id: 'desc' }] }),
  ]);
  return buildGpointMessagesState({ policies, consents, ages, receipts, policyScope: policyScope(), erosEnabled: process.env.EROS_ENABLED === 'true' });
}

export async function recordGpointChatConsent(
  db: PrismaClient,
  userId: string,
  input: { chatHistory?: unknown; longTermMemory?: unknown; disclosureVersion?: unknown },
) {
  if (input.chatHistory !== true || input.longTermMemory !== true || !Number.isSafeInteger(input.disclosureVersion)) {
    throw new BadRequestException({ code: 'explicit_chat_and_memory_consent_required' });
  }
  const tenantId = tenantPinned();
  const current = await loadGpointChatState(db, userId);
  if (!current.enabled || current.disclosureVersion !== input.disclosureVersion) {
    throw new HttpException({ code: 'CHAT_DISCLOSURE_UPDATE_REQUIRED' }, 409);
  }
  const scope = policyScope();
  const profiles = await db.gpointChatPolicyProfile.findMany({ where: { sourceTenantId: tenantId, status: 'active', version: input.disclosureVersion as number, jurisdiction: scope.jurisdiction, market: scope.market } });
  const policy = profiles.find((row) => row.jurisdiction === current.jurisdiction);
  if (!policy || profiles.filter((row) => row.jurisdiction === current.jurisdiction).length !== 1) {
    throw new HttpException({ code: 'CHAT_POLICY_UNAVAILABLE' }, 503);
  }
  const now = new Date();
  await db.$transaction(async (tx) => {
    await tx.gpointChatConsent.updateMany({ where: { sourceTenantId: tenantId, userId, purpose: { in: ['chat_history', 'long_term_memory'] }, revokedAt: null }, data: { revokedAt: now } });
    await tx.gpointChatConsent.createMany({ data: [
      { id: randomUUID(), sourceTenantId: tenantId, userId, purpose: 'chat_history', grantedAt: now, policyVersion: String(policy.version) },
      { id: randomUUID(), sourceTenantId: tenantId, userId, purpose: 'long_term_memory', grantedAt: now, policyVersion: String(policy.version) },
    ] });
    await tx.gpointAiDisclosureReceipt.create({ data: {
      id: randomUUID(), sourceTenantId: tenantId, userId, policyProfileId: policy.id,
      policyVersion: policy.version, channel: 'gpoint_miniapp', receiptKey: randomUUID(), displayedAt: now,
      memoryOptIn: true, memoryOptInAt: now,
    } });
  });
  return loadGpointChatState(db, userId);
}

export async function revokeGpointChatConsent(db: PrismaClient, userId: string, purpose: 'chat_history' | 'long_term_memory') {
  if (process.env.EROS_ENABLED !== 'true') return disabledChatState();
  const sourceTenantId = tenantPinned();
  const now = new Date();
  await db.gpointChatConsent.updateMany({ where: { sourceTenantId, userId, purpose, revokedAt: null }, data: { revokedAt: now } });
  return loadGpointChatState(db, userId);
}

export async function loadGpointAgeAssurance(db: PrismaClient, userId: string) {
  const state = await loadGpointChatState(db, userId);
  if (!state.enabled) throw new HttpException({ code: 'CHAT_POLICY_UNAVAILABLE' }, 503);
  const now = new Date();
  const policy = await db.gpointChatPolicyProfile.findMany({ where: {
    sourceTenantId: tenantPinned(), status: 'active', ...policyScope(), effectiveFrom: { lte: now },
    OR: [{ effectiveTo: null }, { effectiveTo: { gt: now } }],
  } });
  const matching = policy.filter((row) => row.jurisdiction === state.jurisdiction);
  if (matching.length !== 1) throw new HttpException({ code: 'CHAT_POLICY_UNAVAILABLE' }, 503);
  return gpointAgeAssuranceState(state, (matching[0].rules as Record<string, unknown>).self_attestation_allowed === true);
}

export async function recordGpointSelfAttestation(db: PrismaClient, userId: string, confirmed18Plus: unknown) {
  if (confirmed18Plus !== true) throw new BadRequestException({ code: 'explicit_age_confirmation_required' });
  const state = await loadGpointAgeAssurance(db, userId);
  if (!state.required) return state;
  if (!state.selfAttestationAllowed) throw new HttpException({ code: 'AGE_ASSURANCE_REQUIRED' }, 403);
  const tenantId = tenantPinned();
  const now = new Date();
  const latest = await db.gpointAgeAssurance.findFirst({ where: { sourceTenantId: tenantId, userId, jurisdiction: state.jurisdiction }, orderBy: [{ version: 'desc' }, { id: 'desc' }] });
  await db.gpointAgeAssurance.create({ data: {
    id: randomUUID(), sourceTenantId: tenantId, userId, ageBand: 'adult', status: 'self_attested',
    method: 'self_attested', jurisdiction: state.jurisdiction, assertedAt: now, version: (latest?.version ?? 0) + 1,
  } });
  return loadGpointAgeAssurance(db, userId);
}

export { isGpointChatPermitted };
