import assert from 'node:assert/strict';
import test from 'node:test';
import { BadRequestException } from '@nestjs/common';
import type { PrismaClient } from '@prisma/client';
import { loadGpointAgeAssurance, recordGpointSelfAttestation } from '../src/gpoint-chat-store.js';

const tenantId = '30000000-0000-4000-8000-000000000003';
const userId = '10000000-0000-4000-8000-000000000001';
const policy = {
  id: 'policy-1', sourceTenantId: tenantId, termsVersion: 'terms-v1', privacyVersion: 'privacy-v1',
  version: 1, status: 'active', effectiveFrom: new Date('2026-01-01T00:00:00.000Z'), effectiveTo: null,
  rules: { minimum_age: 18, adult_only_features: ['romantic'], ai_disclosure_required: true, self_attestation_allowed: true },
};

function syntheticPolicyDatabase() {
  const ages: Array<Record<string, unknown>> = [];
  return {
    ages,
    gpointChatPolicyProfile: { findMany: async () => [policy] },
    gpointChatConsent: { findMany: async () => [] },
    gpointAiDisclosureReceipt: { findMany: async () => [] },
    gpointAgeAssurance: {
      findMany: async () => ages,
      findFirst: async () => [...ages].sort((a, b) => Number(b.version) - Number(a.version))[0] ?? null,
      create: async ({ data }: { data: Record<string, unknown> }) => { ages.push(data); return data; },
    },
  } as unknown as PrismaClient & { ages: Array<Record<string, unknown>> };
}

test('age assurance is GPoint-owned and persists an explicit 18+ attestation once', async () => {
  const previous = { enabled: process.env.EROS_ENABLED, tenant: process.env.GPOINT_CHAT_POLICY_TENANT_ID };
  process.env.EROS_ENABLED = 'true';
  process.env.GPOINT_CHAT_POLICY_TENANT_ID = tenantId;
  const db = syntheticPolicyDatabase();
  try {
    assert.deepEqual(await loadGpointAgeAssurance(db, userId), {
      required: true, eligible: false, status: 'pending', method: 'unknown', assertedAt: null,
      expiresAt: null, selfAttestationAllowed: true,
    });
    const accepted = await recordGpointSelfAttestation(db, userId, true);
    assert.equal(accepted.required, false);
    assert.equal(accepted.eligible, true);
    assert.equal(accepted.status, 'self_attested');
    assert.equal(db.ages.length, 1);
    assert.equal((await recordGpointSelfAttestation(db, userId, true)).required, false);
    assert.equal(db.ages.length, 1);
  } finally {
    if (previous.enabled === undefined) delete process.env.EROS_ENABLED; else process.env.EROS_ENABLED = previous.enabled;
    if (previous.tenant === undefined) delete process.env.GPOINT_CHAT_POLICY_TENANT_ID; else process.env.GPOINT_CHAT_POLICY_TENANT_ID = previous.tenant;
  }
});

test('age assurance rejects missing explicit confirmation without accessing persistence', async () => {
  const db = new Proxy({}, { get: () => { throw new Error('database should not be reached'); } }) as never;
  await assert.rejects(recordGpointSelfAttestation(db, userId, false), BadRequestException);
  await assert.rejects(recordGpointSelfAttestation(db, userId, undefined), BadRequestException);
});
