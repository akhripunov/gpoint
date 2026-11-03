#!/usr/bin/env python3
"""Run the GPoint consent and encrypted legacy-history migration on isolated PostgreSQL."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "packages/db/prisma/migrations/20261103140000_gpoint_chat_policy_and_agata_state_copy/migration.sql"
DEFAULT_IMAGE = "pgvector/pgvector:0.8.6-pg16"

FIXTURE = r"""
CREATE TABLE "User" (id TEXT PRIMARY KEY);
CREATE TABLE "Creator" (id TEXT PRIMARY KEY);
INSERT INTO "User" VALUES ('gpoint-user-test');
INSERT INTO "Creator" VALUES ('gpoint-creator-test');
CREATE SCHEMA agata;
CREATE TABLE agata.users (id UUID, tenant_id UUID, source_system TEXT, external_id TEXT);
CREATE TABLE agata.regional_policy_profiles (id UUID, tenant_id UUID, jurisdiction TEXT, market TEXT,
  version INTEGER, status TEXT, effective_from TIMESTAMP, effective_to TIMESTAMP, rules JSONB);
CREATE TABLE agata.consents (id UUID, tenant_id UUID, user_id UUID, purpose TEXT, granted_at TIMESTAMP,
  revoked_at TIMESTAMP, policy_version TEXT);
CREATE TABLE agata.age_assurances (id UUID, tenant_id UUID, user_id UUID, age_band TEXT, status TEXT,
  method TEXT, jurisdiction TEXT, verified_at TIMESTAMP, asserted_at TIMESTAMP, expires_at TIMESTAMP,
  version INTEGER);
CREATE TABLE agata.ai_disclosure_receipts (id UUID, tenant_id UUID, user_id UUID, policy_profile_id UUID,
  policy_version INTEGER, channel TEXT, receipt_key TEXT, displayed_at TIMESTAMP, memory_opt_in BOOLEAN,
  memory_opt_in_at TIMESTAMP);
CREATE TABLE agata.characters (id UUID, tenant_id UUID, external_id TEXT);
CREATE TABLE agata.conversations (id UUID, tenant_id UUID, source_system TEXT, external_id TEXT,
  relationship_id UUID, user_id UUID, character_id UUID, scope TEXT, scenario_id UUID,
  started_at TIMESTAMP, closed_at TIMESTAMP, language_code TEXT);
CREATE TABLE agata.messages (id UUID, tenant_id UUID, conversation_id UUID, user_id UUID,
  character_id UUID, idempotency_key TEXT, role TEXT, content_ciphertext BYTEA, content_nonce BYTEA,
  content_key_version TEXT, system_message_type TEXT, system_action TEXT, occurred_at TIMESTAMP,
  deleted_at TIMESTAMP, incoming_read_at TIMESTAMP, language_code TEXT);
CREATE TABLE agata.memories (id UUID, tenant_id UUID, user_id UUID, character_id UUID, relationship_id UUID);
INSERT INTO agata.users VALUES ('10000000-0000-4000-8000-000000000001','30000000-0000-4000-8000-000000000003','gpoint','gpoint-user-test');
INSERT INTO agata.regional_policy_profiles VALUES
 ('40000000-0000-4000-8000-000000000004','30000000-0000-4000-8000-000000000003','GLOBAL','synthetic',1,'active','2026-01-01 00:00:00',NULL,'{"allowSelfAttestation":true}');
INSERT INTO agata.consents VALUES
 ('50000000-0000-4000-8000-000000000005','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','chat_history','2026-01-02 00:00:00',NULL,'1');
INSERT INTO agata.age_assurances VALUES
 ('60000000-0000-4000-8000-000000000006','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','adult','verified','document','GLOBAL','2026-01-02 00:00:00',NULL,NULL,1);
INSERT INTO agata.ai_disclosure_receipts VALUES
 ('70000000-0000-4000-8000-000000000007','30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','40000000-0000-4000-8000-000000000004',1,'web','receipt-v1','2026-01-02 00:00:00',true,'2026-01-02 00:00:00');
INSERT INTO agata.characters VALUES ('80000000-0000-4000-8000-000000000008','30000000-0000-4000-8000-000000000003','gpoint-creator-test');
INSERT INTO agata.conversations VALUES
 ('90000000-0000-4000-8000-000000000009','30000000-0000-4000-8000-000000000003','gpoint','legacy-thread-1',
  'a0000000-0000-4000-8000-00000000000a','10000000-0000-4000-8000-000000000001','80000000-0000-4000-8000-000000000008','real_world',NULL,
  '2026-01-03 00:00:00',NULL,'ru');
INSERT INTO agata.messages VALUES
 ('b0000000-0000-4000-8000-00000000000b','30000000-0000-4000-8000-000000000003','90000000-0000-4000-8000-000000000009',
  '10000000-0000-4000-8000-000000000001','80000000-0000-4000-8000-000000000008','legacy-msg-1','assistant',
  decode('00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff','hex'),
  decode('00112233445566778899aabb','hex'),'aead-v1',NULL,NULL,'2026-01-03 00:01:00',NULL,NULL,'ru');
"""


def docker(*args: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], input=input_text, text=True,
                            capture_output=True, check=False, timeout=90)
    if check and result.returncode:
        raise RuntimeError(f"docker {' '.join(args[:3])} failed: {result.stderr[-1500:]}")
    return result


def psql(container: str, sql: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return docker("exec", "-i", container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres",
                  "-d", "gpoint", "-At", input_text=sql, check=check)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9._/:@+-]+", args.image) or args.image.endswith(":latest"):
        raise SystemExit("PostgreSQL test image must be an explicit non-latest image reference")
    docker("info")
    suffix = f"{os.getpid()}-{int(time.time())}"
    network, container = f"gpoint-chat-migration-{suffix}", f"gpoint-chat-migration-db-{suffix}"
    created_network = created_container = False
    try:
        docker("network", "create", "--internal", network)
        created_network = True
        docker("run", "-d", "--name", container, "--network", network,
               "-e", "POSTGRES_PASSWORD=isolated-test-only", "-e", "POSTGRES_USER=postgres",
               "-e", "POSTGRES_DB=gpoint", args.image)
        created_container = True
        for _ in range(60):
            probe = docker("exec", container, "pg_isready", "-U", "postgres", "-d", "gpoint", check=False)
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("isolated PostgreSQL did not become ready")
        psql(container, FIXTURE)
        migration = MIGRATION.read_text(encoding="utf-8")
        result = psql(container, migration)
        checks = psql(container, """
          SELECT (SELECT count(*) FROM "GpointChatConsent") || '|' ||
                 (SELECT count(*) FROM "GpointLegacyConversationFence") || '|' ||
                 (SELECT count(*) FROM "GpointLegacyConversationArchive") || '|' ||
                 (SELECT count(*) FROM "GpointLegacyMessageArchive") || '|' ||
                 (SELECT encode("contentCiphertext",'hex') FROM "GpointLegacyMessageArchive") || '|' ||
                 (SELECT "contentKeyVersion" FROM "GpointLegacyMessageArchive");
        """).stdout.strip()
        expected = "1|1|1|1|00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff|aead-v1"
        if checks != expected:
            raise RuntimeError(f"consent/history archive mismatch: got {checks!r}")

        psql(container, 'UPDATE "GpointLegacyConversationFence" SET "firstMessageAt"=NULL')
        drift = psql(container, migration, check=False)
        if drift.returncode == 0 or "GPOINT_LEGACY_HISTORY_FENCE_CONTENT_MISMATCH" not in drift.stderr:
            raise RuntimeError("rerun did not reject altered legacy fence state")
        psql(container, 'UPDATE "GpointLegacyConversationFence" SET "firstMessageAt"=TIMESTAMP \'2026-01-03 00:01:00\'')
        psql(container, 'UPDATE "GpointLegacyMessageArchive" SET "contentCiphertext"=decode(\'ff\',\'hex\')')
        drift = psql(container, migration, check=False)
        if drift.returncode == 0 or "GPOINT_LEGACY_MESSAGE_ARCHIVE_MISMATCH" not in drift.stderr:
            raise RuntimeError("rerun did not reject altered encrypted message archive")
        print("GPOINT_AGATA_CONSENT_AND_ENCRYPTED_HISTORY_MIGRATION_E2E=PASS")
        print("GPOINT_LEGACY_ARCHIVE_SOURCE_PLAINTEXT_COPIED=NO")
        print("GPOINT_LEGACY_ARCHIVE_DRIFT_GUARDS=PASS")
        return 0
    finally:
        if created_container:
            docker("rm", "-f", container, check=False)
        if created_network:
            docker("network", "rm", network, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
