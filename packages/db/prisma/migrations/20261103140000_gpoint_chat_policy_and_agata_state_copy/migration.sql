CREATE TABLE IF NOT EXISTS "GpointChatPolicyProfile" (
  "id" TEXT PRIMARY KEY,
  "sourceAgataId" TEXT UNIQUE,
  "sourceTenantId" TEXT NOT NULL,
  "jurisdiction" TEXT NOT NULL,
  "market" TEXT NOT NULL,
  "version" INTEGER NOT NULL CHECK ("version" > 0),
  "status" TEXT NOT NULL CHECK ("status" IN ('draft','active','retired')),
  "effectiveFrom" TIMESTAMP(3) NOT NULL,
  "effectiveTo" TIMESTAMP(3),
  "rules" JSONB NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "GpointChatPolicyProfile_effective_interval_check"
    CHECK ("effectiveTo" IS NULL OR "effectiveTo" > "effectiveFrom"),
  CONSTRAINT "GpointChatPolicyProfile_tenant_jurisdiction_market_version_key"
    UNIQUE ("sourceTenantId", "jurisdiction", "market", "version")
);
CREATE INDEX IF NOT EXISTS "GpointChatPolicyProfile_active_idx"
  ON "GpointChatPolicyProfile" ("sourceTenantId", "jurisdiction", "market", "status", "effectiveFrom");
CREATE UNIQUE INDEX IF NOT EXISTS "GpointChatPolicyProfile_one_active_scope_idx"
  ON "GpointChatPolicyProfile" ("sourceTenantId", "jurisdiction", "market") WHERE "status"='active';

CREATE TABLE IF NOT EXISTS "GpointChatConsent" (
  "id" TEXT PRIMARY KEY,
  "sourceAgataId" TEXT UNIQUE,
  "sourceTenantId" TEXT NOT NULL,
  "userId" TEXT NOT NULL REFERENCES "User"("id") ON DELETE RESTRICT,
  "purpose" TEXT NOT NULL CHECK ("purpose" IN ('chat_history','long_term_memory')),
  "grantedAt" TIMESTAMP(3) NOT NULL,
  "revokedAt" TIMESTAMP(3),
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
  "userId" TEXT NOT NULL REFERENCES "User"("id") ON DELETE RESTRICT,
  "ageBand" TEXT NOT NULL CHECK ("ageBand" IN ('unknown','minor','adult')),
  "status" TEXT NOT NULL CHECK ("status" IN ('pending','verified','self_attested','expired','rejected')),
  "method" TEXT NOT NULL,
  "jurisdiction" TEXT NOT NULL,
  "verifiedAt" TIMESTAMP(3),
  "assertedAt" TIMESTAMP(3),
  "expiresAt" TIMESTAMP(3),
  "version" INTEGER NOT NULL CHECK ("version" > 0),
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "GpointAgeAssurance_verified_evidence_check"
    CHECK ("status" <> 'verified' OR ("ageBand" = 'adult' AND "verifiedAt" IS NOT NULL)),
  CONSTRAINT "GpointAgeAssurance_self_attested_evidence_check"
    CHECK ("status" <> 'self_attested' OR ("ageBand" = 'adult' AND "method" = 'self_attested' AND "assertedAt" IS NOT NULL)),
  CONSTRAINT "GpointAgeAssurance_user_jurisdiction_version_key" UNIQUE ("userId", "jurisdiction", "version")
);
CREATE INDEX IF NOT EXISTS "GpointAgeAssurance_user_version_idx"
  ON "GpointAgeAssurance" ("userId", "version");

CREATE TABLE IF NOT EXISTS "GpointAiDisclosureReceipt" (
  "id" TEXT PRIMARY KEY,
  "sourceAgataId" TEXT UNIQUE,
  "sourceTenantId" TEXT NOT NULL,
  "userId" TEXT NOT NULL REFERENCES "User"("id") ON DELETE RESTRICT,
  "policyProfileId" TEXT NOT NULL REFERENCES "GpointChatPolicyProfile"("id") ON DELETE RESTRICT,
  "policyVersion" INTEGER NOT NULL CHECK ("policyVersion" > 0),
  "channel" TEXT NOT NULL,
  "receiptKey" TEXT NOT NULL,
  "displayedAt" TIMESTAMP(3) NOT NULL,
  "memoryOptIn" BOOLEAN NOT NULL DEFAULT FALSE,
  "memoryOptInAt" TIMESTAMP(3),
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
  "firstMessageAt" TIMESTAMP(3),
  "lastMessageAt" TIMESTAMP(3),
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
  "startedAt" TIMESTAMP(3) NOT NULL,
  "closedAt" TIMESTAMP(3),
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
  "occurredAt" TIMESTAMP(3) NOT NULL,
  "deletedAt" TIMESTAMP(3),
  "incomingReadAt" TIMESTAMP(3),
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

DO $$
BEGIN
  -- A brand-new GPoint database has no AGATA schema. In the production six-to-four
  -- cutover the separate preflight requires it and verifies the copied row counts.
  IF to_regnamespace('agata') IS NOT NULL THEN
    IF (SELECT count(DISTINCT au.tenant_id)
        FROM agata.users au WHERE au.source_system='gpoint' AND EXISTS (
          SELECT 1 FROM "User" u WHERE u.id=au.external_id
        )) > 1 THEN
      RAISE EXCEPTION 'GPOINT_AGATA_TENANT_SCOPE_AMBIGUOUS';
    END IF;

    IF EXISTS (
      SELECT 1 FROM agata.users au
      LEFT JOIN "User" u ON u.id=au.external_id
      WHERE au.source_system='gpoint' AND u.id IS NULL
        AND (
          EXISTS (SELECT 1 FROM agata.consents c WHERE c.tenant_id=au.tenant_id AND c.user_id=au.id)
          OR EXISTS (SELECT 1 FROM agata.age_assurances a WHERE a.tenant_id=au.tenant_id AND a.user_id=au.id)
          OR EXISTS (SELECT 1 FROM agata.ai_disclosure_receipts r WHERE r.tenant_id=au.tenant_id AND r.user_id=au.id)
          OR EXISTS (SELECT 1 FROM agata.conversations c WHERE c.tenant_id=au.tenant_id AND c.user_id=au.id)
          OR EXISTS (SELECT 1 FROM agata.memories m WHERE m.tenant_id=au.tenant_id AND m.user_id=au.id)
        )
    ) THEN RAISE EXCEPTION 'GPOINT_AGATA_STATE_HAS_UNMAPPED_GPOINT_USER'; END IF;

    INSERT INTO "GpointChatPolicyProfile"
      ("id","sourceAgataId","sourceTenantId","jurisdiction","market","version","status","effectiveFrom","effectiveTo","rules")
    SELECT p.id::text,p.id::text,p.tenant_id::text,p.jurisdiction,p.market,p.version,p.status,p.effective_from,p.effective_to,p.rules
    FROM agata.regional_policy_profiles p
    WHERE EXISTS (
      SELECT 1 FROM agata.users au JOIN "User" u ON u.id=au.external_id
      WHERE au.tenant_id=p.tenant_id AND au.source_system='gpoint'
    ) OR EXISTS (
      SELECT 1 FROM agata.ai_disclosure_receipts r
      JOIN agata.users au ON au.tenant_id=r.tenant_id AND au.id=r.user_id
      JOIN "User" u ON u.id=au.external_id
      WHERE r.tenant_id=p.tenant_id AND r.policy_profile_id=p.id AND au.source_system='gpoint'
    )
    ON CONFLICT ("sourceAgataId") DO NOTHING;

    INSERT INTO "GpointChatConsent"
      ("id","sourceAgataId","sourceTenantId","userId","purpose","grantedAt","revokedAt","policyVersion")
    SELECT c.id::text,c.id::text,c.tenant_id::text,u.id,c.purpose,c.granted_at,c.revoked_at,c.policy_version
    FROM agata.consents c
    JOIN agata.users au ON au.tenant_id=c.tenant_id AND au.id=c.user_id AND au.source_system='gpoint'
    JOIN "User" u ON u.id=au.external_id
    WHERE c.purpose IN ('chat_history','long_term_memory')
    ON CONFLICT ("sourceAgataId") DO NOTHING;

    INSERT INTO "GpointAgeAssurance"
      ("id","sourceAgataId","sourceTenantId","userId","ageBand","status","method","jurisdiction","verifiedAt","assertedAt","expiresAt","version")
    SELECT a.id::text,a.id::text,a.tenant_id::text,u.id,a.age_band,a.status,a.method,a.jurisdiction,a.verified_at,a.asserted_at,a.expires_at,a.version
    FROM agata.age_assurances a
    JOIN agata.users au ON au.tenant_id=a.tenant_id AND au.id=a.user_id AND au.source_system='gpoint'
    JOIN "User" u ON u.id=au.external_id
    ON CONFLICT ("sourceAgataId") DO NOTHING;

    INSERT INTO "GpointAiDisclosureReceipt"
      ("id","sourceAgataId","sourceTenantId","userId","policyProfileId","policyVersion","channel","receiptKey","displayedAt","memoryOptIn","memoryOptInAt")
    SELECT r.id::text,r.id::text,r.tenant_id::text,u.id,r.policy_profile_id::text,r.policy_version,r.channel,r.receipt_key,r.displayed_at,r.memory_opt_in,r.memory_opt_in_at
    FROM agata.ai_disclosure_receipts r
    JOIN agata.users au ON au.tenant_id=r.tenant_id AND au.id=r.user_id AND au.source_system='gpoint'
    JOIN "User" u ON u.id=au.external_id
    ON CONFLICT ("sourceAgataId") DO NOTHING;

    INSERT INTO "GpointLegacyConversationFence"
      ("id","sourceAgataConversationId","sourceTenantId","userId","creatorId","sourceUserId","sourceCharacterId","messageCount","memoryCount","firstMessageAt","lastMessageAt","consentWasActive")
    SELECT c.id::text,c.id::text,c.tenant_id::text,u.id,gp.id,au.external_id,ac.external_id,
      (SELECT count(*)::int FROM agata.messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id),
      (SELECT count(*)::int FROM agata.memories mm WHERE mm.tenant_id=c.tenant_id AND mm.user_id=c.user_id AND mm.character_id=c.character_id AND mm.relationship_id=c.relationship_id),
      (SELECT min(m.occurred_at) FROM agata.messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id),
      (SELECT max(m.occurred_at) FROM agata.messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id),
      EXISTS (SELECT 1 FROM agata.consents co WHERE co.tenant_id=c.tenant_id AND co.user_id=c.user_id AND co.purpose='chat_history' AND co.revoked_at IS NULL)
    FROM agata.conversations c
    JOIN agata.users au ON au.tenant_id=c.tenant_id AND au.id=c.user_id AND au.source_system='gpoint'
    LEFT JOIN "User" u ON u.id=au.external_id
    JOIN agata.characters ac ON ac.tenant_id=c.tenant_id AND ac.id=c.character_id
    LEFT JOIN "Creator" gp ON gp.id=ac.external_id
    WHERE c.source_system='gpoint'
    ON CONFLICT ("sourceAgataConversationId") DO NOTHING;

    INSERT INTO "GpointLegacyConversationArchive"
      ("id","sourceAgataConversationId","sourceTenantId","userId","creatorId","sourceRelationshipId","sourceScope","sourceScenarioId","sourceSystem","sourceExternalId","startedAt","closedAt","languageCode")
    SELECT c.id::text,c.id::text,c.tenant_id::text,u.id,gp.id,c.relationship_id::text,c.scope::text,
      c.scenario_id::text,c.source_system,c.external_id,c.started_at,c.closed_at,c.language_code
    FROM agata.conversations c
    JOIN agata.users au ON au.tenant_id=c.tenant_id AND au.id=c.user_id AND au.source_system='gpoint'
    JOIN "User" u ON u.id=au.external_id
    LEFT JOIN agata.characters ac ON ac.tenant_id=c.tenant_id AND ac.id=c.character_id
    LEFT JOIN "Creator" gp ON gp.id=ac.external_id
    WHERE c.source_system='gpoint'
    ON CONFLICT ("sourceAgataConversationId") DO NOTHING;

    INSERT INTO "GpointLegacyMessageArchive"
      ("id","sourceAgataMessageId","sourceAgataConversationId","sourceTenantId","userId","creatorId","idempotencyKey","role","contentCiphertext","contentNonce","contentKeyVersion","systemMessageType","systemAction","occurredAt","deletedAt","incomingReadAt","languageCode")
    SELECT m.id::text,m.id::text,m.conversation_id::text,m.tenant_id::text,u.id,gp.id,m.idempotency_key,m.role,
      m.content_ciphertext,m.content_nonce,m.content_key_version,m.system_message_type,m.system_action,
      m.occurred_at,m.deleted_at,m.incoming_read_at,m.language_code
    FROM agata.messages m
    JOIN agata.conversations c ON c.tenant_id=m.tenant_id AND c.id=m.conversation_id AND c.source_system='gpoint'
    JOIN agata.users au ON au.tenant_id=m.tenant_id AND au.id=m.user_id AND au.source_system='gpoint'
    JOIN "User" u ON u.id=au.external_id
    LEFT JOIN agata.characters ac ON ac.tenant_id=m.tenant_id AND ac.id=m.character_id
    LEFT JOIN "Creator" gp ON gp.id=ac.external_id
    ON CONFLICT ("sourceAgataMessageId") DO NOTHING;

    -- Existing source rows may be copied once only. A rerun never overwrites a
    -- changed destination; it verifies exact source identity and aborts on drift.
    IF EXISTS (
      SELECT 1 FROM agata.consents c
      JOIN agata.users au ON au.tenant_id=c.tenant_id AND au.id=c.user_id AND au.source_system='gpoint'
      JOIN "User" u ON u.id=au.external_id
      LEFT JOIN "GpointChatConsent" d ON d."sourceAgataId"=c.id::text
      WHERE c.purpose IN ('chat_history','long_term_memory')
        AND (d."id" IS NULL OR d."sourceTenantId"<>c.tenant_id::text OR d."userId"<>u.id OR d."purpose"<>c.purpose OR d."grantedAt"<>c.granted_at OR d."revokedAt" IS DISTINCT FROM c.revoked_at OR d."policyVersion"<>c.policy_version)
    ) THEN RAISE EXCEPTION 'GPOINT_CHAT_CONSENT_COPY_MISMATCH'; END IF;

    IF EXISTS (
      SELECT 1 FROM agata.age_assurances a
      JOIN agata.users au ON au.tenant_id=a.tenant_id AND au.id=a.user_id AND au.source_system='gpoint'
      JOIN "User" u ON u.id=au.external_id
      LEFT JOIN "GpointAgeAssurance" d ON d."sourceAgataId"=a.id::text
      WHERE d."id" IS NULL OR d."sourceTenantId"<>a.tenant_id::text OR d."userId"<>u.id OR d."ageBand"<>a.age_band OR d."status"<>a.status OR d."method"<>a.method OR d."jurisdiction"<>a.jurisdiction OR d."verifiedAt" IS DISTINCT FROM a.verified_at OR d."assertedAt" IS DISTINCT FROM a.asserted_at OR d."expiresAt" IS DISTINCT FROM a.expires_at OR d."version"<>a.version
    ) THEN RAISE EXCEPTION 'GPOINT_AGE_ASSURANCE_COPY_MISMATCH'; END IF;

    IF EXISTS (
      SELECT 1 FROM agata.ai_disclosure_receipts r
      JOIN agata.users au ON au.tenant_id=r.tenant_id AND au.id=r.user_id AND au.source_system='gpoint'
      JOIN "User" u ON u.id=au.external_id
      LEFT JOIN "GpointAiDisclosureReceipt" d ON d."sourceAgataId"=r.id::text
      WHERE d."id" IS NULL OR d."sourceTenantId"<>r.tenant_id::text OR d."userId"<>u.id OR d."policyProfileId"<>r.policy_profile_id::text OR d."policyVersion"<>r.policy_version OR d."channel"<>r.channel OR d."receiptKey"<>r.receipt_key OR d."displayedAt"<>r.displayed_at OR d."memoryOptIn"<>r.memory_opt_in OR d."memoryOptInAt" IS DISTINCT FROM r.memory_opt_in_at
    ) THEN RAISE EXCEPTION 'GPOINT_DISCLOSURE_RECEIPT_COPY_MISMATCH'; END IF;

    IF EXISTS (
      SELECT 1 FROM agata.regional_policy_profiles p
      WHERE (
        EXISTS (SELECT 1 FROM agata.users au JOIN "User" u ON u.id=au.external_id WHERE au.tenant_id=p.tenant_id AND au.source_system='gpoint')
        OR EXISTS (SELECT 1 FROM agata.ai_disclosure_receipts r JOIN agata.users au ON au.tenant_id=r.tenant_id AND au.id=r.user_id JOIN "User" u ON u.id=au.external_id WHERE r.tenant_id=p.tenant_id AND r.policy_profile_id=p.id AND au.source_system='gpoint')
      ) AND NOT EXISTS (
          SELECT 1 FROM "GpointChatPolicyProfile" d WHERE d."sourceAgataId"=p.id::text
            AND d."sourceTenantId"=p.tenant_id::text AND d.jurisdiction=p.jurisdiction
            AND d.market=p.market AND d.version=p.version AND d.status=p.status
            AND d."effectiveFrom"=p.effective_from AND d."effectiveTo" IS NOT DISTINCT FROM p.effective_to
            AND d.rules=p.rules
        )
    ) THEN RAISE EXCEPTION 'GPOINT_CHAT_POLICY_COPY_MISMATCH'; END IF;

    IF (SELECT count(*) FROM agata.conversations c
        JOIN agata.users au ON au.tenant_id=c.tenant_id AND au.id=c.user_id AND au.source_system='gpoint'
        JOIN "User" u ON u.id=au.external_id WHERE c.source_system='gpoint')
       <> (SELECT count(*) FROM "GpointLegacyConversationFence" f WHERE f.userId IS NOT NULL) THEN
      RAISE EXCEPTION 'GPOINT_LEGACY_HISTORY_FENCE_COUNT_MISMATCH';
    END IF;
    IF EXISTS (
      SELECT 1 FROM agata.conversations c
      JOIN agata.users au ON au.tenant_id=c.tenant_id AND au.id=c.user_id AND au.source_system='gpoint'
      JOIN "User" u ON u.id=au.external_id
      LEFT JOIN agata.characters ac ON ac.tenant_id=c.tenant_id AND ac.id=c.character_id
      LEFT JOIN "Creator" gp ON gp.id=ac.external_id
      LEFT JOIN "GpointLegacyConversationFence" d ON d."sourceAgataConversationId"=c.id::text
      WHERE c.source_system='gpoint' AND (
        d.id IS NULL OR d."sourceTenantId"<>c.tenant_id::text OR d."userId"<>u.id
        OR d."creatorId" IS DISTINCT FROM gp.id OR d."sourceCharacterId" IS DISTINCT FROM ac.external_id
        OR d."firstMessageAt" IS DISTINCT FROM (SELECT min(m.occurred_at) FROM agata.messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id)
        OR d."lastMessageAt" IS DISTINCT FROM (SELECT max(m.occurred_at) FROM agata.messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id)
        OR d."consentWasActive" IS DISTINCT FROM EXISTS (SELECT 1 FROM agata.consents co WHERE co.tenant_id=c.tenant_id AND co.user_id=c.user_id AND co.purpose='chat_history' AND co.revoked_at IS NULL)
        OR d."messageCount"<>(SELECT count(*)::int FROM agata.messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id)
        OR d."memoryCount"<>(SELECT count(*)::int FROM agata.memories mm WHERE mm.tenant_id=c.tenant_id AND mm.user_id=c.user_id AND mm.character_id=c.character_id AND mm.relationship_id=c.relationship_id)
      )
    ) THEN RAISE EXCEPTION 'GPOINT_LEGACY_HISTORY_FENCE_CONTENT_MISMATCH'; END IF;

    IF (SELECT count(*) FROM agata.conversations c
        JOIN agata.users au ON au.tenant_id=c.tenant_id AND au.id=c.user_id AND au.source_system='gpoint'
        JOIN "User" u ON u.id=au.external_id WHERE c.source_system='gpoint')
       <> (SELECT count(*) FROM "GpointLegacyConversationArchive") THEN
      RAISE EXCEPTION 'GPOINT_LEGACY_CONVERSATION_ARCHIVE_COUNT_MISMATCH';
    END IF;
    IF EXISTS (
      SELECT 1 FROM agata.conversations c
      JOIN agata.users au ON au.tenant_id=c.tenant_id AND au.id=c.user_id AND au.source_system='gpoint'
      JOIN "User" u ON u.id=au.external_id
      LEFT JOIN agata.characters ac ON ac.tenant_id=c.tenant_id AND ac.id=c.character_id
      LEFT JOIN "Creator" gp ON gp.id=ac.external_id
      LEFT JOIN "GpointLegacyConversationArchive" d ON d."sourceAgataConversationId"=c.id::text
      WHERE c.source_system='gpoint' AND (
        d.id IS NULL OR d."sourceTenantId"<>c.tenant_id::text OR d."userId"<>u.id
        OR d."creatorId" IS DISTINCT FROM gp.id OR d."sourceRelationshipId"<>c.relationship_id::text
        OR d."sourceScope"<>c.scope::text OR d."sourceScenarioId" IS DISTINCT FROM c.scenario_id::text
        OR d."sourceSystem"<>c.source_system OR d."sourceExternalId" IS DISTINCT FROM c.external_id
        OR d."startedAt"<>c.started_at OR d."closedAt" IS DISTINCT FROM c.closed_at
        OR d."languageCode"<>c.language_code
      )
    ) THEN RAISE EXCEPTION 'GPOINT_LEGACY_CONVERSATION_ARCHIVE_MISMATCH'; END IF;

    IF (SELECT count(*) FROM agata.messages m
        JOIN agata.conversations c ON c.tenant_id=m.tenant_id AND c.id=m.conversation_id AND c.source_system='gpoint'
        JOIN agata.users au ON au.tenant_id=m.tenant_id AND au.id=m.user_id AND au.source_system='gpoint'
        JOIN "User" u ON u.id=au.external_id)
       <> (SELECT count(*) FROM "GpointLegacyMessageArchive") THEN
      RAISE EXCEPTION 'GPOINT_LEGACY_MESSAGE_ARCHIVE_COUNT_MISMATCH';
    END IF;
    IF EXISTS (
      SELECT 1 FROM agata.messages m
      JOIN agata.conversations c ON c.tenant_id=m.tenant_id AND c.id=m.conversation_id AND c.source_system='gpoint'
      JOIN agata.users au ON au.tenant_id=m.tenant_id AND au.id=m.user_id AND au.source_system='gpoint'
      JOIN "User" u ON u.id=au.external_id
      LEFT JOIN agata.characters ac ON ac.tenant_id=m.tenant_id AND ac.id=m.character_id
      LEFT JOIN "Creator" gp ON gp.id=ac.external_id
      LEFT JOIN "GpointLegacyMessageArchive" d ON d."sourceAgataMessageId"=m.id::text
      WHERE d.id IS NULL OR d."sourceAgataConversationId"<>m.conversation_id::text
        OR d."sourceTenantId"<>m.tenant_id::text OR d."userId"<>u.id
        OR d."creatorId" IS DISTINCT FROM gp.id OR d."idempotencyKey"<>m.idempotency_key
        OR d.role<>m.role OR d."contentCiphertext" IS DISTINCT FROM m.content_ciphertext
        OR d."contentNonce" IS DISTINCT FROM m.content_nonce
        OR d."contentKeyVersion" IS DISTINCT FROM m.content_key_version
        OR d."systemMessageType" IS DISTINCT FROM m.system_message_type
        OR d."systemAction" IS DISTINCT FROM m.system_action OR d."occurredAt"<>m.occurred_at
        OR d."deletedAt" IS DISTINCT FROM m.deleted_at OR d."incomingReadAt" IS DISTINCT FROM m.incoming_read_at
        OR d."languageCode"<>m.language_code
    ) THEN RAISE EXCEPTION 'GPOINT_LEGACY_MESSAGE_ARCHIVE_MISMATCH'; END IF;
  END IF;
END $$;
