import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_NAME = "20261105100000_gpoint_chat_policy_and_history_fences"
MIGRATION = ROOT / "packages/db/prisma/migrations" / MIGRATION_NAME / "migration.sql"
ALLOWLIST = ROOT / "packages/db/prisma/production-approved-migrations.json"
POSTGRES_REHEARSAL = ROOT / "tests/integration/gpoint-chat-schema-docker.py"


def test_gpoint_cutover_installs_one_global_policy_without_reading_regional_policy():
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "create table if not exists \"gpointchatpolicyprofile\"" in sql
    assert "agata.regional_policy_profiles" not in sql
    assert "'gpoint-terms-v1','gpoint-privacy-v1','active'" in sql
    assert '"minimum_age":18' in sql
    assert '"ai_disclosure_required":true' in sql
    assert 'from agata.consents' in sql
    assert 'from agata.age_assurances' in sql
    assert 'from agata.ai_disclosure_receipts' not in sql
    for table in ("agata.conversations", "agata.messages", "agata.memories"):
        assert f"from {table}" not in sql
    for table in (
        'insert into "gpointlegacyconversationfence"',
        'insert into "gpointlegacyconversationarchive"',
        'insert into "gpointlegacymessagearchive"',
    ):
        assert table not in sql


def test_schema_only_pilot_migration_is_the_exact_production_allowlisted_checksum():
    manifest = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    expected = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()

    assert manifest["migrations"].get(MIGRATION_NAME) == expected


def test_postgres_rehearsal_checks_global_policy_legacy_state_and_archive_non_transfer():
    rehearsal = POSTGRES_REHEARSAL.read_text(encoding="utf-8")

    assert "GPOINT_AGATA_SOURCE_STATE_UNCHANGED=PASS" in rehearsal
    assert "GPOINT_GLOBAL_POLICY=PASS version=1 terms=gpoint-terms-v1 privacy=gpoint-privacy-v1" in rehearsal
    assert "GPOINT_LATEST_CONSENT_AND_AGE_WINS=PASS consent_rows=4 active_chat_rows=1 active_memory_rows=0 age=latest-created-state-wins" in rehearsal
    assert "GPOINT_NEW_DISCLOSURE_REQUIRED=PASS legacy_receipt_stays_in_agata=true" in rehearsal
    assert "GPOINT_AGATA_HISTORY_AND_MEMORY_NOT_COPIED=PASS" in rehearsal


def test_global_self_attestation_version_accounts_for_legacy_non_global_age_rows():
    source = (ROOT / "apps/api/src/gpoint-chat-store.ts").read_text()
    assert "where: { sourceTenantId: tenantId, userId }, orderBy: [{ version: 'desc'" in source
    assert "jurisdiction" not in source


def test_gpoint_messages_never_reuses_legacy_agata_jurisdiction():
    source = (ROOT / "apps/api/src/messages.ts").read_text()
    assert "current.jurisdiction" not in source
    assert source.count("jurisdiction: 'GLOBAL'") == 2


def test_consent_migration_keeps_only_the_latest_legacy_active_row_current():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert re.search(
        r'row_number\(\) over \(partition by c\.tenant_id,c\.user_id,c\.purpose\s+'
        r'order by coalesce\(c\.revoked_at,c\.granted_at\) desc,\s*case when c\.revoked_at is null then 1 else 0 end,c\.granted_at desc,c\.id desc\) as latest_event_rank',
        sql,
    )
    assert re.search(r'when revoked_at is null and latest_event_rank > 1 then latest_event_at', sql)
    assert re.search(r'order by u\.id,a\.created_at desc,coalesce\(a\.verified_at,a\.asserted_at\) desc nulls last,a\.version desc,a\.id desc', sql)
