from pathlib import Path
import os
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
TRIGGERS = WORKFLOW.get("on", WORKFLOW.get(True, {}))
ROLLBACK_WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
ROLLBACK_TRIGGERS = ROLLBACK_WORKFLOW.get("on", ROLLBACK_WORKFLOW.get(True, {}))


def steps_for(job):
    if job == "rollback":
        return ROLLBACK_WORKFLOW["jobs"]["deploy"]["steps"]
    return WORKFLOW["jobs"][job]["steps"]


def test_native_deploy_provisions_only_bootstrap_and_rehearses_exact_eros_sqlx_transition():
    deploy = steps_for("deploy")
    names = [step.get("name", "") for step in deploy]
    gate = next(step for step in deploy if step.get("name", "").startswith("Rehearse exact Eros SQLx"))
    body = gate["run"]
    assert gate["if"] == "(steps.component_plan.outputs.deploy_eros == 'true') && inputs.mode != 'rollback'"
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


def test_first_eros_engine_and_relay_start_before_core_api_selects_eros_route():
    deploy = steps_for("deploy")
    kamal = next(step for step in deploy if step.get("name") == "Deploy exact immutable images with stock Kamal")
    body = kamal["run"]
    eros = body.index('if [[ "$DEPLOY_EROS" == true ]]; then')
    core = body.index('if [[ "$DEPLOY_CORE" == true ]]; then')
    miniapp = body.index('if [[ "$DEPLOY_MINIAPP" == true ]]; then')
    assert eros < core
    assert core < miniapp
    assert body.index("verify_single_running_role gpoint-eros-pilot-engine", eros) < core
    assert body.index("verify_single_running_role gpoint-eros-relay-relay", eros) < core
    assert "core_api_config=config/deploy.eros-api.yml" in body


def test_runtime_verify_uses_executable_eros_label_helper_and_ci_exercises_docker_templates():
    deploy = steps_for("deploy")
    verify = next(step for step in deploy if step.get("id") == "verify")
    test_steps = steps_for("tests")
    e2e = next(step for step in test_steps
               if "run-local-e2e.sh" in step.get("run", ""))
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
    upstream = "d1a140083321687212fb7b17c09cc0c3c9639f5e"
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


def test_eros_relay_health_checks_use_its_configured_host_bind_address():
    deploy = next(step for step in steps_for("deploy") if step.get("id") == "verify")
    rollback = next(step for step in steps_for("rollback")
                    if step.get("name") == "Verify rollback Runtime and preserve databases before SUCCESS")
    for step in (deploy, rollback):
        assert "http://172.31.254.1:11434/healthz" in step["run"]
        assert "http://127.0.0.1:11434/healthz" not in step["run"]


def test_ollama_is_a_pinned_persistent_private_kamal_accessory():
    config = yaml.safe_load((ROOT / "config/deploy.eros-relay.yml").read_text())
    ollama = config["accessories"]["ollama"]
    assert ollama["image"] == (
        "ollama/ollama@sha256:a717bfc1c56acfc2e85d3109bd40a7646258ec9062c40cea83b9ed0927eb4223"
    )
    assert ollama["port"] == "127.0.0.1:11435:11434"
    assert "ollama-layer7-models:/root/.ollama" in ollama["volumes"]
    assert ollama["options"]["restart"] == "unless-stopped"
    assert ollama["options"]["cpus"] == 1.5
    assert ollama["options"]["memory"] == "3g"
    assert ollama["options"]["memory-swap"] == "3g"


def test_native_eros_deploy_bootstraps_and_verifies_local_ollama_before_engine():
    deploy = steps_for("deploy")
    kamal = next(step for step in deploy if step.get("name") == "Deploy exact immutable images with stock Kamal")
    body = kamal["run"]
    eros = body.index('if [[ "$DEPLOY_EROS" == true ]]; then')
    accessory_boot = body.index("kamal accessory boot ollama", eros)
    model_pull = body.index("kamal accessory exec ollama ollama pull qwen3-embedding:0.6b", accessory_boot)
    model_verify = body.index("verify-layer7-ollama.py", model_pull)
    relay_deploy = body.index("kamal deploy --skip-push --version \"$IMAGE_VERSION\" --roles relay", model_verify)
    engine_deploy = body.index("kamal deploy --skip-push --version \"$IMAGE_VERSION\" --roles engine", relay_deploy)
    assert accessory_boot < model_pull < model_verify < relay_deploy < engine_deploy
    assert "EROS_OLLAMA_MODEL_DIGEST" in body
    assert 'if [[ "$accessory_state" == ABSENT ]]; then' in body
    assert '"No such object: gpoint-eros-relay-ollama"' in body
    assert 'docker inspect --format' in body
    assert '2>/dev/null || true' not in body
    legacy_verify = body.index(
        'python3 - "$EROS_OLLAMA_MODEL_DIGEST" ollama-layer7',
        body.index('if [[ "$accessory_state" == ABSENT ]]; then'),
    )
    legacy_stop = body.index('docker stop ollama-layer7', legacy_verify)
    assert legacy_verify < legacy_stop < accessory_boot
    assert "restore_legacy_ollama" in body
    assert body.index('docker rm ollama-layer7') > model_verify


def test_runtime_verify_sends_512_dimension_embedding_from_inside_eros_container():
    verify = next(step for step in steps_for("deploy") if step.get("id") == "verify")
    body = verify["run"]
    assert "docker exec \"gpoint-eros-pilot-engine-$eros_version\" sh -c" in body
    assert "EROS_PILOT_EMBEDDING_PROBE_URL" in body
    assert "EROS_OLLAMA_EMBEDDING_URL" not in body
    assert "http://172.31.254.1:11434/api/embed" in body
    assert '\\"dimensions\\":512' in body
    assert "Synthetic Layer7 Eros network verification" in body


def test_production_embedding_route_is_layer7_internal_and_not_mac_hosted():
    model_config = (ROOT / "services/eros-engine/eros-production-model-config.toml").read_text()
    readme = (ROOT / "services/eros-engine/README.md").read_text()
    secret_writer = (ROOT / "scripts/eros-production-secrets.py").read_text()
    assert "http://gpoint-core-api:3000/eros-worker/v1/embeddings" in model_config
    assert "Mac-hosted Ollama" not in readme
    assert "Eros embeddings use the pinned Qwen model on Layer7" in readme
    assert "'EROS_OLLAMA_EMBEDDING_URL': 'http://172.31.254.1:11434/api/embed'" in secret_writer


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
    assert compatibility_step["if"] == "(inputs.rollback_eros == true) && inputs.mode == 'rollback'"
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
    assert compat["if"] == "(inputs.rollback_eros == true) && inputs.mode == 'rollback'"


def test_eros_topology_inventory_shell_loop_has_no_unused_read_variable():
    deploy = steps_for("deploy")
    topology = next(step["run"] for step in deploy
                    if step.get("name") == "Verify installed Kamal topology before ordinary release")
    assert '"config/deploy.eros.yml|gpoint-eros-pilot-engine"' in topology
    assert 'IFS=\'|\' read -r config expected <<< "$pair"' in topology


def test_kamal_roles_share_the_private_eros_network_with_stable_service_aliases():
    api = (ROOT / "config/deploy.eros-api.yml").read_text()
    engine = (ROOT / "config/deploy.eros.yml").read_text()
    staging_engine = (ROOT / "config/deploy.layer7-staging.eros.yml").read_text()
    model = (ROOT / "services/eros-engine/eros-production-model-config.toml").read_text()
    assert "- shared_internal" in api and "- gpoint_eros_data" in api
    assert "network-alias: gpoint-core-api" in api
    assert "network: gpoint_eros_data" in engine and "network-alias: gpoint-eros" in engine
    assert "network-alias: gpoint-eros" in staging_engine
    assert "'EROS_SERVICE_URL': 'http://gpoint-eros:8080'" in (ROOT / "scripts/eros-production-secrets.py").read_text()
    assert model.count("http://gpoint-core-api:3000/eros-worker/v1/") == 2


def test_hosted_stock_eros_e2e_uses_the_policy_approved_upstream_tag():
    test_steps = steps_for("tests")
    e2e = next(step["run"] for step in test_steps
               if "run-local-e2e.sh" in step.get("run", ""))
    build = next(step for step in test_steps if step.get("id") == "eros_test_image")
    upstream = "d1a140083321687212fb7b17c09cc0c3c9639f5e"
    assert build["with"]["tags"] == f"gpoint-eros:stock-{upstream}"
    assert f"#{upstream}" in build["with"]["context"]
    assert 'image="gpoint-eros:stock-$EROS_UPSTREAM_SHA"' in e2e
