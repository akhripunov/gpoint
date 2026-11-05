BEGIN;

CREATE TABLE IF NOT EXISTS "GpointChatPolicyProfile" (
  "id" TEXT PRIMARY KEY,
  "sourceAgataId" TEXT UNIQUE,
  "sourceTenantId" TEXT NOT NULL,
  "version" INTEGER NOT NULL CHECK ("version" > 0),
  "termsVersion" TEXT NOT NULL,
  "privacyVersion" TEXT NOT NULL,
  "status" TEXT NOT NULL CHECK ("status" IN ('draft','active','retired')),
  "effectiveFrom" TIMESTAMPTZ(6) NOT NULL,
  "effectiveTo" TIMESTAMPTZ(6),
  "rules" JSONB NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "GpointChatPolicyProfile_effective_interval_check"
    CHECK ("effectiveTo" IS NULL OR "effectiveTo" > "effectiveFrom"),
  CONSTRAINT "GpointChatPolicyProfile_tenant_version_key"
    UNIQUE ("sourceTenantId", "version")
);
CREATE INDEX IF NOT EXISTS "GpointChatPolicyProfile_active_idx"
  ON "GpointChatPolicyProfile" ("sourceTenantId", "status", "effectiveFrom");
CREATE UNIQUE INDEX IF NOT EXISTS "GpointChatPolicyProfile_one_active_tenant_idx"
  ON "GpointChatPolicyProfile" ("sourceTenantId") WHERE "status"='active';

CREATE TABLE IF NOT EXISTS "GpointChatConsent" (
  "id" TEXT PRIMARY KEY,
  "sourceAgataId" TEXT UNIQUE,
  "sourceTenantId" TEXT NOT NULL,
  "userId" TEXT NOT NULL REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE,
  "purpose" TEXT NOT NULL CHECK ("purpose" IN ('chat_history','long_term_memory','terms_of_use','privacy_policy')),
  "grantedAt" TIMESTAMPTZ(6) NOT NULL,
  "revokedAt" TIMESTAMPTZ(6),
  "policyVersion" TEXT NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "GpointChatConsent_revocation_interval_check"
    CHECK ("revokedAt" IS NULL OR "revokedAt" >= "grantedAt")
);
CREATE INDEX IF NOT EXISTS "GpointChatConsent_user_purpose_granted_idx"
  ON "GpointChatConsent" ("userId", "purpose", "grantedAt");
CREATE UNIQUE INDEX IF NOT EXISTS "GpointChatConsent_one_current_per_purpose_idx"
  ON "GpointChatConsent" ("userId", "purpose") WHERE "revokedAt" IS NULL;

CREATE TABLE IF NOT EXISTS "GpointAgeAssurance" (
  "id" TEXT PRIMARY KEY,
  "sourceAgataId" TEXT UNIQUE,
  "sourceTenantId" TEXT NOT NULL,
  "userId" TEXT NOT NULL REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE,
  "ageBand" TEXT NOT NULL CHECK ("ageBand" IN ('unknown','minor','adult')),
  "status" TEXT NOT NULL CHECK ("status" IN ('pending','verified','self_attested','expired','rejected')),
  "method" TEXT NOT NULL,
  "verifiedAt" TIMESTAMPTZ(6),
  "assertedAt" TIMESTAMPTZ(6),
  "expiresAt" TIMESTAMPTZ(6),
  "version" INTEGER NOT NULL CHECK ("version" > 0),
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "GpointAgeAssurance_verified_evidence_check"
    CHECK ("status" <> 'verified' OR ("ageBand" = 'adult' AND "verifiedAt" IS NOT NULL)),
  CONSTRAINT "GpointAgeAssurance_self_attested_evidence_check"
    CHECK ("status" <> 'self_attested' OR ("ageBand" = 'adult' AND "method" = 'self_attested' AND "assertedAt" IS NOT NULL)),
  CONSTRAINT "GpointAgeAssurance_user_version_key" UNIQUE ("userId", "version")
);
CREATE INDEX IF NOT EXISTS "GpointAgeAssurance_user_version_idx"
  ON "GpointAgeAssurance" ("userId", "version");

CREATE TABLE IF NOT EXISTS "GpointAiDisclosureReceipt" (
  "id" TEXT PRIMARY KEY,
  "sourceAgataId" TEXT UNIQUE,
  "sourceTenantId" TEXT NOT NULL,
  "userId" TEXT NOT NULL REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE,
  "policyProfileId" TEXT NOT NULL REFERENCES "GpointChatPolicyProfile"("id") ON DELETE RESTRICT ON UPDATE CASCADE,
  "policyVersion" INTEGER NOT NULL CHECK ("policyVersion" > 0),
  "channel" TEXT NOT NULL,
  "receiptKey" TEXT NOT NULL,
  "displayedAt" TIMESTAMPTZ(6) NOT NULL,
  "memoryOptIn" BOOLEAN NOT NULL,
  "memoryOptInAt" TIMESTAMPTZ(6),
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "GpointAiDisclosureReceipt_user_policy_displayed_idx"
  ON "GpointAiDisclosureReceipt" ("userId", "policyProfileId", "policyVersion", "displayedAt");

CREATE TABLE IF NOT EXISTS "GpointLegacyConversationFence" (
  "id" TEXT PRIMARY KEY,
  "sourceAgataConversationId" TEXT NOT NULL UNIQUE,
  "sourceTenantId" TEXT NOT NULL,
  "userId" TEXT,
  "creatorId" TEXT,
  "sourceUserId" TEXT NOT NULL,
  "sourceCharacterId" TEXT NOT NULL,
  "messageCount" INTEGER NOT NULL CHECK ("messageCount" >= 0),
  "memoryCount" INTEGER NOT NULL CHECK ("memoryCount" >= 0),
  "firstMessageAt" TIMESTAMPTZ(6),
  "lastMessageAt" TIMESTAMPTZ(6),
  "consentWasActive" BOOLEAN NOT NULL,
  "migratedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "GpointLegacyConversationFence_user_creator_idx"
  ON "GpointLegacyConversationFence" ("userId", "creatorId");

CREATE TABLE IF NOT EXISTS "GpointLegacyConversationArchive" (
  "id" TEXT PRIMARY KEY,
  "sourceAgataConversationId" TEXT NOT NULL UNIQUE,
  "sourceTenantId" TEXT NOT NULL,
  "userId" TEXT,
  "creatorId" TEXT,
  "sourceRelationshipId" TEXT NOT NULL,
  "sourceScope" TEXT NOT NULL,
  "sourceScenarioId" TEXT,
  "sourceSystem" TEXT NOT NULL,
  "sourceExternalId" TEXT,
  "startedAt" TIMESTAMPTZ(6) NOT NULL,
  "closedAt" TIMESTAMPTZ(6),
  "languageCode" TEXT NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "GpointLegacyConversationArchive_user_creator_started_idx"
  ON "GpointLegacyConversationArchive" ("userId", "creatorId", "startedAt");

CREATE TABLE IF NOT EXISTS "GpointLegacyMessageArchive" (
  "id" TEXT PRIMARY KEY,
  "sourceAgataMessageId" TEXT NOT NULL UNIQUE,
  "sourceAgataConversationId" TEXT NOT NULL,
  "sourceTenantId" TEXT NOT NULL,
  "userId" TEXT,
  "creatorId" TEXT,
  "idempotencyKey" TEXT NOT NULL,
  "role" TEXT NOT NULL CHECK ("role" IN ('user','assistant','system')),
  "contentCiphertext" BYTEA,
  "contentNonce" BYTEA,
  "contentKeyVersion" TEXT,
  "systemMessageType" TEXT,
  "systemAction" TEXT,
  "occurredAt" TIMESTAMPTZ(6) NOT NULL,
  "deletedAt" TIMESTAMPTZ(6),
  "incomingReadAt" TIMESTAMPTZ(6),
  "languageCode" TEXT NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "GpointLegacyMessageArchive_envelope_check" CHECK (
    ("contentCiphertext" IS NULL AND "contentNonce" IS NULL AND "contentKeyVersion" IS NULL)
    OR ("contentCiphertext" IS NOT NULL AND "contentNonce" IS NOT NULL
      AND "contentKeyVersion" IS NOT NULL AND octet_length("contentNonce") = 12)
  )
);
CREATE INDEX IF NOT EXISTS "GpointLegacyMessageArchive_conversation_time_idx"
  ON "GpointLegacyMessageArchive" ("sourceAgataConversationId", "occurredAt");
CREATE INDEX IF NOT EXISTS "GpointLegacyMessageArchive_user_creator_time_idx"
  ON "GpointLegacyMessageArchive" ("userId", "creatorId", "occurredAt");

-- This migration provisions GPoint-owned chat state and policy structures.
-- Policy, consent, age-assurance and disclosure state for mapped GPoint users
-- are copied. Conversations, messages and memories remain in AGATA and are
-- deliberately not copied or deleted.
DO $$
BEGIN
  IF to_regnamespace('agata') IS NOT NULL THEN
    IF (SELECT count(DISTINCT au.tenant_id)
        FROM agata.users au WHERE au.source_system='gpoint' AND EXISTS (
          SELECT 1 FROM "User" u WHERE u.id=au.external_id
        )) > 1 THEN
      RAISE EXCEPTION 'GPOINT_AGATA_TENANT_SCOPE_AMBIGUOUS';
    END IF;

    -- One explicit GPoint-wide policy. Legacy AGATA regional profiles remain untouched
    -- in AGATA and are never consulted to define GPoint runtime rules.
    INSERT INTO "GpointChatPolicyProfile"
      ("id","sourceTenantId","version","termsVersion","privacyVersion","status","effectiveFrom","effectiveTo","rules")
    SELECT md5(au.tenant_id::text || ':gpoint-global-chat-policy:v1'),au.tenant_id::text,1,
      'gpoint-terms-v1','gpoint-privacy-v1','active',now(),NULL,
      '{"minimum_age":18,"ai_disclosure_required":true,"self_attestation_allowed":true,"adult_only_features":[],"disclosure_repeat_hours":24}'::jsonb
    FROM (SELECT DISTINCT au.tenant_id FROM agata.users au
      JOIN "User" u ON u.id=au.external_id WHERE au.source_system='gpoint') au
    ON CONFLICT ("sourceTenantId","version") DO NOTHING;

    WITH scoped AS (
      SELECT c.*,u.id AS gpoint_user_id,
        max(coalesce(c.revoked_at,c.granted_at))
          OVER (PARTITION BY c.tenant_id,c.user_id,c.purpose) AS latest_event_at,
        row_number() OVER (PARTITION BY c.tenant_id,c.user_id,c.purpose
          ORDER BY coalesce(c.revoked_at,c.granted_at) DESC,
            CASE WHEN c.revoked_at IS NULL THEN 1 ELSE 0 END,c.granted_at DESC,c.id DESC) AS latest_event_rank
      FROM agata.consents c
      JOIN agata.users au ON au.tenant_id=c.tenant_id AND au.id=c.user_id AND au.source_system='gpoint'
      JOIN "User" u ON u.id=au.external_id
      WHERE c.purpose IN ('chat_history','long_term_memory')
    )
    INSERT INTO "GpointChatConsent"
      ("id","sourceAgataId","sourceTenantId","userId","purpose","grantedAt","revokedAt","policyVersion")
    SELECT id::text,id::text,tenant_id::text,gpoint_user_id,purpose,granted_at,
      CASE WHEN revoked_at IS NULL AND latest_event_rank > 1 THEN latest_event_at ELSE revoked_at END,
      policy_version
    FROM scoped
    ON CONFLICT ("sourceAgataId") DO NOTHING;

    INSERT INTO "GpointAgeAssurance"
      ("id","sourceAgataId","sourceTenantId","userId","ageBand","status","method","verifiedAt","assertedAt","expiresAt","version")
    SELECT DISTINCT ON (u.id) a.id::text,a.id::text,a.tenant_id::text,u.id,a.age_band,a.status,a.method,a.verified_at,a.asserted_at,a.expires_at,a.version
    FROM agata.age_assurances a
    JOIN agata.users au ON au.tenant_id=a.tenant_id AND au.id=a.user_id AND au.source_system='gpoint'
    JOIN "User" u ON u.id=au.external_id
    ORDER BY u.id,a.created_at DESC,coalesce(a.verified_at,a.asserted_at) DESC NULLS LAST,a.version DESC,a.id DESC
    ON CONFLICT ("sourceAgataId") DO NOTHING;

    IF EXISTS (SELECT 1 FROM agata.users au WHERE au.source_system='gpoint'
               AND EXISTS (SELECT 1 FROM "User" u WHERE u.id=au.external_id))
       AND NOT EXISTS (
         SELECT 1 FROM "GpointChatPolicyProfile" p
         WHERE p."sourceTenantId" IN (SELECT au.tenant_id::text FROM agata.users au
           JOIN "User" u ON u.id=au.external_id WHERE au.source_system='gpoint')
           AND p.status='active' AND p."effectiveFrom" <= now()
           AND (p."effectiveTo" IS NULL OR p."effectiveTo" > now())
       ) THEN RAISE EXCEPTION 'GPOINT_CHAT_POLICY_PROFILE_COPY_MISSING'; END IF;
  END IF;
END $$;

COMMIT;
