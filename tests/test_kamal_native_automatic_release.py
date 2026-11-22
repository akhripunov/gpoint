import os
from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-release.yml"

_ROLLBACK = yaml.safe_load((ROOT / ".github/workflows/gpoint-release.yml").read_text())
_REHEARSAL = yaml.safe_load((ROOT / ".github/workflows/gpoint-operations.yml").read_text())



def test_main_push_runs_exact_candidate_tests_build_kamal_and_verify():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    events = workflow.get("on", workflow.get(True))
    assert events["push"]["branches"] == ["main"]
    assert events["pull_request"]["branches"] == ["main"]
    deploy = workflow["jobs"]["deploy"]
    assert deploy["needs"] == ["tests", "build"]
    assert "push" in deploy["if"] and "refs/heads/main" in deploy["if"]
    assert "workflow_dispatch" in deploy["if"]
    assert deploy["environment"]["name"] == "production"
    build = workflow["jobs"]["build"]
    assert set(workflow["jobs"]) == {"tests", "build", "deploy"}
    assert build["needs"] == ["tests"]
    assert "github.event_name == 'pull_request'" in build["if"]
    assert "outputs.mode == 'DEPLOY'" in build["if"]
    assert "ARTIFACT_PREPARE" not in build["if"]
    assert "LAYER7_API" not in build["if"]
    assert "LAYER7_EROS" not in build["if"]
    assert "environment" not in build
    assert build["permissions"]["packages"] == "write"
    assert (ROOT / ".github/workflows/gpoint-operations.yml").exists()


def test_core_deploy_prepares_and_verifies_stable_worker_tunnel_with_stock_kamal():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    apply = next(step for step in steps if step.get("id") == "kamal_apply")["run"]
    verify = next(step for step in steps if step.get("id") == "verify")["run"]
    accessory = apply.index("kamal accessory boot worker-proxy")
    core_api = apply.index("kamal deploy --skip-push --version \"$IMAGE_VERSION\" --roles api")

    assert core_api < accessory
    assert "current_proxy_service=\"$(ssh -T" not in apply
    assert "REMOTE_WORKER_PROXY_INVENTORY" in apply
    assert "PROXY_STATE=missing" in apply
    assert "|| true)" not in apply
    assert "kamal accessory reboot worker-proxy" in apply
    assert 'GPOINT_WORKER_PROXY_DIGEST="$APP_DIGEST"' in apply
    assert "--env GPOINT_WORKER_PROXY_DIGEST" in apply
    assert "docker run" not in apply[accessory:core_api]
    assert "127.0.0.1:13004" in apply
    assert "expected 401" in apply
    assert "--request POST --data '{}'" in apply
    assert "gpoint-core-worker-proxy" in verify
    assert "GPOINT_WORKER_TUNNEL_PROXY=PASS" in verify
    assert '''if bindings != {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "13004"}]}''' in verify
    assert '''if set(networks) != {"kamal"}:''' in verify


def test_eros_rollback_image_uses_the_upstream_pin_from_its_source_revision():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(item for item in workflow["jobs"]["deploy"]["steps"]
                if item.get("name") == "Verify target digests, platform, revision and provenance")

    assert 'git show "${EROS_SOURCE_SHA}:services/eros-engine/upstream.sha"' in step["run"]
    assert '--upstream-sha "$eros_upstream_sha"' in step["run"]


def test_core_rollback_keeps_the_worker_tunnel_accessory_and_checks_it_after_rollback():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    rollback_apply = next(step for step in steps if step.get("id") == "rollback_apply")["run"]
    rollback_verify = next(step for step in steps if step.get("id") == "rollback_verify")["run"]

    assert "accessory remove worker-proxy" not in rollback_apply
    assert "accessory stop worker-proxy" not in rollback_apply
    assert "gpoint-core-worker-proxy" in rollback_verify
    assert "Worker tunnel accessory is missing after Core rollback" in rollback_verify
    assert "127.0.0.1:13004" in rollback_verify
    assert "WORKER_TUNNEL_PROXY=PASS" in rollback_verify
    assert "--request POST --data '{}'" in rollback_verify


def test_layer7_is_production_and_retired_staging_paths_cannot_deploy():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from native_release_intent import classify_push, push_deploy_authorized

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert set(jobs) == {"tests", "build", "deploy"}
    assert not (ROOT / ".github/workflows/gpoint-layer7-staging.yml").exists()
    assert not (ROOT / ".github/actions/layer7-deploy/action.yml").exists()
    legacy_paths = {".github/workflows/gpoint-layer7-staging.yml",
                    "scripts/layer7_release_scope.py"}
    for marker in ("GPOINT-ARTIFACT-PREPARE", "GPOINT-LAYER7-API", "GPOINT-LAYER7-EROS"):
        mode = classify_push(event_name="push", ref="refs/heads/main",
                             changed_paths=legacy_paths, commit_message=marker)
        assert mode == "BLOCK"
        assert not push_deploy_authorized(mode=mode, changed_paths=legacy_paths)

    deploy = jobs["deploy"]
    assert "needs.tests.outputs.deploy == 'true'" in deploy["if"]
    assert "github.actor == github.repository_owner" in deploy["if"]
    assert deploy["environment"]["name"] == "production"
    assert deploy["concurrency"]["group"] == "gpoint-production-release"
    assert not any("LAYER7_API" in step.get("if", "") for step in deploy["steps"])
    assert any("prisma migrate deploy" in step.get("run", "") for step in deploy["steps"])


def test_eros_builds_share_layers_without_publishing_the_test_image():
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    test_steps = jobs["tests"]["steps"]
    test_build = next(step for step in test_steps if step.get("id") == "eros_test_image")
    verification = next(step for step in test_steps if "run-local-e2e.sh" in step.get("run", ""))
    release_build = next(step for step in jobs["build"]["steps"] if step.get("id") == "eros")

    test_inputs = test_build["with"]
    release_inputs = release_build["with"]
    assert test_build["if"] == verification["if"]
    assert test_steps.index(test_build) < test_steps.index(verification)
    assert test_inputs["context"] == release_inputs["context"]
    assert test_inputs["file"] == release_inputs["file"]
    assert test_inputs["platforms"] == release_inputs["platforms"] == "linux/amd64"
    assert test_inputs["load"] is True
    assert test_inputs["push"] is False
    assert "github.ref == 'refs/heads/main'" in test_inputs["cache-to"]
    assert "github.event_name != 'pull_request'" in test_inputs["cache-to"]
    assert "scope=gpoint-eros-test" in test_inputs["cache-from"]
    assert "scope=gpoint-eros-test" in test_inputs["cache-to"]
    assert "scope=gpoint-eros-test" in release_inputs["cache-from"]
    assert "scope=gpoint-eros-release" in release_inputs["cache-from"]
    assert "scope=gpoint-eros-release" in release_inputs["cache-to"]
    assert "scope=gpoint-eros-test" not in release_inputs["cache-to"]
    assert release_inputs["push"] is True
    assert release_inputs["sbom"] is True
    assert release_inputs["provenance"] == "mode=max"


def test_build_unconditionally_publishes_all_three_images_and_live_inventory_scopes_apply():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    build = workflow["jobs"]["build"]
    steps = build["steps"]
    assert "build_components" not in build["outputs"]
    assert "deploy_scope" not in build["outputs"]
    assert not any(step.get("id") == "build_scope" for step in steps)
    for step in steps:
        if step.get("uses", "").startswith(("docker/setup-buildx-action@", "docker/login-action@")):
            assert "if" not in step
    image_steps = [step for step in steps
                   if step.get("uses", "").startswith("docker/build-push-action@")]
    assert {step.get("id") for step in image_steps} == {"app", "miniapp", "eros"}
    assert all("if" not in step for step in image_steps)

    deploy_steps = workflow["jobs"]["deploy"]["steps"]
    component_plan = next(step for step in deploy_steps if step.get("id") == "component_plan")
    plan_run = component_plan["run"]
    assert "VERIFY_ONLY" not in plan_run
    assert "classify_component_scope" in plan_run
    assert "COMPONENT_IMAGES" not in plan_run
    assert "BUILD_COMPONENTS" not in plan_run

    migration_plan = next(step for step in deploy_steps if step.get("id") == "migration_plan")
    assert "BUILD_COMPONENTS" not in migration_plan["env"]
    assert "VERIFY_ONLY" not in migration_plan["run"]
    assert 'no prebuilt candidate Core image' not in migration_plan["run"]

    tests = workflow["jobs"]["tests"]
    assert "eros_e2e_required" not in tests["outputs"]
    eros_scope = next(step for step in tests["steps"] if step.get("id") == "eros_scope")
    assert eros_scope["if"] == "matrix.suite == 'workspace'"
    evidence = next(step for step in deploy_steps if step.get("id") == "eros_e2e_evidence")
    assert evidence["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert 'select(.name == "Tests (workspace)")' in evidence["run"]
    assert 'select(.name == "Run stock Eros SQLx and synthetic conversation E2E")' in evidence["run"]
    assert component_plan["env"]["E2E_OUTCOME"] == "${{ steps.eros_e2e_evidence.outputs.outcome }}"
    assert "require_live_eros_e2e_success" in plan_run
    assert "E2E_REQUIRED" not in plan_run


def test_merge_bot_can_deploy_exact_current_main_sha_automatically(tmp_path):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    deploy = workflow["jobs"]["deploy"]
    gate = next(step for step in deploy["steps"]
                if step.get("name") == "Recheck exact SHA, current main and owner approval")
    sha = "a" * 40
    fake_gh = tmp_path / "gh"
    try:
        fake_gh.write_text("#!/bin/sh\nprintf '%s\\n' '" + sha + "'\n", encoding="utf-8")
        fake_gh.chmod(0o700)
        env = os.environ.copy()
        env.update({
            "PATH": str(tmp_path) + os.pathsep + env.get("PATH", ""),
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REPOSITORY": "akhripunov/gpoint",
            "GITHUB_SHA": sha,
            "REQUESTED_SHA": sha,
            "WORKFLOW_SHA": sha,
            "APPROVE_PRODUCTION": "false",
            "OWNER_APPROVAL_ACTOR": "akhripunov",
            "OWNER_TRIGGERING_ACTOR": "collaborator",
            "OWNER_REPOSITORY": "akhripunov",
        })

        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", gate["run"]], env=env,
            capture_output=True, text=True, check=False,
        )

        assert result.returncode == 0, result.stderr
    finally:
        fake_gh.unlink(missing_ok=True)


def test_non_owner_cannot_rerun_migration_rehearsal(tmp_path):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    rehearsal = _REHEARSAL["jobs"]["migration_rehearsal"]
    gate = next(step for step in rehearsal["steps"]
                if step.get("name") == "Require exact main-only owner initiated rehearsal")
    sha = "a" * 40
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", gate["run"]],
        env={**os.environ,
             "GITHUB_EVENT_NAME": "workflow_dispatch",
             "GITHUB_REF": "refs/heads/main",
             "GITHUB_SHA": sha,
             "REQUESTED_SHA": sha,
             "APPROVE_PRODUCTION": "false",
             "OWNER_ACTOR": "akhripunov",
             "OWNER_TRIGGERING_ACTOR": "collaborator",
             "REPOSITORY_OWNER": "akhripunov"},
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "triggering actor" in result.stderr.lower()


def test_deploy_uses_only_active_migration_ledgers_before_apply():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    setup = next((i for i, step in enumerate(steps)
                  if step.get("uses", "").startswith("actions/setup-python@")), None)
    install = next((i for i, step in enumerate(steps)
                    if "services/agata" in step.get("run", "")), None)
    parity = next(i for i, step in enumerate(steps)
                  if step.get("name") == "Verify candidate compatibility with the live database baseline")

    assert install is None
    assert setup is not None and setup < parity
    assert "--policy compatible" in steps[parity]["run"]
    assert "--prisma-only" in steps[parity]["run"]
    for step in steps:
        if "verify-kamal-migration-parity.py" in step.get("run", ""):
            assert "--prisma-only" in step["run"] or "--static-pending" in step["run"]
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    assert "gsales-agata-api" not in workflow_text
    assert "gsales-agata-memory-worker" not in workflow_text
    assert "services/agata/migrations" not in workflow_text
    assert "verify-kamal-migration-parity.py" in workflow_text


def test_kamal_inventory_summary_contains_only_the_crosschecked_version():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    inventory = next(step for step in workflow["jobs"]["deploy"]["steps"]
                     if step.get("id") == "kamal_inventory")

    assert "`" not in inventory["run"]
    assert "Current version: **%s**" in inventory["run"]
    assert "details" not in inventory["run"]
    assert "containers" not in inventory["run"]


def test_release_event_routing_is_fail_closed_and_does_not_give_prs_prod_access():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    intent = workflow["jobs"]["tests"]
    assert "if" not in intent
    assert "environment" not in intent
    intent_script = next(step.get("run", "") for step in intent["steps"]
                         if step.get("id") == "intent")
    assert "--backup-maintenance" not in intent_script
    assert (ROOT / ".github/workflows/gpoint-operations.yml").exists()
    assert "GPOINT_MIGRATION_REHEARSAL_PREP" not in intent_script
    assert 'return 2' in (ROOT / "scripts/native_release_intent.py").read_text()
    deploy = workflow["jobs"]["deploy"]
    assert "needs.tests.outputs.deploy == 'true'" in deploy["if"]
    assert "pull_request" not in deploy["if"]
    assert "production" not in workflow["jobs"]["tests"].get("environment", {})
    assert workflow["permissions"] == {"contents": "read"}


def test_runtime_verify_rechecks_public_routes_and_five_target_roles_after_deploy():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy = workflow.split("  deploy:\n", 1)[1]
    deploy_step = deploy.index("name: Deploy exact immutable images with stock Kamal")
    verify_step = deploy.index("name: Verify public routes and exact running services")
    assert "name: Record GitHub Deployment result against the same attempt" not in deploy
    verify = deploy[verify_step:]
    assert deploy_step < verify_step
    assert '"${PENDING_PRISMA_MIGRATIONS:--}" "${PENDING_PRISMA_CHECKSUMS:--}"' in verify
    assert '[[ "$pending_prisma_migrations" != "-" ]] || pending_prisma_migrations=""' in verify
    assert '[[ "$pending_prisma_checksums" != "-" ]] || pending_prisma_checksums=""' in verify
    for expected in (
        "https://gpoint.one/api/health/live",
        "gpoint-core-api",
        "gpoint-core-worker",
        "gpoint-core-bot",
        "gpoint-miniapp-web",
        "gpoint-eros-pilot-engine",
        "kamal-proxy",
        "shared_postgres_1",
        "shared_redis_1",
        "PRISMA_HEAD",
        "GPOINT_MEDIA_READ_WRITE=PASS",
        "verify-layer7-ollama.py",
    ):
        assert expected in verify
    assert "'<title>GPoint</title>'" in verify
    assert 'docker exec "gpoint-core-bot-$core_version" node -e' in verify
    assert "http://127.0.0.1:9091/health/ready" in verify
    assert "GPOINT_BOT_READY=PASS" in verify
    assert "if grep -q '^gpoint-core-bot|' <<< \"$runtime_rows\"; then" in verify
    assert "apps/bot/dist/main.js" not in verify
    helper = (ROOT / "scripts/verify-layer7-ollama.py").read_text(encoding="utf-8")
    assert 'BASE_URL = "http://ollama:11434"' in helper
    assert 'const base = "http://ollama:11434"' in helper


def test_runtime_smokes_validate_the_public_miniapp_on_ordinary_and_rollback_paths():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    verify = next(step["run"] for step in workflow["jobs"]["deploy"]["steps"]
                  if step.get("name") == "Verify public routes and exact running services")
    rollback = next(step["run"] for step in workflow["jobs"]["deploy"]["steps"]
                    if step.get("name") == "Verify rollback Runtime and preserve databases before SUCCESS")
    for script in (verify, rollback):
        assert 'for path in / /creators; do' in script
        assert 'curl --retry' in script and '--fail --silent --show-error "https://gpoint.one$path"' in script
        assert "<title>GPoint</title>" in script


def test_verify_only_component_scope_runs_runtime_verification_without_kamal_replacements():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    apply = next(step for step in steps
                 if step.get("name") == "Deploy exact immutable images with stock Kamal")
    verify = next(step for step in steps
                  if step.get("name") == "Verify public routes and exact running services")
    run = apply["run"]

    for component in ("CORE", "EROS", "MINIAPP"):
        assert f'if [[ "$DEPLOY_{component}" == true ]]; then' in run
    assert "(success()) && inputs.mode != 'rollback'" in apply["if"]
    assert verify["if"].endswith("inputs.mode != 'rollback')")
    assert "EXPECTED_ROLE_IDENTITIES_B64" in verify["run"]
    for role in ("gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot",
                 "gpoint-miniapp-web", "gpoint-eros-pilot-engine"):
        assert role in verify["run"]


def test_production_preflight_accepts_historical_version_source_sha_split_but_checks_digest_identity():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy = workflow.split("  deploy:\n", 1)[1]
    inventory = deploy.split("name: Read live application version with Kamal", 1)[1].split(
        "name: Verify installed Kamal topology before ordinary release", 1
    )[0]
    preflight = deploy.split("name: Verify installed Kamal topology before ordinary release", 1)[1].split(
        "name: Verify candidate compatibility with the live database baseline", 1
    )[0]
    assert "verify-kamal-production-baseline.py" in inventory
    assert "LIVE_ROLE_INVENTORY" in preflight
    assert '"$version" == "$source_sha"-*' not in preflight
    assert "RepoDigests" in (ROOT / "scripts/verify-kamal-production-baseline.py").read_text()
    assert "org.opencontainers.image.revision" in (ROOT / "scripts/verify-kamal-production-baseline.py").read_text()
    assert "GPOINT_BASELINE_ROLE_VERSION" in preflight
    assert "GPOINT_BASELINE_ROLE_SOURCE_SHA" in preflight
    assert "DB_MOUNTS_SHA256" in preflight
    assert "REDIS_MOUNTS_SHA256" in preflight
    verify = deploy.split("name: Verify public routes and exact running services", 1)[1].split(
        "name: Report recovery guidance after ordinary release", 1
    )[0]
    assert "DB_MOUNTS_SHA256" in verify and "REDIS_MOUNTS_SHA256" in verify
    assert "EXPECTED_ROLE_IDENTITIES_B64" in verify
    assert "core_version=\"$(sed -n 's/^gpoint-core-api|" in verify
    assert "agata_version=" not in verify
    assert "BASELINE_PRISMA_HEAD" in verify and "BASELINE_AGATA_HEAD" not in verify
    assert 'test "$prisma_migrations" = "$expected_prisma_migrations"' in verify
    assert 'test "$prisma_checksums" = "$expected_prisma_checksums"' in verify
    assert "find packages/db/prisma/migrations" not in verify


def test_core_consumers_are_updated_one_role_at_a_time_and_partial_failure_fails_closed():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy = workflow.split("  deploy:\n", 1)[1]
    apply = deploy.split("name: Deploy exact immutable images with stock Kamal", 1)[1].split(
        "name: Verify public routes and exact running services", 1
    )[0]
    eros = apply.split('if [[ "$DEPLOY_EROS" == true ]]; then', 1)[1].split(
        'if [[ "$DEPLOY_CORE" == true ]]; then', 1
    )[0]
    core = apply.split('if [[ "$DEPLOY_CORE" == true ]]; then', 1)[1].split(
        'if [[ "$DEPLOY_MINIAPP" == true ]]; then', 1
    )[0]
    assert apply.index('if [[ "$DEPLOY_EROS" == true ]]; then') < apply.index('if [[ "$DEPLOY_CORE" == true ]]; then')
    assert apply.index('if [[ "$DEPLOY_CORE" == true ]]; then') < apply.index('if [[ "$DEPLOY_MINIAPP" == true ]]; then')
    assert core.index("KAMAL_ROLES=worker") < core.index("KAMAL_ROLES=bot")
    for block, role in ((core, "worker"), (core, "bot")):
        stop = block.index(f"kamal app stop --roles {role}")
        deploy_call = block.index(f"kamal deploy --skip-push --version \"$IMAGE_VERSION\" --roles {role}")
        assert stop < deploy_call
    assert "KAMAL_ROLES=worker,bot" not in apply
    assert "verify_single_running_role gpoint-core-worker" in apply
    assert "verify_single_running_role gpoint-core-bot" in apply
    assert "verify_single_running_role gpoint-agata-memory-worker" not in apply
    assert "app stop --roles relay" not in apply
    ordinary = deploy.split("  rollback:\n", 1)[0]
    status = ordinary.split("name: Report recovery guidance after ordinary release", 1)[1]
    assert "RECOVERY_REQUIRED" in status
    assert "automatic database downgrade is prohibited" in status
    assert "deployments/$DEPLOYMENT_ID/statuses" not in status


def test_ordinary_deployment_uses_native_environment_status_after_verify():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    deploy = workflow["jobs"]["deploy"]
    names = [step.get("name", "") for step in deploy["steps"]]
    resolve = names.index("Resolve exact GitHub environment Deployment ID")
    verify = names.index("Verify public routes and exact running services")
    assert resolve < verify
    assert "steps.verify.outputs.environment_url" in deploy["environment"]["url"]
    deployment_steps = "\n".join(step.get("run", "") for step in deploy["steps"])
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/deployments' not in deployment_steps
    assert "gpoint-manifest-sha256=" in deployment_steps
    assert "GITHUB_RUN_ATTEMPT" in deploy["steps"][resolve]["run"]


def test_manual_rollback_uses_native_environment_deployment_and_target_metadata():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    rollback = _ROLLBACK["jobs"]["deploy"]
    assert rollback["environment"] == {
        "name": "production",
        "url": "${{ inputs.mode == 'rollback' && steps.rollback_verify.outputs.environment_url || steps.verify.outputs.environment_url }}",
    }
    body = "\n".join(step.get("run", "") for step in rollback["steps"])
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/deployments' not in body
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/deployments/$DEPLOYMENT_ID/statuses' not in body
    assert "gpoint-runtime-target-sha=" in body
    assert "gpoint-core-source-sha=$CORE_SOURCE_SHA" in body
    assert "gpoint-miniapp-source-sha=$MINIAPP_SOURCE_SHA" in body
    assert "gpoint-eros-source-sha=$EROS_SOURCE_SHA" in body
    for key in ("core_version", "miniapp_version", "eros_version"):
        assert f"steps.evidence.outputs.{key}" in yaml.safe_dump(rollback)
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/deployments' not in body
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/deployments/$DEPLOYMENT_ID/statuses' not in body


def test_production_mutations_share_one_non_cancelling_lock():
    native = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "gpoint-production-release" in native["jobs"]["deploy"]["concurrency"]["group"]
    assert native["jobs"]["deploy"]["concurrency"]["cancel-in-progress"] is False
    for workflow_name, job in (("gpoint-operations.yml", "migration_rehearsal"),):
        special = yaml.safe_load((ROOT / ".github/workflows" / workflow_name).read_text())
        assert "gpoint-production-release" in special["jobs"][job]["concurrency"]["group"]
        assert special["jobs"][job]["concurrency"]["cancel-in-progress"] is False


def test_ordinary_release_success_does_not_depend_on_scheduled_backup_freshness():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy = workflow.split("  deploy:\n", 1)[1]
    ordinary_deploy = deploy
    status = ordinary_deploy.split("name: Report recovery guidance after ordinary release", 1)[1]
    assert "Verify public routes and exact running services" in ordinary_deploy
    assert "Verify existing scheduled VPS backups" not in ordinary_deploy
    assert "gpoint-backup.timer" not in ordinary_deploy
    migration_apply = ordinary_deploy.split("id: migration_apply", 1)[1].split(
        "name: Deploy exact immutable images with stock Kamal", 1
    )[0]
    no_migration_path = migration_apply.split('if [[ "$APPLY" == false ]]', 1)[1].split("exit 0", 1)[0]
    assert "gpoint-backup-policy.py" not in no_migration_path
    assert "freshness" not in no_migration_path
    assert 'gpoint-backup-policy.py" freshness' in migration_apply
    assert "BACKUP_OUTCOME" not in status
    assert 'steps.verify.outcome != \'success\'' in status
    assert 'DEPLOY_JOB_STATUS' not in status


def test_backup_maintenance_retains_timer_and_freshness_checks():
    procedure = (ROOT / "scripts/gpoint-backup-install.sh").read_text(encoding="utf-8")
    remote = (ROOT / "scripts/gpoint-backup-install-remote.sh").read_text(encoding="utf-8")
    assert "enable --now gpoint-backup.timer" in remote
    assert "start gpoint-backup.service" in remote
    assert "gpoint-backup-policy.py" in remote and "freshness" in remote
    assert "#!/usr/bin/env bash" in procedure


def test_backup_timer_setup_is_a_separate_exact_sha_owner_authorized_operation():
    """Backups stay independent of the release workflow (MaxiBot reference).

    Daily backups are a VPS systemd timer. Installing or updating that release is
    a protected one-shot procedure outside GitHub Releases, not an extra job.
    """
    procedure = (ROOT / "scripts/gpoint-backup-install.sh").read_text(encoding="utf-8")
    remote = (ROOT / "scripts/gpoint-backup-install-remote.sh").read_text(encoding="utf-8")
    assert 'APPROVE_PRODUCTION' in procedure
    assert 'CANDIDATE_SHA' in procedure
    assert 'git ls-remote origin refs/heads/main' in procedure
    assert 'git status --porcelain' in procedure
    assert 'test "$(sha256 "$STAGE/payload.tar.gz")" = "$ARCHIVE_SHA"' in remote
    assert 'freshness' in remote and 'gpoint-backup-policy.py' in remote
    assert 'GPOINT_BACKUP_INSTALL=PASS' in procedure
    assert 'repos/$GITHUB_REPOSITORY/deployments' not in procedure
    operations = yaml.safe_load((ROOT / ".github/workflows/gpoint-operations.yml").read_text())
    assert "backup_maintenance" not in operations["jobs"]


def test_manual_migration_rehearsal_uses_existing_backup_without_deployment_or_kamal():
    workflow = _REHEARSAL
    events = workflow.get("on", workflow.get(True))
    assert events["workflow_dispatch"]["inputs"]["migration_rehearsal"]["type"] == "boolean"
    rehearsal = _REHEARSAL["jobs"]["migration_rehearsal"]
    assert "inputs.migration_rehearsal" in rehearsal["if"]
    assert "refs/heads/main" in rehearsal["if"]
    assert rehearsal["environment"] == {"name": "production", "deployment": False}
    body = "\n".join(step.get("run", "") for step in rehearsal["steps"])
    assert "GPOINT_MIGRATION_REHEARSAL=1" in body
    assert "/opt/gpoint/release-logs/7a1afde1d94c324828565a36cb7dd90f422dee3d/kamal-backup-restore-41223019715-1.txt" in body
    assert "GPOINT_MIGRATION_BACKUP_EVIDENCE_SHA256" in body
    assert "GPOINT_MIGRATION_BACKUP_EVIDENCE" in body
    assert "GPOINT_MIGRATION_BACKUP_FRESHNESS=PASS" in body
    assert "docker_free" in body and "insufficient Docker storage" in body
    assert "required = app_bytes + agata_bytes + db_bytes * 10 + 2 * 1024**3" in body
    assert 'docker pull --platform linux/amd64 "$app_image"' in body
    assert 'docker pull --platform linux/amd64 "$agata_image"' in body
    rehearsal_step = next(step for step in rehearsal["steps"]
                          if step.get("name") == "Rehearse migrations against existing encrypted VPS backup")
    assert rehearsal_step["env"]["GHCR_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert "rm -rf -- \"$stage/docker-config\"" in body
    assert "GPOINT_MIGRATION_TEMP_CLEANUP=PASS" in body
    assert "cleanup_status=0" in body
    assert "GPOINT_MIGRATION_STAGE_FAILURE=$current_step" in body
    assert "verify_image_id" not in body
    assert ".Id}}'" not in body
    assert "APP_IMAGE_ID" not in body and "AGATA_IMAGE_ID" not in body
    assert "exit \"$rehearsal_status\"" in body
    assert 'current_step=isolated-backup-restore-and-four-migrations' in body
    for image_stage in (
        "ghcr-app-pull", "ghcr-app-platform",
        "ghcr-app-source-revision", "ghcr-app-repository-digest", "ghcr-agata-pull",
        "ghcr-agata-platform",
        "ghcr-agata-source-revision", "ghcr-agata-repository-digest",
        "ghcr-post-pull-capacity",
    ):
        assert f"current_step={image_stage}" in body
    assert "GPOINT_MIGRATION_REHEARSAL=PASS" in body
    assert "kamal deploy" not in body
    assert "repos/$GITHUB_REPOSITORY/deployments" not in body
    assert rehearsal["permissions"] == {"contents": "read", "packages": "read"}
    native = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "needs.tests.outputs.deploy == 'true'" in native["jobs"]["deploy"]["if"]


def test_migration_rehearsal_uses_immutable_oci_identity_across_docker_hosts():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    rehearsal = _REHEARSAL["jobs"]["migration_rehearsal"]
    body = "\n".join(step.get("run", "") for step in rehearsal["steps"])
    build = workflow["jobs"]["build"]
    build_body = "\n".join(step.get("run", "") for step in build["steps"])

    for role, repository in (
        ("app", "ghcr.io/akhripunov/gpoint-runtime"),
        ("agata", "ghcr.io/akhripunov/gpoint-agata-runtime"),
    ):
        image_var = f"{role}_image"
        digest_var = "APP_DIGEST" if role == "app" else "AGATA_DIGEST"
        assert f'[[ "${digest_var}" =~ ^sha256:[0-9a-f]{{64}}$ ]]' in body
        assert f'{image_var}="{repository}@$' in body
        assert f'[[ "${image_var}" =~ ^ghcr\\.io/akhripunov/' in body
        assert f'@sha256:[0-9a-f]{{64}}$ ]]' in body
        assert f'docker pull --platform linux/amd64 "${image_var}"' in body
        assert 'test "$(docker image inspect "$app_image" --format \'{{.Os}} {{.Architecture}}\')" = \'linux amd64\'' in body
        assert 'test "$(docker image inspect "$agata_image" --format \'{{.Os}} {{.Architecture}}\')" = \'linux amd64\'' in body
        assert '"org.opencontainers.image.revision"' in body
        assert 'test "$(docker image inspect "$app_image" --format \'{{index .Config.Labels "org.opencontainers.image.revision"}}\')" = "$candidate"' in body
        assert 'test "$(docker image inspect "$agata_image" --format \'{{index .Config.Labels "org.opencontainers.image.revision"}}\')" = "$candidate"' in body
        assert 'docker image inspect "$app_image" --format \'{{json .RepoDigests}}\' | grep -F "$app_image" >/dev/null' in body
        assert 'docker image inspect "$agata_image" --format \'{{json .RepoDigests}}\' | grep -F "$agata_image" >/dev/null' in body
        assert f'current_step=ghcr-{role}-repository-digest' in body

    assert build["outputs"]["app_digest"] == "${{ steps.app.outputs.digest }}"
    assert "agata_digest" not in build["outputs"]
    assert "app_image_id" not in build.get("outputs", {})
    assert "agata_image_id" not in build.get("outputs", {})
    assert "image inspect" in build_body and "{{.Id}}" not in build_body
    assert "{{json .RepoDigests}}" in build_body
    assert "GPOINT_MIGRATION_IMAGE_ID_MISMATCH" not in body
    assert 'docker image inspect "$app_image" --format \'{{.Size}}\')" = "$app_image_bytes"' not in body
    assert 'docker image inspect "$agata_image" --format \'{{.Size}}\')" = "$agata_image_bytes"' not in body
    assert 'docker_free_after_pull="$(df -PB1 "$docker_root" | awk ' in body
    assert "required_bytes = database_bytes * 10 + 2 * 1024**3" in body
    assert "GPOINT_MIGRATION_POST_PULL_CAPACITY=PASS" in body


def test_migration_rehearsal_distinguishes_synthetic_rls_from_production_grants():
    helper = (ROOT / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    runner = (ROOT / "scripts/gpoint-scheduled-backup.py").read_text()
    assert "MIGRATION_SYNTHETIC_RLS=PASS" in helper
    assert "MIGRATION_PRODUCTION_GRANTS_RLS=NOT_CHECKED" in helper
    assert "MIGRATION_SYNTHETIC_RLS" in runner
    assert "MIGRATION_PRODUCTION_GRANTS_RLS" in runner


def test_runtime_image_targets_stage_with_prisma_migration_history():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    build = workflow["jobs"]["build"]
    app_build = next(step for step in build["steps"]
                     if step.get("name") == "Build and publish immutable runtime image")
    assert app_build["with"]["target"] == "runtime"
    helper = (ROOT / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    for migration in (
        "20261016120000_eros_worker_queue",
        "20261020153000_eros_external_relationship_mapping",
        "20261022110000_eros_gpoint_message_state",
    ):
        assert f"/app/packages/db/prisma/migrations/{migration}/migration.sql" in helper


def test_scheduled_vps_backup_uploads_and_restores_same_bunny_snapshot_during_transition():
    policy_path = ROOT / "scripts/gpoint-backup-policy.py"
    runner_path = ROOT / "scripts/gpoint-scheduled-backup.py"
    helper_path = ROOT / "scripts/verify-gpoint-scheduled-backup-restore.sh"
    assert policy_path.is_file()
    assert runner_path.is_file()
    assert helper_path.is_file()
    policy = policy_path.read_text(encoding="utf-8")
    runner = runner_path.read_text(encoding="utf-8")
    helper = helper_path.read_text(encoding="utf-8")
    assert "isolated_restore" in policy and "sha256" in policy
    assert "verify_bunny_evidence(manifest)" in policy
    assert "gpoint-backups" in policy
    assert "_bunny_remote_root" in runner and "_publish_bunny_receipt" in runner
    assert "rclone" in runner and "rclone" in helper
    assert "--immutable" in helper
    assert 'rclone copyto --config "$rclone_config" "$object" "${bunny_readback_dir}/${name}"' in helper
    assert 'sha256sum "$source"' in helper and 'sha256sum "${bunny_readback_dir}/${name}"' in helper
    assert "--download --one-way" not in helper
    assert "restore_backup_file=" in helper and "restore_media_backup_file=" in helper
    assert 'OFFSITE_RESTORE=$(' in helper and 'MEDIA_OFFSITE_RESTORE=$(' in helper
    assert "gpoint-backup.agekey" in runner and "age-keygen" in runner
    assert "/opt/gpoint/layer7-staging/media" in runner and "media-tree" in runner
    assert "def _retain_local(now: float)" in runner


def test_protected_rollback_uses_only_verified_prior_target_and_shared_lock():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    rollback = _ROLLBACK["jobs"]["deploy"]
    assert "inputs.mode == 'rollback'" in rollback["if"]
    assert "inputs.mode == 'rollback'" in rollback["if"]
    assert rollback["environment"] == {
        "name": "production", "url": "${{ inputs.mode == 'rollback' && steps.rollback_verify.outputs.environment_url || steps.verify.outputs.environment_url }}",
    }
    assert "gpoint-production-release" in rollback["concurrency"]["group"]
    assert rollback["concurrency"]["cancel-in-progress"] is False
    assert rollback["permissions"] == {"contents": "read", "packages": "read", "actions": "read", "deployments": "write"}
    body = yaml.safe_dump(rollback)
    names = [step.get("name", step.get("uses", "")) for step in rollback["steps"]]
    assert names.index("Validate linked build, successful Runtime Verify and component-role manifest") < names.index("Verify target digests, platform, revision and provenance")
    assert names.index("Verify target digests, platform, revision and provenance") < names.index("Verify live DB compatibility and retained target containers before mutation")
    assert names.index("Verify live DB compatibility and retained target containers before mutation") < names.index("Roll back consumers and then APIs with stock Kamal")
    assert names.index("Verify retained Eros rollback containers") < names.index("Verify Eros rollback image against an isolated current database restore")
    assert names.index("Verify Eros rollback image against an isolated current database restore") < names.index("Roll back consumers and then APIs with stock Kamal")
    assert "(inputs.rollback_eros == true) && inputs.mode == 'rollback'" in rollback["steps"][names.index("Verify Eros rollback image against an isolated current database restore")]["if"]
    assert names.index("Roll back consumers and then APIs with stock Kamal") < names.index("Verify rollback Runtime and preserve databases before SUCCESS")
    for gate in (
        "rollback_target_sha", "rollback_deployment_id", "verify_native_rollback_evidence.py",
        "verify_native_image_readback.py", "verify_native_image_attestation.py",
        "verify-kamal-migration-parity.py", "core_version", "eros_version",
        "miniapp_version", "KAMAL_ROLES=worker", "KAMAL_ROLES=bot",
        "app stop --roles",
        "RECOVERY_REQUIRED", "steps.verify.outputs.environment_url",
    ):
        assert gate in body
    events = _ROLLBACK.get("on", _ROLLBACK.get(True))
    inputs = events["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["default"] == "deploy"
    assert "rollback" in inputs["mode"]["options"]
    assert inputs["rollback_target_sha"]["type"] == "string"
    assert "rollback_target_sha" in inputs
    assert "rollback_deployment_id" in inputs
    assert "outputs.deploy == 'true'" in workflow["jobs"]["deploy"]["if"]


def test_deploy_publishes_release_timing_even_when_the_release_fails():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    step = next(item for item in steps if item.get("name") == "Publish release timing")
    assert step["if"].endswith("(always())")
    assert "GPOINT_RELEASE_TIMING=REPORTED" in step["run"]
    assert "GPOINT_RELEASE_TIMING=UNVERIFIED" in step["run"]
    assert 'jobs?per_page=100' in step["run"]


def test_production_deploy_scope_is_resolved_only_from_live_inventory():
    """Even an apparently no-op push reads installed roles before deciding Apply."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    condition = workflow["jobs"]["deploy"]["if"]
    assert "needs.build.outputs.deploy_scope" not in condition
    assert "needs.tests.outputs.deploy == 'true'" in condition
    assert "needs.build.result == 'success'" in condition
    assert not any(step.get("id") == "build_scope"
                   for step in workflow["jobs"]["build"]["steps"])
    component_plan = next(step for step in workflow["jobs"]["deploy"]["steps"]
                          if step.get("id") == "component_plan")
    assert "classify_component_scope" in component_plan["run"]


def test_release_apply_has_no_relay_component_or_role():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    apply = next(s for s in steps if s.get("id") == "kamal_apply")["run"]
    build = workflow["jobs"]["build"]
    assert "EROS_RELAY" not in apply
    assert "DEPLOY_EROS_RELAY" not in apply
    assert "--roles relay" not in apply
    assert "EROS_RELAY" not in str(build)


def test_topology_preflight_allows_missing_bootstrap_roles_and_absent_proxy_only_when_ports_are_free():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    inventory = next(step for step in steps if step.get("id") == "kamal_inventory")
    topology = next(step for step in steps if step.get("id") == "topology")

    assert "verify-kamal-production-baseline.py" in inventory["run"]
    assert "missing_roles" in inventory["run"]
    assert "role not in roles" in topology["run"]
    assert "GPOINT_LIVE_ROLE_INVENTORY" in inventory["run"]
    assert any("GPOINT_LIVE_ROLE_INVENTORY" in value for value in topology["env"].values())
    assert "legacy_relay" not in topology["run"]
    assert "gpoint-eros-relay-relay" not in inventory["run"]
    assert "ss -ltn" in topology["run"]
    assert 'publish=$port' in topology["run"] and 'for port in 80 443' in topology["run"]
    assert "test -n \"$ids\" || { echo \"expected stopped legacy Compose service is missing" not in topology["run"]
    assert "legacy Compose service is still active" in topology["run"]
    assert "shared_postgres_1" in topology["run"] and "shared_redis_1" in topology["run"]
    assert "DB_MOUNTS_SHA256" in topology["run"] and "REDIS_MOUNTS_SHA256" in topology["run"]
    assert 'docker inspect --format \'{{.State.Running}}\' kamal-proxy' in topology["run"]


def test_rollback_preflight_checks_retained_target_after_consumer_stopped():
    steps = _ROLLBACK["jobs"]["deploy"]["steps"]
    preflight = next(step for step in steps if step.get("id") == "baseline")
    role_checks = preflight["run"].split("rollback_roles=(", 1)[1]
    role_checks = "rollback_roles=(" + role_checks.split('\ndb_mounts=', 1)[0]
    script = r'''
set -euo pipefail
version_holder=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
core_version="$version_holder-91-1"
miniapp_version="$core_version"
core_sha="$version_holder"
miniapp_sha="$version_holder"
core='ghcr.io/akhripunov/gpoint-runtime@sha256:example'
miniapp="$core"
bot_enabled=false
docker() {
    case "$1 $2" in
      "ps --no-trunc")
        if [[ "$prefix" == gpoint-core-worker ]]; then
          printf '%s' "$MOCK_WORKER"
        else
          printf '%s\n' "$prefix-$core_version"
        fi ;;
      "ps -aq")
        if [[ "$4" != *gpoint-core-worker* || "$MOCK_RETAINED" == true ]]; then
          printf 'retained\n'
        fi ;;
      "inspect --format")
        case "$3" in
          '{{.Name}}') printf '/%s\n' "$prefix-$core_version" ;;
          '{{.Image}}') printf 'image\n' ;;
          *) printf '%s\n' "$version_holder" ;;
        esac ;;
      "image inspect") printf '["%s"]\n' "$core" ;;
      *) return 2 ;;
    esac
}
''' + role_checks
    env = {**os.environ, "MOCK_WORKER": "", "MOCK_RETAINED": "true"}
    result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "ROLLBACK_WORKER_RUNNING=false" in result.stdout

    for changes in ({"MOCK_RETAINED": "false"},
                    {"MOCK_WORKER": f"gpoint-core-worker-{'a' * 40}-91-1\ngpoint-core-worker-{'b' * 40}-92-1\n"}):
        result = subprocess.run(["bash", "-c", script], env={**env, **changes}, capture_output=True)
        assert result.returncode != 0

    apply = next(step for step in steps if step.get("id") == "rollback_apply")
    assert apply["env"]["ROLLBACK_WORKER_RUNNING"] == "${{ steps.baseline.outputs.ROLLBACK_WORKER_RUNNING }}"
    assert 'if [[ "$ROLLBACK_WORKER_RUNNING" == true ]]; then' in apply["run"]
    assert 'verify_role gpoint-core-worker "$CORE_VERSION" "$CORE_SOURCE_SHA"' in apply["run"]


def test_eros_inventory_accepts_kamal_version_without_container_name(tmp_path):
    import json

    steps = _ROLLBACK["jobs"]["deploy"]["steps"]
    inventory = next(step for step in steps if step.get("id") == "kamal_inventory")
    topology = next(step for step in steps if step.get("id") == "topology")
    version = "a" * 40 + "-93-1"
    roles = {"gpoint-eros-pilot-engine": {"version": version}}
    role_inventory = json.dumps({"roles": roles, "missing_roles": [], "mixed_components": []})
    output = tmp_path / "output"
    env = {
        **os.environ,
        "GITHUB_WORKSPACE": str(ROOT),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GPOINT_PRODUCTION_HOST": "example.invalid",
        "KAMAL_IMAGE": "unused",
        "TEST_ROLE_INVENTORY": role_inventory,
        "TEST_VERSION": version,
    }
    fake_commands = '''
ssh() { printf '%s\\n' "$TEST_ROLE_INVENTORY"; }
docker() { printf 'example.invalid: %s\\n' "$TEST_VERSION"; }
'''
    result = subprocess.run(["bash", "-c", fake_commands + inventory["run"]],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert f"eros_version={version}" in output.read_text().splitlines()

    check = topology["run"].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    env.update(KAMAL_CORE_VERSION="ABSENT", KAMAL_MINIAPP_VERSION="ABSENT",
               KAMAL_EROS_VERSION=version)
    result = subprocess.run(["python3", "-c", check, role_inventory],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    env["KAMAL_EROS_VERSION"] = "b" * 40 + "-93-1"
    result = subprocess.run(["python3", "-c", check, role_inventory],
                            env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "gpoint-eros-pilot-engine" in result.stderr
