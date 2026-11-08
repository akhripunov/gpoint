#!/usr/bin/env python3
"""Exercise GPoint policy transfer and archive non-transfer on isolated PostgreSQL 16."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_NAME = "20261105100000_gpoint_chat_policy_and_history_fences"
MIGRATION = ROOT / f"packages/db/prisma/migrations/{MIGRATION_NAME}/migration.sql"
DEFAULT_IMAGE = "pgvector/pgvector:0.8.6-pg16"
GPOINT_TABLES = (
    "GpointChatPolicyProfile", "GpointChatConsent", "GpointAgeAssurance",
    "GpointAiDisclosureReceipt", "GpointLegacyConversationFence",
    "GpointLegacyConversationArchive", "GpointLegacyMessageArchive",
)
ARCHIVE_TABLES = ("GpointLegacyConversationFence", "GpointLegacyConversationArchive", "GpointLegacyMessageArchive")

FIXTURE = r"""
CREATE TABLE "User" (id TEXT PRIMARY KEY);
CREATE TABLE "Creator" (id TEXT PRIMARY KEY);
INSERT INTO "User" VALUES ('gpoint-user-test');
INSERT INTO "User" VALUES ('gpoint-user-test-newer-denial');
INSERT INTO "Creator" VALUES ('gpoint-creator-test');
CREATE SCHEMA agata;
CREATE TABLE agata.synthetic_user_state (id integer PRIMARY KEY, payload text NOT NULL);
INSERT INTO agata.synthetic_user_state VALUES (1, 'synthetic-only');
CREATE TABLE agata.users (id UUID, tenant_id UUID, source_system TEXT, external_id TEXT);
CREATE TABLE agata.regional_policy_profiles (id UUID, tenant_id UUID, jurisdiction TEXT, market TEXT,
  version INTEGER, status TEXT, effective_from TIMESTAMPTZ, effective_to TIMESTAMPTZ, rules JSONB);
CREATE TABLE agata.consents (id UUID, tenant_id UUID, user_id UUID, purpose TEXT, granted_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ, policy_version TEXT);
CREATE TABLE agata.age_assurances (id UUID, tenant_id UUID, user_id UUID, age_band TEXT, status TEXT,
  method TEXT, jurisdiction TEXT, verified_at TIMESTAMPTZ, asserted_at TIMESTAMPTZ, expires_at TIMESTAMPTZ,
  version INTEGER, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE agata.ai_disclosure_receipts (id UUID, tenant_id UUID, user_id UUID, policy_profile_id UUID,
  policy_version INTEGER, channel TEXT, receipt_key TEXT, displayed_at TIMESTAMPTZ, memory_opt_in BOOLEAN,
  memory_opt_in_at TIMESTAMPTZ);
CREATE TABLE agata.characters (id UUID, tenant_id UUID, external_id TEXT);
CREATE TABLE agata.conversations (id UUID, tenant_id UUID, source_system TEXT, external_id TEXT,
  relationship_id UUID, user_id UUID, character_id UUID, scope TEXT, scenario_id UUID,
  started_at TIMESTAMPTZ, closed_at TIMESTAMPTZ, language_code TEXT);
CREATE TABLE agata.messages (id UUID, tenant_id UUID, conversation_id UUID, user_id UUID,
  character_id UUID, idempotency_key TEXT, role TEXT, content_ciphertext BYTEA, content_nonce BYTEA,
  content_key_version TEXT, system_message_type TEXT, system_action TEXT, occurred_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ, incoming_read_at TIMESTAMPTZ, language_code TEXT);
CREATE TABLE agata.memories (id UUID, tenant_id UUID, user_id UUID, character_id UUID, relationship_id UUID);
INSERT INTO agata.users VALUES
 ('10000000-0000-4000-8000-000000000001','30000000-0000-4000-8000-000000000003','gpoint','gpoint-user-test'),
 ('10000000-0000-4000-8000-000000000011','30000000-0000-4000-8000-000000000003','gpoint','gpoint-user-test-newer-denial');
INSERT INTO agata.regional_policy_profiles VALUES
 ('40000000-0000-4000-8000-000000000004','30000000-0000-4000-8000-000000000003','GLOBAL','synthetic',1,'active',now()-interval '1 day',NULL,
  '{"ai_disclosure_required":true,"self_attestation_allowed":false,"adult_only_features":["restricted"]}'),
 ('40000000-0000-4000-8000-000000000014','30000000-0000-4000-8000-000000000003','OTHER','synthetic-other',2,'active',now()-interval '2 days',now()+interval '1 day',
  '{"ai_disclosure_required":false,"self_attestation_allowed":true,"adult_only_features":[]}');
INSERT INTO agata.consents VALUES
 ('50000000-0000-4000-8000-000000000005','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','chat_history',now()-interval '2 hours',NULL,'1'),
 ('50000000-0000-4000-8000-000000000015','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','chat_history',now()-interval '1 hour',NULL,'1'),
 ('50000000-0000-4000-8000-000000000025','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','long_term_memory',now()-interval '3 hours',now()-interval '30 minutes','1'),
 ('50000000-0000-4000-8000-000000000035','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','long_term_memory',now()-interval '30 minutes',NULL,'1');
INSERT INTO agata.age_assurances
  (id,tenant_id,user_id,age_band,status,method,jurisdiction,verified_at,asserted_at,expires_at,version,created_at) VALUES
 ('60000000-0000-4000-8000-000000000006','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','adult','verified','document','GLOBAL',now(),NULL,NULL,1,now()-interval '2 days'),
 ('60000000-0000-4000-8000-000000000016','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','adult','expired','document','US',now()-interval '400 days',NULL,now()-interval '1 day',5,now()),
 ('60000000-0000-4000-8000-000000000026','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000011','adult','verified','document','GLOBAL',now()-interval '10 days',NULL,NULL,9,now()-interval '10 days'),
 ('60000000-0000-4000-8000-000000000036','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000011','adult','expired','document','US',now()-interval '10 days',NULL,now()-interval '1 day',2,now());
INSERT INTO agata.ai_disclosure_receipts VALUES
 ('70000000-0000-4000-8000-000000000007','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','40000000-0000-4000-8000-000000000004',1,'web','synthetic-receipt',now(),true,now());
INSERT INTO agata.characters VALUES ('80000000-0000-4000-8000-000000000008','30000000-0000-4000-8000-000000000003','gpoint-creator-test');
INSERT INTO agata.conversations VALUES
 ('90000000-0000-4000-8000-000000000009','30000000-0000-4000-8000-000000000003','gpoint','legacy-thread-1',
  'a0000000-0000-4000-8000-00000000000a','10000000-0000-4000-8000-000000000001','80000000-0000-4000-8000-000000000008','real_world',NULL,
  now(),NULL,'ru');
INSERT INTO agata.messages VALUES
 ('b0000000-0000-4000-8000-00000000000b','30000000-0000-4000-8000-000000000003','90000000-0000-4000-8000-000000000009',
  '10000000-0000-4000-8000-000000000001','80000000-0000-4000-8000-000000000008','legacy-msg-1','assistant',
  decode('00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff','hex'),
  decode('00112233445566778899aabb','hex'),'aead-v1',NULL,NULL,now(),NULL,NULL,'ru');
"""


def docker(*args: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], input=input_text, text=True,
                            capture_output=True, check=False, timeout=90)
    if check and result.returncode:
        raise RuntimeError(f"docker {' '.join(args[:3])} failed: {result.stderr[-1500:]}")
    return result


def psql(container: str, sql: str, *, database: str = "gpoint",
         check: bool = True) -> subprocess.CompletedProcess[str]:
    return docker("exec", "-i", container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres",
                  "-d", database, "-At", input_text=sql, check=check)


def run_exact_prisma_image(app_image: str, candidate_sha: str, network: str, container: str) -> None:
    if (not re.fullmatch(r"ghcr\.io/akhripunov/gpoint-runtime@sha256:[0-9a-f]{64}", app_image)
            or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha)):
        raise RuntimeError("GPoint migration E2E requires exact candidate image digest and SHA")
    revision = docker("image", "inspect", app_image, "--format",
                      '{{index .Config.Labels "org.opencontainers.image.revision"}}').stdout.strip()
    if revision != candidate_sha:
        raise RuntimeError("GPoint migration E2E image revision differs from exact candidate")
    psql(container, "CREATE DATABASE gpoint_prisma", database="postgres")
    dsn = f"postgresql://postgres:isolated-test-only@{container}:5432/gpoint_prisma?schema=public"
    docker("run", "--rm", "--network", network, "-e", f"DATABASE_URL={dsn}",
           "--workdir", "/app", "--entrypoint", "pnpm", app_image,
           "--filter", "@gpoint/db", "exec", "prisma", "migrate", "deploy")
    expected_checksum = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    ledger = psql(container, f"""
      SELECT count(*) || '|' || coalesce(max(checksum),'')
      FROM public._prisma_migrations
      WHERE migration_name='{MIGRATION_NAME}' AND finished_at IS NOT NULL AND rolled_back_at IS NULL;
    """, database="gpoint_prisma").stdout.strip()
    if ledger != "1|" + expected_checksum:
        raise RuntimeError("exact-image Prisma migrate ledger checksum mismatch")
    print("GPOINT_EXACT_IMAGE_PRISMA_MIGRATION_E2E=PASS checksum_match=true")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--app-image")
    parser.add_argument("--candidate-sha")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9._/:@+-]+", args.image) or args.image.endswith(":latest"):
        raise SystemExit("PostgreSQL test image must be an explicit non-latest image reference")
    docker("info")
    suffix = f"{os.getpid()}-{int(time.time())}"
    network, container = f"gpoint-chat-schema-{suffix}", f"gpoint-chat-schema-db-{suffix}"
    created_network = created_container = False
    try:
        docker("network", "create", "--internal", network)
        created_network = True
        docker("run", "-d", "--name", container, "--network", network,
               "-e", "POSTGRES_PASSWORD=isolated-test-only", "-e", "POSTGRES_USER=postgres",
               "-e", "POSTGRES_DB=gpoint", args.image)
        created_container = True
        for _ in range(60):
            if docker("exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "gpoint", check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("isolated PostgreSQL did not become ready")

        # Install one GPoint global policy. Legacy regional policy and disclosure
        # rows remain in AGATA; they cannot select or satisfy the current policy.
        psql(container, FIXTURE)
        if args.app_image:
            run_exact_prisma_image(args.app_image, args.candidate_sha or "", network, container)
        migration = MIGRATION.read_text(encoding="utf-8")
        psql(container, migration)
        psql(container, migration)  # additive DDL must be safely repeatable
        source_counts = psql(container, "SELECT (SELECT count(*) FROM agata.users WHERE source_system='gpoint') || '|' || (SELECT count(*) FROM agata.regional_policy_profiles) || '|' || (SELECT count(*) FROM agata.consents) || '|' || (SELECT count(*) FROM agata.age_assurances) || '|' || (SELECT count(*) FROM agata.ai_disclosure_receipts) || '|' || (SELECT count(*) FROM agata.conversations) || '|' || (SELECT count(*) FROM agata.messages) || '|' || (SELECT count(*) FROM agata.memories)").stdout.strip()
        policy_counts = psql(container, "SELECT count(*) || '|' || max(\"version\") || '|' || min(\"termsVersion\") || '|' || min(\"privacyVersion\") || '|' || min((\"rules\"->>'minimum_age')::integer) || '|' || bool_and(\"rules\"->>'ai_disclosure_required'='true')::text FROM \"GpointChatPolicyProfile\"").stdout.strip()
        user_state_counts = psql(container, "SELECT (SELECT count(*) FROM \"GpointChatConsent\") || '|' || (SELECT count(*) FROM \"GpointAgeAssurance\") || '|' || (SELECT count(*) FROM \"GpointAiDisclosureReceipt\")").stdout.strip()
        active_chat_consents = psql(container, "SELECT count(*) FROM \"GpointChatConsent\" WHERE purpose='chat_history' AND \"revokedAt\" IS NULL").stdout.strip()
        active_memory_consents = psql(container, "SELECT count(*) FROM \"GpointChatConsent\" WHERE purpose='long_term_memory' AND \"revokedAt\" IS NULL").stdout.strip()
        age_state = psql(container, 'SELECT string_agg("ageBand" || \'|\' || status || \'|\' || "version", \'|\' ORDER BY "userId") FROM "GpointAgeAssurance"').stdout.strip()
        archive_counts = psql(container, "SELECT " + " || '|' || ".join(
            f'(SELECT count(*) FROM "{table}")' for table in ARCHIVE_TABLES)).stdout.strip()
        if source_counts != "2|2|4|4|1|1|1|0":
            raise RuntimeError(f"AGATA source counts changed or fixture mismatch: {source_counts!r}")
        if policy_counts != "1|1|gpoint-terms-v1|gpoint-privacy-v1|18|true":
            raise RuntimeError(f"GPoint policy definition copy mismatch: {policy_counts!r}")
        if user_state_counts != "4|2|0" or active_chat_consents != "1" or active_memory_consents != "0" or age_state != "adult|verified|1|adult|expired|2":
            raise RuntimeError(f"GPoint consent/age/disclosure state copy mismatch: {user_state_counts!r}")
        if archive_counts != "0|0|0":
            raise RuntimeError(f"AGATA conversation, message, or memory archive was copied: {archive_counts!r}")
        print("GPOINT_AGATA_SOURCE_STATE_UNCHANGED=PASS")
        print("GPOINT_GLOBAL_POLICY=PASS version=1 terms=gpoint-terms-v1 privacy=gpoint-privacy-v1")
        print("GPOINT_LATEST_CONSENT_AND_AGE_WINS=PASS consent_rows=4 active_chat_rows=1 active_memory_rows=0 age=latest-event-state-wins")
        print("GPOINT_NEW_DISCLOSURE_REQUIRED=PASS legacy_receipt_stays_in_agata=true")
        print("GPOINT_AGATA_HISTORY_AND_MEMORY_NOT_COPIED=PASS")
        print("GPOINT_CHAT_SCHEMA_ADDITIVE_RERUN=PASS")
        return 0
    finally:
        if created_container:
            docker("rm", "-f", container, check=False)
        if created_network:
            docker("network", "rm", network, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
