from pathlib import Path
import importlib.util
import os
import subprocess
import sys
import tempfile
import tomllib
import hashlib

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/gpoint-release.yml").read_text())
TRIGGERS = WORKFLOW.get("on", WORKFLOW.get(True, {}))
ROLLBACK_WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/gpoint-release.yml").read_text())
ROLLBACK_TRIGGERS = ROLLBACK_WORKFLOW.get("on", ROLLBACK_WORKFLOW.get(True, {}))
DB_SPEC = importlib.util.spec_from_file_location("eros_native_database", ROOT / "scripts/eros-native-database.py")
DB = importlib.util.module_from_spec(DB_SPEC)
DB_SPEC.loader.exec_module(DB)


def steps_for(job):
    if job == "rollback":
        return ROLLBACK_WORKFLOW["jobs"]["deploy"]["steps"]
    return WORKFLOW["jobs"][job]["steps"]


def test_schema_fingerprint_uses_stable_pg_dump_restrict_key_without_ignoring_ddl(monkeypatch):
    calls = []
    ddl = ["CREATE TABLE engine.example (id bigint);\n"]

    def dump(argv, **kwargs):
        calls.append(argv)
        # PostgreSQL 16.15 emits a random restrict guard by default.
        command = argv[argv.index("-ec") + 1]
        key = "GpointSchemaFingerprint" if "--restrict-key=GpointSchemaFingerprint" in command else str(len(calls))
        return subprocess.CompletedProcess(argv, 0, "\\restrict " + key + "\n" + ddl[0] + "\\unrestrict " + key + "\n")

    monkeypatch.setattr(DB, "_run", dump)
    original = DB._schema_fingerprint("eros")
    assert original == DB._schema_fingerprint("eros_check_123_1")
    ddl[0] = "CREATE TABLE engine.example (id text);\n"
    assert original != DB._schema_fingerprint("eros")
    assert all("--schema-only --no-owner --no-privileges" in argv[argv.index("-ec") + 1] for argv in calls)


def test_native_deploy_provisions_only_bootstrap_and_rehearses_exact_eros_sqlx_transition():
    deploy = steps_for("deploy")
    names = [step.get("name", "") for step in deploy]
    gate = next(step for step in deploy if step.get("name", "").startswith("Rehearse exact Eros SQLx"))
    body = gate["run"]
    assert "(steps.component_plan.outputs.deploy_eros == 'true') && inputs.mode != 'rollback'" in gate["if"]
    assert names.index(gate["name"]) < names.index("Deploy exact immutable images with stock Kamal")
    assert "--provision-database" in body and "--write-native-engine-env" in body and "--write-native-api-env" in body
    assert "if [[ \"$EROS_BOOTSTRAP\" == true ]]" in body
    assert "--mode prepare" in body and "--mode apply" in body
    assert "ghcr.io/akhripunov/gpoint-eros@$EROS_DIGEST" in body
    assert "docker pull --platform linux/amd64" in body


def test_eros_digest_is_available_to_kamal_apply_and_runtime_verify():
    deploy = steps_for("deploy")
    kamal = next(step for step in deploy if step.get("name") == "Deploy exact immutable images with stock Kamal")
    verify = next(step for step in deploy if step.get("id") == "verify")
    assert kamal["env"]["EROS_DIGEST"] == "${{ steps.component_plan.outputs.eros_digest }}"
    assert verify["env"]["EROS_DIGEST"] == "${{ steps.component_plan.outputs.eros_digest }}"


def test_native_voyage_deploy_has_no_retired_local_embedding_infrastructure():
    deploy = steps_for("deploy")
    kamal = next(step for step in deploy if step.get("name") == "Deploy exact immutable images with stock Kamal")
    body = kamal["run"]
    eros = body.index('if [[ "$DEPLOY_EROS" == true ]]; then')
    core = body.index('if [[ "$DEPLOY_CORE" == true ]]; then')
    miniapp = body.index('if [[ "$DEPLOY_MINIAPP" == true ]]; then')
    assert eros < core
    assert core < miniapp
    assert body.index("verify_single_running_role gpoint-eros-pilot-engine", eros) < core
    assert "ollama" not in body.lower()
    assert "qwen" not in body.lower()
    assert "verify-layer7-ollama.py" not in body
    assert "core_api_config=config/deploy.eros-api.yml" in body
    assert "gpoint_ollama_internal" not in yaml.safe_load((ROOT / "config/deploy.eros-api.yml").read_text())["servers"]["api"]["options"]["network"]
    assert not (ROOT / "config/deploy.ollama.yml").exists()
    hook = (ROOT / ".kamal/hooks/post-app-boot").read_text()
    assert "gpoint-core:api) networks='kamal:gpoint-core-api,gpoint_app:api,shared_internal:'" in hook


def test_runtime_verify_uses_executable_eros_label_helper_and_ci_exercises_docker_templates():
    deploy = steps_for("deploy")
    verify = next(step for step in deploy if step.get("id") == "verify")
    e2e = next(step for step in deploy if step.get("id") == "eros_e2e")
    helper = (ROOT / "scripts/verify-eros-runtime-labels.py").read_text()
    assert "verify-eros-runtime-labels.py" in verify["run"]
    assert "verify-eros-runtime-labels.py" in e2e["run"]
    assert '"org.opencontainers.image.revision"' in helper
    assert '"com.gpoint.eros.patch-set.sha256"' in helper
    assert all(value in helper for value in ("subprocess.run", '"docker",', '"inspect",', '"--format",'))
    assert '\\"org.opencontainers.image.revision\\"' not in verify["run"]
    assert '\\"com.gpoint.eros.patch-set.sha256\\"' not in verify["run"]


def test_eros_runtime_label_helper_passes_valid_docker_templates_and_fails_closed():
    helper = ROOT / "scripts/verify-eros-runtime-labels.py"
    upstream = "c7d63d77e9b52d7ae4952b8374512f571da065d5"
    with tempfile.TemporaryDirectory() as temporary:
        fake_docker = Path(temporary) / "docker"
        fake_docker.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "assert sys.argv[1:3] == ['inspect', '--format']\n"
            "labels = {\n"
            "  '{{index .Config.Labels \\\"org.opencontainers.image.revision\\\"}}': '" + upstream + "',\n"
            "  '{{index .Config.Labels \\\"com.gpoint.eros.upstream-revision\\\"}}': '" + upstream + "',\n"
            "  '{{index .Config.Labels \\\"com.gpoint.eros.patch-set.sha256\\\"}}': 'none',\n"
            "}\n"
            "print(labels.get(sys.argv[3], ''))\n"
        )
        fake_docker.chmod(0o755)
        environment = dict(os.environ, PATH=temporary + os.pathsep + os.environ.get("PATH", ""))
        passed = subprocess.run(
            [sys.executable, str(helper), "container-id", upstream, "none"],
            env=environment, capture_output=True, text=True,
        )
        assert passed.returncode == 0, passed.stderr
        assert passed.stdout.strip() == "EROS_RUNTIME_LABELS=PASS"
        rejected = subprocess.run(
            [sys.executable, str(helper), "container-id", "0" * 40, "none"],
            env=environment, capture_output=True, text=True,
        )
        assert rejected.returncode == 1
        assert "identity mismatch" in rejected.stderr


def test_native_eros_deploy_uses_stock_kamal_without_retired_embedding_infrastructure():
    deploy = steps_for("deploy")
    kamal = next(step for step in deploy if step.get("name") == "Deploy exact immutable images with stock Kamal")
    body = kamal["run"]
    eros = body.index('if [[ "$DEPLOY_EROS" == true ]]; then')
    engine = body.index("kamal redeploy --skip-push --version \"$IMAGE_VERSION\" --roles engine", eros)
    assert engine > eros
    assert "config/deploy.eros-relay.yml" not in body
    assert "ollama" not in body.lower()
    assert "qwen" not in body.lower()
    assert "verify-layer7-ollama.py" not in body


def test_production_embedding_config_uses_native_voyage_4_lite():
    model = tomllib.loads((ROOT / "services/eros-engine/eros-production-model-config.toml").read_text())
    assert model["tasks"]["embedding"]["model"] == "voyage-4-lite"
    assert "gpoint_embeddings" not in model.get("providers", {})


def test_completed_memory_cutover_is_not_repeated_by_ordinary_release():
    deploy = steps_for("deploy")
    workflow = (ROOT / ".github/workflows/gpoint-release.yml").read_text()
    lifecycle = next(step for step in deploy if step.get("id") == "eros_database")
    body = lifecycle["run"]
    assert "voyage_memory_reset" not in workflow
    assert "Freeze Eros memory access for Voyage cutover" not in workflow
    assert "reset-test-memory" not in workflow
    assert "--test-memory-reset" not in workflow
    assert "Verify native Voyage memory write and recall" not in workflow
    assert "Disable Qwen route and stop Ollama" not in workflow
    assert "EROS_TEST_MEMORY_RESET=PASS" in (ROOT / "scripts/eros-native-database.py").read_text()
    assert "--mode prepare" in body and "--mode apply" in body
    assert "GITHUB_RUN_ID" in body and "GITHUB_RUN_ATTEMPT" in body


def test_native_config_and_runtime_env_change_only_after_backup_restore_and_cleanup():
    deploy = steps_for("deploy")
    gate = next(step for step in deploy if step.get("id") == "eros_database")
    body = gate["run"]
    assert body.index("--verify-voyage-api") < body.index("--mode prepare")
    assert "test-memory-reset" not in body
    assert body.index("--mode prepare") < body.index("EROS_MODEL_CONFIG=READY")
    assert body.index("EROS_MODEL_CONFIG=READY") < body.index("--write-native-engine-env")
    assert body.index("--write-native-engine-env") < body.index("--mode apply")


def test_repeat_preflight_accepts_only_receipt_consistent_cleared_backup():
    baseline = {
        "schemaSha256": "c" * 64,
        "tableCounts": {"engine.companion_memories": 476, "engine.relationship_journal": 11,
                        "engine.persona_instances": 3},
        "targets": {
            "engine.companion_memories": {"rows": 476, "vectors": 476, "dimension512": 476,
                                           "inboundFks": 0, "deleteTriggers": 0},
            "engine.relationship_journal": {"rows": 11, "vectors": 11, "dimension512": 11,
                                             "inboundFks": 0, "deleteTriggers": 0},
        },
    }
    cleared = {
        "schemaSha256": "c" * 64,
        "tableCounts": {"engine.companion_memories": 0, "engine.relationship_journal": 0,
                        "engine.persona_instances": 3},
        "targets": {
            "engine.companion_memories": {"rows": 0, "vectors": 0, "dimension512": 0,
                                           "inboundFks": 0, "deleteTriggers": 0},
            "engine.relationship_journal": {"rows": 0, "vectors": 0, "dimension512": 0,
                                             "inboundFks": 0, "deleteTriggers": 0},
        },
    }
    assert DB._memory_reset_is_cleared(cleared, baseline, {"state": "COMPLETE"}) is True
    assert DB._memory_reset_is_cleared(cleared, baseline, {"state": "PENDING"}) is True
    assert DB._memory_reset_is_cleared(baseline, baseline, {"state": "PENDING"}) is False


def test_eros_memory_reset_is_idempotent_for_same_authorized_release():
    helper = (ROOT / "scripts/eros-native-database.py").read_text()
    assert "testMemoryResetReceipt" in helper
    assert "EROS_TEST_MEMORY_RESET=SKIP_ALREADY_CLEARED" in helper
    workflow = (ROOT / ".github/workflows/gpoint-release.yml").read_text()
    assert "--mode reset-test-memory" not in workflow
    assert "GITHUB_RUN_ID" in workflow
    assert "GITHUB_RUN_ATTEMPT" in workflow


def test_memory_reset_repeat_with_complete_receipt_never_deletes_again(capsys):
    from unittest.mock import patch

    candidate = "a" * 40
    image = "ghcr.io/akhripunov/gpoint-eros@sha256:" + "b" * 64
    run_id = "123456-2"
    inventory = {
        "schemaSha256": "c" * 64,
        "tableCounts": {"engine.companion_memories": 0, "engine.relationship_journal": 0,
                        "engine.persona_instances": 3},
        "targets": {
            "engine.companion_memories": {"rows": 0, "vectors": 0, "dimension512": 0,
                                           "inboundFks": 0, "deleteTriggers": 0},
            "engine.relationship_journal": {"rows": 0, "vectors": 0, "dimension512": 0,
                                              "inboundFks": 0, "deleteTriggers": 0},
        },
    }
    receipt = {"schema": "gpoint-eros-test-memory-reset/v1", "candidateSha": candidate,
               "image": image, "state": "COMPLETE", "backupPath": "/opt/gpoint/backups/eros/old.age",
               "backupSha256": "d" * 64, "baselineInventory": inventory}
    plan = {"testMemoryResetReceipt": receipt, "testMemoryResetValidatedAt": "verified"}
    with patch.object(DB, "_read_env", return_value={"EROS_DATABASE_IDENTITY": "opaque"}), \
            patch.object(DB, "_load_plan", return_value=plan), \
            patch.object(DB, "_validate_reset_plan", return_value=inventory), \
            patch.object(DB, "_load_memory_reset_receipt", return_value=receipt), \
            patch.object(DB, "_memory_inventory", return_value=inventory), \
            patch.object(DB, "_validate_test_memory_inventory", return_value=True), \
            patch.object(DB, "_clear_test_memory_rows") as delete:
        DB.lifecycle("reset-test-memory", candidate, image, run_id, test_memory_reset=True)
    delete.assert_not_called()
    assert "EROS_TEST_MEMORY_RESET=SKIP_ALREADY_CLEARED" in capsys.readouterr().out


def test_voyage_secret_is_routed_only_to_eros_and_verified_from_vps():
    writer = (ROOT / "scripts/eros-production-secrets.py").read_text()
    config = yaml.safe_load((ROOT / "config/deploy.eros.yml").read_text())
    api_config = yaml.safe_load((ROOT / "config/deploy.eros-api.yml").read_text())
    assert "/opt/gpoint/env/voyage.env" in writer
    assert "VOYAGE_API_KEY" in writer
    assert "VOYAGE_API_KEY" not in str(api_config)
    assert "/opt/gpoint/env/eros-engine.env" in str(config)
    deploy = steps_for("deploy")
    probe = next(step for step in deploy if step.get("id") == "eros_database")
    assert "--verify-voyage-api" in probe["run"]
    assert "VOYAGE_{kind.upper()}=PASS" in writer and "--verify-voyage-api" in writer


def test_memory_reset_validator_rejects_non_target_drift_and_unknown_relationships():
    baseline = {
        "schemaSha256": "a" * 64,
        "tableCounts": {"engine.companion_memories": 476, "engine.relationship_journal": 11,
                        "engine.persona_instances": 3, "engine.chat_sessions": 7},
        "targets": {
            "engine.companion_memories": {"rows": 476, "vectors": 476, "dimension512": 476,
                                           "inboundFks": 0, "deleteTriggers": 0},
            "engine.relationship_journal": {"rows": 11, "vectors": 11, "dimension512": 11,
                                              "inboundFks": 0, "deleteTriggers": 0},
        },
    }
    assert DB._validate_test_memory_inventory(baseline, baseline) is True
    changed = __import__("copy").deepcopy(baseline)
    changed["tableCounts"]["engine.persona_instances"] += 1
    try:
        DB._validate_test_memory_inventory(changed, baseline)
    except RuntimeError as error:
        assert "non-target Eros rows changed" in str(error)
    else:
        raise AssertionError("non-target rows must block memory cleanup")
    changed = __import__("copy").deepcopy(baseline)
    changed["targets"]["engine.relationship_journal"]["inboundFks"] = 1
    try:
        DB._validate_test_memory_inventory(changed, baseline)
    except RuntimeError as error:
        assert "references or triggers" in str(error)
    else:
        raise AssertionError("unknown inbound references must block memory cleanup")
    changed = __import__("copy").deepcopy(baseline)
    changed["targets"]["engine.companion_memories"]["dimension512"] = 475
    try:
        DB._validate_test_memory_inventory(changed, baseline)
    except RuntimeError as error:
        assert "approved vector inventory" in str(error)
    else:
        raise AssertionError("non-512 or non-vector rows must block cleanup")


def test_component_deploys_use_stock_kamal_redeploy_without_automatic_prune():
    deploy = steps_for("deploy")
    kamal = next(step for step in deploy if step.get("name") == "Deploy exact immutable images with stock Kamal")
    body = kamal["run"]
    assert body.count("kamal redeploy --skip-push --version \"$IMAGE_VERSION\"") == 5
    assert "kamal deploy --skip-push" not in body


def test_release_docker_inspect_label_lookups_use_go_template_index():
    import re
    workflow = (ROOT / ".github/workflows/gpoint-release.yml").read_text()
    assert not re.search(r'\{\{\s*\.Config\.Labels\s+"', workflow)
    verify = next(step for step in steps_for("deploy") if step.get("id") == "verify")
    assert '{{index .Config.Labels "service"}}' in verify["run"]


def test_runtime_verify_checks_native_voyage_config_without_writing_synthetic_memory():
    verify = next(step for step in steps_for("deploy") if step.get("id") == "verify")
    body = verify["run"]
    assert 'EROS_EMBEDDING_PROVIDER=PASS provider=voyage model=voyage-4-lite dimensions=512' in body
    assert "createErosEngineClient" not in body
    assert "reset-test-memory" not in body


def test_production_embedding_is_native_voyage_and_test_fixture_is_not_emitted_to_runtime():
    model_config = (ROOT / "services/eros-engine/eros-production-model-config.toml").read_text()
    readme = (ROOT / "services/eros-engine/README.md").read_text()
    secret_writer = (ROOT / "scripts/eros-production-secrets.py").read_text()
    assert 'model = "voyage-4-lite"' in model_config
    assert "http://gpoint-core-api:3000/eros-worker/v1/embeddings" not in model_config
    assert "stock Eros v2.8.1's native `voyage-4-lite` embedding" in readme
    assert "EROS_TEST_EMBEDDING_URL" not in secret_writer
    assert "EROS_OLLAMA_EMBEDDING_URL" not in secret_writer


def test_native_deploy_materializes_and_hash_verifies_the_required_model_config():
    gate = next(step for step in steps_for("deploy")
                if step.get("name", "").startswith("Rehearse exact Eros SQLx"))
    body = gate["run"]
    assert "services/eros-engine/eros-production-model-config.toml" in body
    assert "EROS_MODEL_CONFIG=READY SHA256=$expected" in body
    assert 'sha256sum "$target"' in body
    assert "install -o root -g root -m 0600" in body


def test_sqlx_helper_checks_acl_ledger_backup_restore_and_never_downgrades_live_database():
    source = (ROOT / "scripts/eros-native-database.py").read_text()
    assert "public._sqlx_migrations" in source
    assert "runtimeForeign" in source and "migratorForeign" in source and "otherConnect" in source
    assert "GRANT CONNECT ON DATABASE {quoted} TO eros_runtime, eros_migrator" in source
    assert "pg_dump" in source and "pg_restore" in source
    assert '"age", "-r", recipient, "-o", str(staged)' in source
    assert '"age", "-d", "-i", str(AGE_IDENTITY)' in source
    assert 'b"age-encryption.org/v1\\n"' in source
    assert '"stat", "-f", "-c", "%T", "/run"' in source
    assert 'f"native-{run_id}.dump.age"' in source
    assert 'f".native-{run_id}.dump.age.pending"' in source
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
    dispatch = ROLLBACK_TRIGGERS["workflow_dispatch"]["inputs"]
    assert dispatch["rollback_eros"]["default"] is False
    rollback = steps_for("rollback")
    names = [step.get("name", "") for step in rollback]
    retained = names.index("Verify retained Eros rollback containers")
    compatibility = names.index("Verify Eros rollback image against an isolated current database restore")
    apply = names.index("Roll back consumers and then APIs with stock Kamal")
    runtime = names.index("Verify rollback Runtime and preserve databases before SUCCESS")
    assert retained < compatibility < apply < runtime
    compatibility_step = rollback[compatibility]
    assert "(inputs.rollback_eros == true) && inputs.mode == 'rollback'" in compatibility_step["if"]
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
    assert "(inputs.rollback_eros == true) && inputs.mode == 'rollback'" in compat["if"]


def test_eros_topology_consumes_the_kamal_inventory_version():
    topology = next(step for step in steps_for("deploy") if step.get("id") == "topology")
    assert topology["env"]["KAMAL_EROS_VERSION"] == "${{ steps.kamal_inventory.outputs.eros_version }}"


def test_kamal_roles_share_the_private_eros_network_with_stable_service_aliases():
    api = (ROOT / "config/deploy.eros-api.yml").read_text()
    engine = (ROOT / "config/deploy.eros.yml").read_text()
    model = (ROOT / "services/eros-engine/eros-production-model-config.toml").read_text()
    assert "- shared_internal" in api and "- gpoint_eros_data" in api
    assert "gpoint_ollama_internal" not in api
    assert "network-alias: gpoint-core-api" in api
    assert "network: gpoint_eros_data" in engine and "network-alias: gpoint-eros" in engine
    # Kamal adds --network kamal itself; an explicit duplicate fails docker run.
    assert yaml.safe_load(engine)["servers"]["engine"]["options"]["network"] == "gpoint_eros_data"
    assert "network-alias: gpoint-eros" in engine
    assert "'EROS_SERVICE_URL': 'http://gpoint-eros:8080'" in (ROOT / "scripts/eros-production-secrets.py").read_text()
    assert "http://gpoint-core-api:3000/eros-worker/v1/" not in model
    assert "https://api.deepseek.com/chat/completions" not in model
    assert "gpoint-codex@gpoint" not in model
    tasks = tomllib.loads(model)["tasks"]
    assert tasks["embedding"]["model"] == "voyage-4-lite"
    assert tasks["chat_companion"]["model"] == "deepseek/deepseek-v4.1-flash-1017"
    assert all(task["model"] == "anthropic/claude-haiku-5.5"
               for name, task in tasks.items() if name not in {"embedding", "chat_companion", "pde_decision"})
    assert tasks["pde_decision"]["model"] == "anthropic/claude-haiku-4.5"


def test_hosted_stock_eros_e2e_uses_exact_build_digest_and_approved_local_alias():
    assert not any("run-local-e2e.sh" in step.get("run", "") for step in steps_for("tests"))
    e2e = next(step for step in steps_for("deploy") if step.get("id") == "eros_e2e")
    assert e2e["env"]["EROS_DIGEST"] == "${{ steps.component_plan.outputs.eros_digest }}"
    assert e2e["env"]["APP_DIGEST"] == "${{ needs.build.outputs.app_digest }}"
    assert 'services/eros-engine/upstream.sha' in e2e["run"]
    assert 'image="gpoint-eros:stock-$upstream"' in e2e["run"]
    assert 'docker tag "$eros" "$image"' in e2e["run"]
    assert 'ghcr.io/akhripunov/gpoint-eros@$EROS_DIGEST' in e2e["run"]
    assert 'run-local-e2e.sh "$image" "$core"' in e2e["run"]


def test_normal_kamal_apply_and_runtime_verify_have_no_ollama_dependency():
    apply = next(step["run"] for step in steps_for("deploy") if step.get("id") == "kamal_apply")
    assert "ollama" not in apply.lower()
    assert "qwen" not in apply.lower()
    verify = next(step["run"] for step in steps_for("deploy") if step.get("id") == "verify")
    assert "ollama" not in verify.lower()
    assert "qwen" not in verify.lower()


def test_openrouter_runtime_verification_and_rollback_provider_guard():
    deploy = steps_for("deploy")
    verify = next(step for step in deploy if step.get("id") == "verify")["run"]
    assert 'EROS_PROVIDER_CONFIG=PASS provider=openrouter chat=deepseek/deepseek-v4.1-flash-1017 analysis=anthropic/claude-haiku-5.5' in verify
    assert 'test -n "$OPENROUTER_API_KEY"' in verify
    assert 'sha256sum /opt/gpoint/env/eros-production-model-config.toml' in verify
    retained = next(step for step in deploy if step.get("name") == "Verify retained Eros rollback containers")["run"]
    assert 'EROS_ROLLBACK_PROVIDER_COMPATIBLE=PASS' in retained
    assert 'Eros rollback target lacks OpenRouter credential' in retained
    assert 'Eros rollback target lacks compatible provider networking' in retained


def test_openrouter_uses_upstream_chat_and_group_reasoning_rules():
    config = tomllib.loads((ROOT / "services/eros-engine/eros-production-model-config.toml").read_text())
    assert set(config["providers"]) == {"openrouter"}
    assert config["providers"]["openrouter"]["body"] == [
        {"tasks": ["chat_companion"], "params": {"reasoning": {"enabled": False}}},
        {"tasks": ["group_director", "world_director"], "params": {"reasoning": {"enabled": False}, "response_format": {"type": "json_object"}}},
        {"tasks": ["world_stories_director"], "params": {"reasoning": {"enabled": False}, "response_format": {"type": "json_object"}}},
        {"tasks": ["insight_extraction", "character_insight_extraction", "character_insight_structuring", "user_insight_extraction", "user_insight_structuring", "affinity_summary", "world_comment", "world_reply"], "params": {"reasoning": {"enabled": False}}},
        {"tasks": ["affinity_evaluation", "activity_judge", "profile_rebuild", "dreaming_scan", "arc_rewrite"], "params": {"reasoning": {"effort": "low"}}},
    ]
    assert config["tasks"]["chat_companion"] == {
        "model": "deepseek/deepseek-v4.1-flash-1017",
        "fallback": ["thedrummer/cydonia-24b-v4.1", "z-ai/glm-4.7-flash"],
        "temperature": 0.8, "top_p": 0.9, "frequency_penalty": 0.4,
        "presence_penalty": 0.2, "max_tokens": 1600,
    }


def test_native_character_insights_use_upstream_haiku():
    config = tomllib.loads((ROOT / "services/eros-engine/eros-production-model-config.toml").read_text())
    extraction = dict(config["tasks"]["character_insight_extraction"])
    prompt = extraction.pop("filter_prompt")
    assert extraction == {
        "model": "anthropic/claude-haiku-5.5",
        "fallback": ["deepseek/deepseek-v4.1-flash-1017", "google/gemini-3.1-flash-lite"],
        "temperature": 0.2, "max_tokens": 800,
    }
    assert '"facts"' in prompt and '"details"' in prompt
    assert "current_situation" in prompt and "location" in prompt
    assert config["tasks"]["character_insight_structuring"] == {
        "model": "anthropic/claude-haiku-5.5",
        "fallback": ["deepseek/deepseek-v4.1-flash-1017", "google/gemini-3.1-flash-lite"],
        "temperature": 0.2, "max_tokens": 2400,
    }


def test_stock_eros_text_task_parity():
    config = tomllib.loads((ROOT / "services/eros-engine/eros-production-model-config.toml").read_text())
    expected = {
        "chat_companion": (0.8, 1600),
        "dreaming_scan": (0.2, 4096),
        "arc_rewrite": (0.3, 2400),
        "character_insight_extraction": (0.2, 800),
        "character_insight_structuring": (0.2, 2400),
        "insight_extraction": (0.2, 1200),
        "profile_rebuild": (0.2, 2400),
        "user_insight_extraction": (0.2, 800),
        "user_insight_structuring": (0.2, 2400),
        "affinity_evaluation": (0.3, 400),
        "activity_judge": (0.3, 1200),
        "affinity_summary": (0.6, 200),
        "group_director": (0.5, 4000),
        "world_director": (0.8, 4096),
        "world_stories_director": (0.8, 8192),
        "world_comment": (0.9, 2048),
        "world_reply": (0.9, 1024),
        "pde_decision": (0.3, 400),
    }
    tasks = config["tasks"]
    assert set(tasks) == set(expected) | {"embedding"}
    assert config["defaults"]["fallback_temperature"] == 0.5
    for name, (temperature, max_tokens) in expected.items():
        task = tasks[name]
        assert (task["temperature"], task["max_tokens"]) == (temperature, max_tokens), name
        assert task["model"] == ("deepseek/deepseek-v4.1-flash-1017" if name == "chat_companion" else "anthropic/claude-haiku-4.5" if name == "pde_decision" else "anthropic/claude-haiku-5.5"), name
        assert "retry_depth" not in task, name
        if name in {"pde_decision", "world_director", "world_stories_director", "world_comment", "world_reply"}:
            assert task["fallback"] == [], name
        elif name == "group_director":
            assert task["fallback"] == ["deepseek/deepseek-v4.1-flash-1017"], name
        elif name != "chat_companion":
            assert task["fallback"] == ["deepseek/deepseek-v4.1-flash-1017", "google/gemini-3.1-flash-lite"], name
    # Stock source: Eros v2.8.1 c7d63d77, examples/model_config.d.
    # Hash parsed prompts, not TOML formatting. Only the two explicitly
    # enumerated Dialogue Lab V8 extraction candidates differ from stock.
    # The original stock digests are retained here for the rollback contract.
    prompts = {
        "dreaming_scan": "289513d33da87001b9ffe4a237ed313a358d4be69b1f2094cb5f708ee61451f9",
        "character_insight_extraction": "db2f06cd32f92b27cfefa0ecf81027ec3fa7c3570680362471bb83313f12c2ba",
        "insight_extraction": "9ba059b3c325c4c0d4326ad696b4e6ecc7a9757b3f66fede692a28f913d0a177",
        "user_insight_extraction": "881016104709f3b2b8920c8dbbfde0b442497feee04125bf3e51f81abbf7a45b",
        "group_director": "6eeee1664c42106d2fc6045a2ec583298b931971dea1e3e6258e95423a3966a3",
        "world_director": "532a4cd219c80d559ede860c4f9fa63d6bf9bdd6a3478cd66f56dcf3d1c83a08",
        "world_stories_director": "086491d63fe8f21b162b0a87e61bae56e3ad875d4ef26e63f3d7b45d61291145",
        "world_comment": "98940c36c87eccf0d9887aa91d9c09bd53411c18e0befc6ce522287a01be34cb",
        "world_reply": "058ef651120346e085a3b931e45f094847f2108677b6280a2bf975b3822a4a4c",
    }
    # Intentional prompt-only overrides; every other task retains stock parity.
    v8_extraction_overrides = {
        "character_insight_extraction": "0f0735d19aaaca0a19e6cd1d56b7ec15935a23a31ce6ee1a3fc8a6f95270e899",
        "insight_extraction": "2580d9e1563a8d02dfeb131250f70bece7b059a9e02e0653f5b41f30c537cc30",
    }
    assert set(v8_extraction_overrides) == {"character_insight_extraction", "insight_extraction"}
    prompts.update(v8_extraction_overrides)
    assert {name for name, task in tasks.items() if "filter_prompt" in task} == set(prompts)
    for name, digest in prompts.items():
        assert hashlib.sha256(tasks[name]["filter_prompt"].encode()).hexdigest() == digest, name
    # Keep native schema generation enabled; provider body rules override the three tested directors only.
    for name in ("group_director", "world_director", "world_stories_director", "world_comment"):
        assert tasks[name].get("structured_output", True) is True, name
    assert not any(task.get("structured_output") is False for task in tasks.values())
    assert (tasks["world_director"]["interval_hours"], tasks["world_director"]["retention_days"]) == (24, 30)
    assert tuple(tasks["world_stories_director"][k] for k in
                 ("interval_hours", "retention_days", "active_window_hours", "context_days")) == (8, 30, 72, 7)
    assert tasks["world_comment"]["round_secs"] == 3600
    assert tuple(tasks["world_reply"][k] for k in
                 ("debounce_secs", "thread_cooldown_secs", "daily_cap", "reply_window_secs")) == (90, 600, 20, 604800)


def test_world_writer_protected_lifecycle_and_stock_fixture():
    deploy = steps_for("deploy")
    gate = next(step for step in deploy if step.get("id") == "eros_database")
    body = gate["run"]
    assert body.index("--mode apply") < body.index('python3 "$writer_stage/scripts/eros-world-writer.py"')
    assert "--acl-sha" in body and "writer-acl.sql" in body
    apply = next(step for step in deploy if step.get("id") == "kamal_apply")
    assert apply["run"].index("--mode verify-world-writer") < apply["run"].index("kamal redeploy")
    helper = (ROOT / "scripts/eros-world-writer.py").read_text()
    assert 'DB.lifecycle("verify-applied"' in helper
    assert "assert_acl(False)" in helper and "assert_acl(True)" in helper
    assert "SEC.write_native_api_env()" in helper
    runner = (ROOT / "services/eros-engine/run-local-e2e.sh").read_text()
    assert "LOCAL_E2E_WORLD_WRITER_ACL=PASS" in runner
    assert "LOCAL_E2E_WORLD_ENROLLMENT=PASS" in runner
