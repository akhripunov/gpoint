from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
TRIGGERS = WORKFLOW.get("on", WORKFLOW.get(True, {}))


def steps_for(job):
    return WORKFLOW["jobs"][job]["steps"]


def test_native_deploy_provisions_only_bootstrap_and_rehearses_exact_eros_sqlx_transition():
    deploy = steps_for("deploy")
    names = [step.get("name", "") for step in deploy]
    gate = next(step for step in deploy if step.get("name", "").startswith("Rehearse exact Eros SQLx"))
    body = gate["run"]
    assert gate["if"] == "steps.component_plan.outputs.deploy_eros == 'true'"
    assert names.index(gate["name"]) < names.index("Deploy exact immutable images with stock Kamal")
    assert "--provision-database" in body and "--write-native-engine-env" in body
    assert "if [[ \"$EROS_BOOTSTRAP\" == true ]]" in body
    assert "--mode prepare" in body and "--mode apply" in body
    assert "ghcr.io/akhripunov/gpoint-eros@$EROS_DIGEST" in body
    assert "docker pull --platform linux/amd64" in body


def test_sqlx_helper_checks_acl_ledger_backup_restore_and_never_downgrades_live_database():
    source = (ROOT / "scripts/eros-native-database.py").read_text()
    assert "public._sqlx_migrations" in source
    assert "runtimeForeign" in source and "migratorForeign" in source and "otherConnect" in source
    assert "GRANT CONNECT ON DATABASE {quoted} TO eros_runtime, eros_migrator" in source
    assert "pg_dump" in source and "pg_restore" in source
    assert "--exit-on-error" in source and "_migration_delta" in source
    assert 'EROS_SCHEMA_APPLY=SKIP reason=no_pending_migrations' in source
    assert 'EROS_SCHEMA_APPLY=PASS MIGRATIONS=' in source
    assert 'stock Eros transition inserts a migration behind the live SQLx head' in source
    assert "DROP DATABASE" in source
    assert "backup.read_bytes()" not in source
    assert "_database_facts(env[\"EROS_DATABASE_IDENTITY\"], restore_db)" in source
    assert "pg_restore --exit-on-error --clean" not in source
    rollback_check = source.split('if mode == "verify-unchanged":', 1)[1].split(
        'if (plan.get("schema")', 1)[0]
    assert "_run_migrator(" not in rollback_check


def test_eros_image_rollback_is_opt_in_rehearsed_before_apply_and_verifies_database_unchanged():
    dispatch = TRIGGERS["workflow_dispatch"]["inputs"]
    assert dispatch["rollback_eros"]["default"] is False
    rollback = steps_for("rollback")
    names = [step.get("name", "") for step in rollback]
    retained = names.index("Verify retained Eros rollback containers")
    compatibility = names.index("Verify Eros rollback image against an isolated current database restore")
    apply = names.index("Roll back consumers and then APIs with stock Kamal")
    runtime = names.index("Verify rollback Runtime and preserve databases before SUCCESS")
    assert retained < compatibility < apply < runtime
    compatibility_step = rollback[compatibility]
    assert compatibility_step["if"] == "inputs.rollback_eros == true"
    assert "--mode rollback-check" in compatibility_step["run"]
    apply_step = rollback[apply]["run"]
    assert 'if [[ "$ROLLBACK_EROS" == true ]]' in apply_step
    runtime_step = rollback[runtime]["run"]
    assert "--mode verify-unchanged" in runtime_step
    assert "EROS_ROLLBACK_RUNTIME_VERIFY=PASS" in runtime_step
    assert not any("Refuse rollback across an installed Eros topology" in name for name in names)


def test_eros_database_changes_do_not_run_for_core_only_deploy_or_rollback():
    deploy = steps_for("deploy")
    gate = next(step for step in deploy if step.get("name", "").startswith("Rehearse exact Eros SQLx"))
    assert "deploy_eros == 'true'" in gate["if"]
    rollback = steps_for("rollback")
    compat = next(step for step in rollback if step.get("name", "").startswith("Verify Eros rollback image"))
    assert compat["if"] == "inputs.rollback_eros == true"
