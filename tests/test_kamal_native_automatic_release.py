import os
from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-native-release.yml"


def test_main_push_runs_exact_candidate_tests_build_kamal_and_verify():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    events = workflow.get("on", workflow.get(True))
    assert events["push"]["branches"] == ["main"]
    assert events["pull_request"]["branches"] == ["main"]
    deploy = workflow["jobs"]["deploy"]
    assert "build" in deploy["needs"]
    assert "push" in deploy.get("if", "")
    assert "refs/heads/main" in deploy.get("if", "")
    assert "workflow_dispatch" in deploy.get("if", "")
    assert deploy["environment"] == {
        "name": "production",
        "url": "${{ steps.verify.outputs.environment_url }}",
    }
    build = workflow["jobs"]["build"]
    assert "release_intent" not in workflow["jobs"]
    assert build["needs"] == ["tests"]
    assert "outputs.mode == 'DEPLOY'" in build["if"]
    assert "outputs.mode == 'REHEARSAL'" in build["if"]
    assert "outputs.mode == 'ARTIFACT_PREPARE'" in build["if"]
    assert "environment" not in build
    assert "packages: write" in WORKFLOW.read_text(encoding="utf-8")
    assert deploy["needs"] == ["tests", "build"]


def test_verified_tooling_only_push_skips_images_and_requires_live_noop_scope():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    build = workflow["jobs"]["build"]
    steps = build["steps"]
    resolver = next(step for step in steps if step.get("id") == "build_scope")
    assert "--push-before" in resolver["run"]
    assert "VERIFY_ONLY" in resolver["run"]
    assert "VERIFY_ONLY" in resolver["env"].get("EVENT_BEFORE", "") or "EVENT_BEFORE" in resolver["env"]
    for step in steps:
        if step.get("uses", "").startswith(("docker/setup-buildx-action@", "docker/login-action@")):
            assert "build_components != '[]'" in step["if"]

    deploy_steps = workflow["jobs"]["deploy"]["steps"]
    component_plan = next(step for step in deploy_steps if step.get("id") == "component_plan")
    assert 'elif mode == "VERIFY_ONLY"' in component_plan["run"]
    assert 'scope["scope"] != "NONE"' in component_plan["run"]
    migration_plan = next(step for step in deploy_steps if step.get("id") == "migration_plan")
    assert "LIVE_COMPONENT_SCOPE" in migration_plan["env"]
    assert "verify-only release has pending Prisma migrations" in migration_plan["run"]


def test_rerun_by_non_owner_cannot_deploy_a_main_push(tmp_path):
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

        assert result.returncode != 0
        assert "triggering actor" in result.stderr.lower()
    finally:
        fake_gh.unlink(missing_ok=True)


def test_non_owner_cannot_rerun_migration_rehearsal(tmp_path):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    rehearsal = workflow["jobs"]["migration_rehearsal"]
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


def test_deploy_installs_the_pinned_agata_alembic_runtime_before_migration_checks():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    setup = next((i for i, step in enumerate(steps)
                  if step.get("uses", "").startswith("actions/setup-python@")), None)
    install = next((i for i, step in enumerate(steps)
                    if "python -m pip install ./services/agata" in step.get("run", "")), None)
    parity = next(i for i, step in enumerate(steps)
                  if step.get("name") == "Verify candidate compatibility with the live database baseline")

    assert setup is not None
    assert install is not None
    assert setup < install < parity
    assert steps[setup]["with"]["python-version"] == "3.12"
    assert steps[setup]["with"]["cache-dependency-path"] == "services/agata/pyproject.toml"


def test_kamal_inventory_summary_uses_shellcheck_safe_markdown_fences():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    inventory = next(step for step in workflow["jobs"]["deploy"]["steps"]
                     if step.get("id") == "kamal_inventory")

    assert "`" not in inventory["run"]
    assert r"\140\140\140text" in inventory["run"]

    lines = inventory["run"].splitlines()
    printf_index = next(i for i, line in enumerate(lines)
                        if "printf 'Current version:" in line)
    printf_command = "\n".join(line.strip() for line in lines[printf_index:printf_index + 2])
    result = subprocess.run(
        ["bash", "-c", "version=sha; containers=containers; details=details; " + printf_command],
        check=True, capture_output=True, text=True,
    )
    assert result.stdout == (
        "Current version:\n\n```text\nsha\n```\n\n"
        "Containers:\n\n```text\ncontainers\n```\n\n"
        "Details:\n\n```text\ndetails\n```\n\n"
    )


def test_release_event_routing_is_fail_closed_and_does_not_give_prs_prod_access():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    intent = workflow["jobs"]["tests"]
    assert "if" not in intent
    assert "environment" not in intent
    intent_script = next(step.get("run", "") for step in intent["steps"]
                         if step.get("id") == "intent")
    assert "--backup-maintenance" in intent_script
    assert "GPOINT_MIGRATION_REHEARSAL_PREP" not in intent_script
    assert 'return 2' in (ROOT / "scripts/native_release_intent.py").read_text()
    deploy = workflow["jobs"]["deploy"]
    assert "needs.tests.outputs.deploy == 'true'" in deploy["if"]
    assert "pull_request" not in deploy["if"]
    assert "production" not in workflow["jobs"]["tests"].get("environment", {})
    assert workflow["permissions"] == {"contents": "read"}


def test_runtime_verify_rechecks_public_and_all_six_kamal_roles_after_deploy():
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
        "gpoint-agata-api",
        "gpoint-agata-memory-worker",
        "gpoint-miniapp-web",
        "kamal-proxy",
        "shared_postgres_1",
        "shared_redis_1",
        "PRISMA_HEAD",
        "AGATA_HEAD",
        "GPOINT_MEDIA_READ_WRITE=PASS",
    ):
        assert expected in verify


def test_verify_only_component_scope_runs_runtime_verification_without_kamal_replacements():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    apply = next(step for step in steps
                 if step.get("name") == "Deploy exact immutable images with stock Kamal")
    verify = next(step for step in steps
                  if step.get("name") == "Verify public routes and exact running services")
    run = apply["run"]

    for component in ("CORE", "AGATA", "MINIAPP"):
        assert f'if [[ "$DEPLOY_{component}" == true ]]; then' in run
    assert apply["if"] == "success()"
    assert "if" not in verify
    assert "EXPECTED_ROLE_IDENTITIES_B64" in verify["run"]
    for role in ("gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot",
                 "gpoint-agata-api", "gpoint-agata-memory-worker", "gpoint-miniapp-web"):
        assert role in verify["run"]


def test_production_preflight_accepts_historical_version_source_sha_split_but_checks_digest_identity():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy = workflow.split("  deploy:\n", 1)[1].split("  backup_maintenance:\n", 1)[0]
    preflight = deploy.split("name: Verify installed Kamal topology before ordinary release", 1)[1].split(
        "name: Verify candidate compatibility with the live database baseline", 1
    )[0]
    assert "verify-kamal-production-baseline.py" in preflight
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
    assert "agata_version=\"$(sed -n 's/^gpoint-agata-api|" in verify
    assert "BASELINE_PRISMA_HEAD" in verify and "BASELINE_AGATA_HEAD" in verify
    assert 'test "$prisma_migrations" = "$expected_prisma_migrations"' in verify
    assert 'test "$prisma_checksums" = "$expected_prisma_checksums"' in verify
    assert "find packages/db/prisma/migrations" not in verify


def test_core_consumers_are_updated_one_role_at_a_time_and_partial_failure_fails_closed():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy = workflow.split("  deploy:\n", 1)[1].split("  backup_maintenance:\n", 1)[0]
    apply = deploy.split("name: Deploy exact immutable images with stock Kamal", 1)[1].split(
        "name: Verify public routes and exact running services", 1
    )[0]
    worker = apply.index("KAMAL_ROLES=worker")
    bot = apply.index("KAMAL_ROLES=bot")
    memory_worker = apply.index("KAMAL_ROLES=memory-worker")
    assert worker < bot < memory_worker
    for role in ("worker", "bot", "memory-worker"):
        stop = apply.index(f"kamal app stop --roles {role}")
        deploy_call = apply.index(f"kamal deploy --skip-push --version \"$IMAGE_VERSION\" --roles {role}")
        assert stop < deploy_call
    assert "KAMAL_ROLES=worker,bot" not in apply
    assert "verify_single_running_role gpoint-core-worker" in apply
    assert "verify_single_running_role gpoint-core-bot" in apply
    assert "verify_single_running_role gpoint-agata-memory-worker" in apply
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
    assert deploy["environment"]["url"] == "${{ steps.verify.outputs.environment_url }}"
    deployment_steps = "\n".join(step.get("run", "") for step in deploy["steps"])
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/deployments' not in deployment_steps
    assert "gpoint-manifest-sha256=" in deployment_steps
    assert "GITHUB_RUN_ATTEMPT" in deploy["steps"][resolve]["run"]


def test_manual_rollback_uses_native_environment_deployment_and_target_metadata():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    rollback = workflow["jobs"]["rollback"]
    assert rollback["environment"] == {
        "name": "production",
        "url": "${{ steps.verify.outputs.environment_url }}",
    }
    body = "\n".join(step.get("run", "") for step in rollback["steps"])
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/deployments' not in body
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/deployments/$DEPLOYMENT_ID/statuses' not in body
    assert "gpoint-runtime-target-sha=" in body
    assert "gpoint-core-source-sha=$CORE_SOURCE_SHA" in body
    assert "gpoint-agata-source-sha=$AGATA_SOURCE_SHA" in body
    assert "gpoint-miniapp-source-sha=$MINIAPP_SOURCE_SHA" in body
    for key in ("core_version", "agata_version", "miniapp_version"):
        assert f"steps.evidence.outputs.{key}" in yaml.safe_dump(rollback)
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/deployments' not in body
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/deployments/$DEPLOYMENT_ID/statuses' not in body


def test_production_mutations_share_one_non_cancelling_lock():
    native = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    first_cutover = yaml.safe_load((ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text())
    legacy_deploy = yaml.safe_load((ROOT / ".github/workflows/gpoint-deploy.yml").read_text())
    recovery = yaml.safe_load((ROOT / ".github/workflows/gpoint-recover-interrupted.yml").read_text())
    expected = {"group": "gpoint-production-release", "cancel-in-progress": False}
    for job in ("deploy", "backup_maintenance", "migration_rehearsal"):
        assert native["jobs"][job]["concurrency"] == expected
    assert first_cutover["jobs"]["deploy"]["concurrency"] == expected
    assert legacy_deploy["concurrency"] == expected
    assert recovery["concurrency"] == expected


def test_ordinary_release_success_does_not_depend_on_scheduled_backup_freshness():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy = workflow.split("  deploy:\n", 1)[1]
    ordinary_deploy = deploy[:deploy.index("  rollback:")]
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
    assert "gpoint-backup-policy.py freshness" in migration_apply
    assert "BACKUP_OUTCOME" not in status
    assert 'steps.verify.outcome != \'success\'' in status
    assert 'DEPLOY_JOB_STATUS' not in status


def test_backup_maintenance_retains_timer_and_freshness_checks():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    maintenance = workflow.split("  backup_maintenance:\n", 1)[1]
    assert "systemctl enable --now gpoint-backup.timer" in maintenance
    assert "systemctl start gpoint-backup.service" in maintenance
    assert "gpoint-backup-policy.py" in maintenance and "freshness" in maintenance


def test_backup_timer_setup_is_a_separate_exact_sha_owner_authorized_operation():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    events = workflow.get("on", workflow.get(True))
    inputs = events["workflow_dispatch"]["inputs"]
    assert inputs["backup_maintenance"]["type"] == "boolean"
    job = workflow["jobs"]["backup_maintenance"]
    assert "refs/heads/main" in job["if"]
    assert "inputs.backup_maintenance == true" in job["if"]
    assert "BACKUP_MAINTENANCE" in job["if"]
    assert job["environment"] == {"name": "production", "deployment": False}
    assert job["permissions"] == {"contents": "read"}
    body = "\n".join(step.get("run", "") for step in job["steps"])
    assert "systemctl enable --now gpoint-backup.timer" in body
    assert "systemctl start gpoint-backup.service" in body
    assert "github.event_name" in job["if"]
    assert "APPROVE_PRODUCTION" in body
    assert "GITHUB_SHA" in body
    assert "repos/$GITHUB_REPOSITORY/deployments" not in body


def test_first_cutover_offsite_restore_gate_is_not_removed():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text(encoding="utf-8")
    assert "GPOINT_KAMAL_BACKUP_OFFSITE_REMOTE" in workflow
    assert "GPOINT_ISOLATED_RESTORE=PASS" in workflow


def test_manual_migration_rehearsal_uses_existing_backup_without_deployment_or_kamal():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    events = workflow.get("on", workflow.get(True))
    assert events["workflow_dispatch"]["inputs"]["migration_rehearsal"]["type"] == "boolean"
    rehearsal = workflow["jobs"]["migration_rehearsal"]
    assert "workflow_dispatch" in rehearsal["if"]
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
    deploy = workflow["jobs"]["deploy"]
    assert "needs.tests.outputs.deploy == 'true'" in deploy["if"]
    assert "inputs.migration_rehearsal != true" in deploy["if"]


def test_migration_rehearsal_uses_immutable_oci_identity_across_docker_hosts():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    rehearsal = workflow["jobs"]["migration_rehearsal"]
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
    assert build["outputs"]["agata_digest"] == "${{ steps.agata.outputs.digest }}"
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
    assert "/opt/gpoint/media" in runner and "media-tree" in runner
    assert "def _retain_local(now: float)" in runner


def test_protected_rollback_uses_only_verified_prior_target_and_shared_lock():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    rollback = workflow["jobs"]["rollback"]
    assert "outputs.mode == 'ROLLBACK'" in rollback["if"]
    assert rollback["environment"] == {
        "name": "production", "url": "${{ steps.verify.outputs.environment_url }}",
    }
    assert rollback["concurrency"] == {"group": "gpoint-production-release", "cancel-in-progress": False}
    assert rollback["permissions"] == {"contents": "read", "packages": "read", "deployments": "write"}
    body = yaml.safe_dump(rollback)
    names = [step.get("name", step.get("uses", "")) for step in rollback["steps"]]
    assert names.index("Validate linked build, successful Runtime Verify and component-role manifest") < names.index("Verify target digests, platform, revision and provenance")
    assert names.index("Verify target digests, platform, revision and provenance") < names.index("Verify live DB compatibility and retained target containers before mutation")
    assert names.index("Verify live DB compatibility and retained target containers before mutation") < names.index("Roll back consumers and then APIs with stock Kamal")
    assert names.index("Verify retained Eros rollback containers") < names.index("Verify Eros rollback image against an isolated current database restore")
    assert names.index("Verify Eros rollback image against an isolated current database restore") < names.index("Roll back consumers and then APIs with stock Kamal")
    assert rollback["steps"][names.index("Verify Eros rollback image against an isolated current database restore")]["if"] == "inputs.rollback_eros == true"
    assert names.index("Roll back consumers and then APIs with stock Kamal") < names.index("Verify rollback Runtime and preserve databases before SUCCESS")
    for gate in (
        "rollback_target_sha", "rollback_deployment_id", "verify_native_rollback_evidence.py",
        "verify_native_image_readback.py", "verify_native_image_attestation.py",
        "verify-kamal-migration-parity.py", "core_version", "agata_version",
        "miniapp_version", "KAMAL_ROLES=worker", "KAMAL_ROLES=bot",
        "KAMAL_ROLES=memory-worker", "app stop --roles",
        "RECOVERY_REQUIRED", "steps.verify.outputs.environment_url",
    ):
        assert gate in body
    events = workflow.get("on", workflow.get(True))
    inputs = events["workflow_dispatch"]["inputs"]
    assert "rollback" in inputs and inputs["rollback"]["default"] is False
    assert "rollback_target_sha" in inputs
    assert "rollback_deployment_id" in inputs
    assert "outputs.deploy == 'true'" in workflow["jobs"]["deploy"]["if"]
