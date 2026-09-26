from app.db.models import Base


def test_core_tables_are_owned_only_by_agata_schema() -> None:
    tables = Base.metadata.tables
    assert len(tables) >= 30
    assert all(table.schema == "agata" for table in tables.values())
    assert {
        "users",
        "characters",
        "character_profile_versions",
        "conversations",
        "messages",
        "memories",
        "outbox_jobs",
        "response_runs",
        "relationships",
    } <= {table.name for table in tables.values()}


def test_foreign_keys_resolve_inside_agata_without_gpoint_schema_links() -> None:
    for table in Base.metadata.tables.values():
        for constraint in table.foreign_key_constraints:
            for element in constraint.elements:
                assert element.column.table.schema == "agata"
                assert element.column.table in Base.metadata.tables.values()


def test_model_mapping_has_no_foreign_key_to_gpoint_user_or_creator() -> None:
    user = Base.metadata.tables["agata.users"]
    character = Base.metadata.tables["agata.characters"]
    assert {"source_system", "external_id"} <= set(user.c.keys())
    assert {"source_system", "external_id"} <= set(character.c.keys())
    assert all(
        element.column.table.schema == "agata"
        for table in (user, character)
        for constraint in table.foreign_key_constraints
        for element in constraint.elements
    )


def test_message_response_and_profile_idempotency_constraints_exist() -> None:
    messages = Base.metadata.tables["agata.messages"]
    runs = Base.metadata.tables["agata.response_runs"]
    profiles = Base.metadata.tables["agata.character_profile_versions"]
    relationships = Base.metadata.tables["agata.relationships"]

    def unique_columns(table):
        return {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }

    assert ("tenant_id", "idempotency_key") in unique_columns(messages)
    assert ("tenant_id", "user_id", "request_fingerprint") in unique_columns(runs)
    assert ("tenant_id", "character_id", "version") in unique_columns(profiles)
    assert (
        "tenant_id", "user_id", "character_id", "scope", "scenario_key"
    ) in unique_columns(relationships)
