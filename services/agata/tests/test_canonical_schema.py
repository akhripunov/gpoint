from app.db.models import Base

CANONICAL_TABLES = frozenset({
    "tenants", "users", "characters", "character_profile_versions",
    "user_character_settings", "consents", "retention_policies", "roleplay_scenarios",
    "relationships", "conversations", "conversation_segments", "messages",
    "memory_control_state", "memories", "memory_sources", "memory_dependencies",
    "suppression_rules", "explicit_command_receipts", "shadow_memory_candidates",
    "shadow_candidate_labels", "autowrite_release_policies", "candidate_promotion_receipts",
    "retrieval_runs", "retrieval_candidates", "response_route_policies", "response_runs",
    "response_memory_refs", "memory_snapshots", "shared_history_episodes",
    "memory_entities", "memory_relations", "memory_graph_evidence", "outbox_jobs",
})


def test_canonical_agata_domain_schema_is_exactly_33_tables():
    model_tables = {table.name for table in Base.metadata.tables.values()}
    assert len(CANONICAL_TABLES) == 33
    assert CANONICAL_TABLES <= model_tables
    assert {"memory_entities", "memory_relations", "memory_graph_evidence"} <= model_tables
