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

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

function disabledChatState() {
  return buildGpointMessagesState({
    policies: [], consents: [], ages: [], receipts: [],
    erosEnabled: false,
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
  return buildGpointMessagesState({ policies, consents, ages, receipts, erosEnabled: process.env.EROS_ENABLED === 'true' });
}

export async function recordGpointChatConsent(
  db: PrismaClient,
  userId: string,
  input: { chatHistory?: unknown; longTermMemory?: unknown; termsAccepted?: unknown; privacyAccepted?: unknown;
    termsVersion?: unknown; privacyVersion?: unknown; disclosureVersion?: unknown },
) {
  if (input.chatHistory !== true || input.longTermMemory !== true || input.termsAccepted !== true ||
      input.privacyAccepted !== true || !Number.isSafeInteger(input.disclosureVersion) ||
      typeof input.termsVersion !== 'string' || typeof input.privacyVersion !== 'string') {
    throw new BadRequestException({ code: 'explicit_terms_privacy_chat_and_memory_consent_required' });
  }
  const tenantId = tenantPinned();
  const current = await loadGpointChatState(db, userId);
  if (!current.enabled || current.disclosureVersion !== input.disclosureVersion ||
      current.termsVersion !== input.termsVersion || current.privacyVersion !== input.privacyVersion) {
    throw new HttpException({ code: 'CHAT_DISCLOSURE_UPDATE_REQUIRED' }, 409);
  }
  const now = new Date();
  const profiles = await db.gpointChatPolicyProfile.findMany({ where: { sourceTenantId: tenantId, status: 'active', version: input.disclosureVersion as number, effectiveFrom: { lte: now }, OR: [{ effectiveTo: null }, { effectiveTo: { gt: now } }] }, orderBy: [{ id: 'asc' }] });
  const rules = profiles[0]?.rules;
  const policy = profiles[0];
  if (!policy || profiles.some((row) => stableJson(row.rules) !== stableJson(rules))) {
    throw new HttpException({ code: 'CHAT_POLICY_UNAVAILABLE' }, 503);
  }
  await db.$transaction(async (tx) => {
    await tx.gpointChatConsent.updateMany({ where: { sourceTenantId: tenantId, userId, purpose: { in: ['chat_history', 'long_term_memory', 'terms_of_use', 'privacy_policy'] }, revokedAt: null }, data: { revokedAt: now } });
    await tx.gpointChatConsent.createMany({ data: [
      { id: randomUUID(), sourceTenantId: tenantId, userId, purpose: 'chat_history', grantedAt: now, policyVersion: String(policy.version) },
      { id: randomUUID(), sourceTenantId: tenantId, userId, purpose: 'long_term_memory', grantedAt: now, policyVersion: String(policy.version) },
      { id: randomUUID(), sourceTenantId: tenantId, userId, purpose: 'terms_of_use', grantedAt: now, policyVersion: policy.termsVersion },
      { id: randomUUID(), sourceTenantId: tenantId, userId, purpose: 'privacy_policy', grantedAt: now, policyVersion: policy.privacyVersion },
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
    sourceTenantId: tenantPinned(), status: 'active', effectiveFrom: { lte: now },
    OR: [{ effectiveTo: null }, { effectiveTo: { gt: now } }],
  } });
  if (!policy.length) throw new HttpException({ code: 'CHAT_POLICY_UNAVAILABLE' }, 503);
  const selected = policy.sort((a, b) => b.version - a.version || b.effectiveFrom.getTime() - a.effectiveFrom.getTime() || String(a.id).localeCompare(String(b.id)))[0];
  const latestVersion = policy.filter((row) => row.version === selected.version);
  if (latestVersion.some((row) => stableJson(row.rules) !== stableJson(selected.rules))) throw new HttpException({ code: 'CHAT_POLICY_UNAVAILABLE' }, 503);
  return gpointAgeAssuranceState(state, (selected.rules as Record<string, unknown>).self_attestation_allowed === true);
}

export async function recordGpointSelfAttestation(db: PrismaClient, userId: string, confirmed18Plus: unknown) {
  if (confirmed18Plus !== true) throw new BadRequestException({ code: 'explicit_age_confirmation_required' });
  const state = await loadGpointAgeAssurance(db, userId);
  if (!state.required) return state;
  if (!state.selfAttestationAllowed) throw new HttpException({ code: 'AGE_ASSURANCE_REQUIRED' }, 403);
  const tenantId = tenantPinned();
  const now = new Date();
  const latest = await db.gpointAgeAssurance.findFirst({ where: { sourceTenantId: tenantId, userId }, orderBy: [{ version: 'desc' }, { id: 'desc' }] });
  await db.gpointAgeAssurance.create({ data: {
    id: randomUUID(), sourceTenantId: tenantId, userId, ageBand: 'adult', status: 'self_attested',
    method: 'self_attested', assertedAt: now, version: (latest?.version ?? 0) + 1,
  } });
  return loadGpointAgeAssurance(db, userId);
}

export { isGpointChatPermitted };
