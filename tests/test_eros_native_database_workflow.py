from pathlib import Path
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


def steps_for(job):
    if job == "rollback":
        return ROLLBACK_WORKFLOW["jobs"]["deploy"]["steps"]
    return WORKFLOW["jobs"][job]["steps"]


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


def test_ollama_network_and_eros_engine_start_before_core_api_uses_eros_config():
    deploy = steps_for("deploy")
    kamal = next(step for step in deploy if step.get("name") == "Deploy exact immutable images with stock Kamal")
    body = kamal["run"]
    eros = body.index('if [[ "$DEPLOY_EROS" == true ]]; then')
    core = body.index('if [[ "$DEPLOY_CORE" == true ]]; then')
    miniapp = body.index('if [[ "$DEPLOY_MINIAPP" == true ]]; then')
    assert eros < core
    assert core < miniapp
    assert body.index("verify_single_running_role gpoint-eros-pilot-engine", eros) < core
    helper = (ROOT / "scripts/verify-layer7-ollama.py").read_text()
    assert 'LEGACY_CONTAINER = "gpoint-eros-relay-ollama"' in helper
    assert '"docker", "rename", LEGACY_CONTAINER, CONTAINER' in helper
    assert "accessory boot ollama --config-file config/deploy.ollama.yml" in body
    assert "accessory reboot ollama" not in body
    assert 'NETWORK = "gpoint_ollama_internal"' in helper
    assert "core_api_config=config/deploy.eros-api.yml" in body


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


def test_ollama_is_a_pinned_persistent_private_kamal_accessory():
    config = yaml.safe_load((ROOT / "config/deploy.ollama.yml").read_text())
    ollama = config["accessories"]["ollama"]
    assert ollama["image"] == (
        "ollama/ollama@sha256:a717bfc1c56acfc2e85d3109bd40a7646258ec9062c40cea83b9ed0927eb4223"
    )
    assert ollama["port"] == "127.0.0.1:11435:11434"
    assert ollama["network"] == "gpoint_ollama_internal"
    assert ollama["options"]["network-alias"] == "ollama"
    assert "ollama-layer7-models:/root/.ollama" in ollama["volumes"]
    assert ollama["options"]["restart"] == "unless-stopped"
    assert ollama["options"]["cpus"] == 1.5
    assert ollama["options"]["memory"] == "3g"
    assert ollama["options"]["memory-swap"] == "3g"


def test_native_eros_deploy_uses_stock_kamal_accessory_and_verifies_direct_route():
    deploy = steps_for("deploy")
    kamal = next(step for step in deploy if step.get("name") == "Deploy exact immutable images with stock Kamal")
    body = kamal["run"]
    eros = body.index('if [[ "$DEPLOY_EROS" == true ]]; then')
    accessory = body.index("kamal accessory boot ollama --config-file config/deploy.ollama.yml")
    model_pull = body.index("kamal accessory exec ollama ollama pull qwen3-embedding:0.6b", accessory)
    engine = body.index("kamal redeploy --skip-push --version \"$IMAGE_VERSION\" --roles engine", eros)
    assert accessory < model_pull < engine
    assert "config/deploy.eros-relay.yml" not in body
    assert "restore_legacy_ollama" not in body
    assert "verify-layer7-ollama.py" in body
    assert "--prepare" in body
    assert "--verify-and-detach" in body


def test_component_deploys_use_stock_kamal_redeploy_without_automatic_prune():
    deploy = steps_for("deploy")
    kamal = next(step for step in deploy if step.get("name") == "Deploy exact immutable images with stock Kamal")
    body = kamal["run"]
    assert body.count("kamal redeploy --skip-push --version \"$IMAGE_VERSION\"") == 5
    assert "kamal deploy --skip-push" not in body


def test_runtime_verify_sends_512_dimension_embedding_from_inside_eros_container():
    verify = next(step for step in steps_for("deploy") if step.get("id") == "verify")
    body = verify["run"]
    assert "verify-layer7-ollama.py" in body
    assert "gpoint_ollama_internal" not in body  # network membership is checked by the helper
    assert "http://gpoint-core-api:3000/eros-worker/v1/embeddings" in body


def test_production_embedding_route_is_layer7_internal_and_not_mac_hosted():
    model_config = (ROOT / "services/eros-engine/eros-production-model-config.toml").read_text()
    readme = (ROOT / "services/eros-engine/README.md").read_text()
    secret_writer = (ROOT / "scripts/eros-production-secrets.py").read_text()
    assert "http://gpoint-core-api:3000/eros-worker/v1/embeddings" in model_config
    assert "Mac-hosted Ollama" not in readme
    assert "Eros embeddings use the pinned Qwen model on Layer7" in readme
    assert "'EROS_OLLAMA_EMBEDDING_URL': 'http://ollama:11434/api/embed'" in secret_writer


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
    assert "network-alias: gpoint-core-api" in api
    assert "network: gpoint_eros_data" in engine and "network-alias: gpoint-eros" in engine
    # Kamal adds --network kamal itself; an explicit duplicate fails docker run.
    assert yaml.safe_load(engine)["servers"]["engine"]["options"]["network"] == "gpoint_eros_data"
    assert "network-alias: gpoint-eros" in engine
    assert "'EROS_SERVICE_URL': 'http://gpoint-eros:8080'" in (ROOT / "scripts/eros-production-secrets.py").read_text()
    assert model.count("http://gpoint-core-api:3000/eros-worker/v1/") == 1
    assert "https://api.deepseek.com/chat/completions" in model
    assert "gpoint-codex@gpoint" not in model
    tasks = tomllib.loads(model)["tasks"]
    assert tasks["embedding"]["model"] == "qwen3-embedding:0.6b@gpoint_embeddings"
    assert all(task["model"] == "deepseek-flash@deepseek"
               for name, task in tasks.items() if name != "embedding")


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


def test_ollama_preflight_does_not_send_docker_templates_over_ssh():
    apply = next(step["run"] for step in steps_for("deploy") if step.get("id") == "kamal_apply")
    start = apply.index('if [[ "$DEPLOY_CORE" == true && ( "$EROS_INSTALLED" == true || "$DEPLOY_EROS" == true ) ]]; then')
    end = apply.index('if [[ "$DEPLOY_EROS" == true ]]; then', start)
    ollama_preflight = apply[start:end]
    assert "verify-layer7-ollama.py" in ollama_preflight
    assert "--prepare" in ollama_preflight
    assert "docker inspect --format" not in ollama_preflight
    assert "docker network inspect gpoint_ollama_internal --format" not in ollama_preflight


def test_release_forwards_ollama_helper_over_ssh_without_go_templates(tmp_path):
    import re

    deploy = steps_for("deploy")
    kamal = next(step for step in deploy if step.get("name") == "Deploy exact immutable images with stock Kamal")
    line = next(line.strip() for line in kamal["run"].splitlines()
                if 'accessory_result="$(ssh -T' in line and "--prepare" in line)
    assert "--format" not in line and "{{" not in line
    assert re.search(r"< scripts/verify-layer7-ollama\.py\)\"$", line)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh = fake_bin / "ssh"
    ssh.write_text("#!/bin/sh\n[ \"$1\" = -T ] && shift\nshift\nexec \"$@\"\n")
    ssh.chmod(0o755)
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "a=sys.argv[1:]\n"
        "if a[:3] == ['network','inspect','gpoint_ollama_internal']:\n"
        " print(json.dumps([{'Internal':True,'Containers':{}}]))\n"
        "elif a and a[0] == 'inspect':\n"
        " print('Error: No such object: '+a[1], file=sys.stderr); raise SystemExit(1)\n"
        "else: raise SystemExit('unexpected docker call')\n"
    )
    docker.chmod(0o755)
    environment = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                       GPOINT_PRODUCTION_HOST="example.invalid", EROS_OLLAMA_MODEL_DIGEST="a" * 64)
    result = subprocess.run(["bash", "-c", line + "; printf '%s\\n' \"$accessory_result\""], cwd=ROOT, env=environment,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "OLLAMA_PREPARE=PASS state=absent\n"

def test_deepseek_runtime_verification_and_rollback_provider_guard():
    deploy = steps_for("deploy")
    verify = next(step for step in deploy if step.get("id") == "verify")["run"]
    assert 'EROS_PROVIDER_CONFIG=PASS provider=deepseek model=deepseek-flash' in verify
    assert 'test -n "$DEEPSEEK_API_KEY"' in verify
    assert 'sha256sum /opt/gpoint/env/eros-production-model-config.toml' in verify
    retained = next(step for step in deploy if step.get("name") == "Verify retained Eros rollback containers")["run"]
    assert 'EROS_ROLLBACK_PROVIDER_COMPATIBLE=PASS' in retained
    assert 'Eros rollback target lacks DeepSeek credential' in retained
    assert 'Eros rollback target lacks compatible provider networking' in retained


def test_deepseek_disables_thinking_for_all_configured_text_tasks():
    config = tomllib.loads((ROOT / "services/eros-engine/eros-production-model-config.toml").read_text())
    assert config["providers"]["deepseek"]["body"] == [
        {"tasks": [name for name in config["tasks"] if name != "embedding"],
         "params": {"thinking": {"type": "disabled"}}}
    ]
    assert config["tasks"]["chat_companion"]["model"] == "deepseek-flash@deepseek"
    assert config["tasks"]["chat_companion"]["retry_depth"] == 0
    assert config["tasks"]["chat_companion"] == {
        "model": "deepseek-flash@deepseek",
        "retry_depth": 0,
        "temperature": 0.8,
        "top_p": 0.9,
        "frequency_penalty": 0.4,
        "presence_penalty": 0.2,
        "max_tokens": 1600,
    }

def test_native_character_insights_have_bounded_direct_deepseek_tasks():
    config = tomllib.loads((ROOT / "services/eros-engine/eros-production-model-config.toml").read_text())
    extraction = dict(config["tasks"]["character_insight_extraction"])
    prompt = extraction.pop("filter_prompt")
    assert extraction == {
        "model": "deepseek-flash@deepseek", "fallback": [], "retry_depth": 0,
        "temperature": 0.2, "max_tokens": 800,
    }
    assert '"facts"' in prompt and '"details"' in prompt
    assert "current_situation" in prompt and "location" in prompt
    assert config["tasks"]["character_insight_structuring"] == {
        "model": "deepseek-flash@deepseek", "fallback": [], "retry_depth": 0,
        "temperature": 0.2, "max_tokens": 600,
    }

def test_stock_eros_text_task_parity():
    config = tomllib.loads((ROOT / "services/eros-engine/eros-production-model-config.toml").read_text())
    expected = {
        "chat_companion": (0.8, 1600),
        "dreaming_scan": (0.2, 1024),
        "arc_rewrite": (0.3, 1500),
        "character_insight_extraction": (0.2, 800),
        "character_insight_structuring": (0.2, 600),
        "insight_extraction": (0.2, 1200),
        "profile_rebuild": (0.2, 1200),
        "user_insight_extraction": (0.2, 800),
        "user_insight_structuring": (0.2, 600),
        "affinity_evaluation": (0.3, 400),
        "activity_judge": (0.3, 1200),
        "affinity_summary": (0.6, 200),
        "group_director": (0.5, 4000),
        "world_director": (0.8, 4096),
        "world_stories_director": (0.8, 4096),
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
        assert task["model"] == "deepseek-flash@deepseek", name
        assert task["retry_depth"] == 0, name
        if name != "chat_companion":
            assert task["fallback"] == [], name
    # Source: Eros v2.8.1 c7d63d77, examples/model_config.d.
    # Hash the parsed prompt, not the TOML formatting.
    prompts = {
        "dreaming_scan": "289513d33da87001b9ffe4a237ed313a358d4be69b1f2094cb5f708ee61451f9",
        "character_insight_extraction": "db2f06cd32f92b27cfefa0ecf81027ec3fa7c3570680362471bb83313f12c2ba",
        "insight_extraction": "9ba059b3c325c4c0d4326ad696b4e6ecc7a9757b3f66fede692a28f913d0a177",
        "user_insight_extraction": "881016104709f3b2b8920c8dbbfde0b442497feee04125bf3e51f81abbf7a45b",
        "group_director": "6eeee1664c42106d2fc6045a2ec583298b931971dea1e3e6258e95423a3966a3",
        "world_director": "532a4cd219c80d559ede860c4f9fa63d6bf9bdd6a3478cd66f56dcf3d1c83a08",
        "world_stories_director": "5c2a06b775cb72fe7731e34487311752fb89ef2bfc2078184370291231b2fbf4",
        "world_comment": "98940c36c87eccf0d9887aa91d9c09bd53411c18e0befc6ce522287a01be34cb",
        "world_reply": "058ef651120346e085a3b931e45f094847f2108677b6280a2bf975b3822a4a4c",
    }
    assert {name for name, task in tasks.items() if "filter_prompt" in task} == set(prompts)
    for name, digest in prompts.items():
        assert hashlib.sha256(tasks[name]["filter_prompt"].encode()).hexdigest() == digest, name
    assert {name for name, task in tasks.items() if task.get("structured_output") is False} == {
        "group_director", "world_director", "world_stories_director", "world_comment",
    }
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
