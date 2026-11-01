from pathlib import Path

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
    assert deploy["environment"] == {"name": "production", "deployment": False}
    build = workflow["jobs"]["build"]
    assert "release_intent" in build["needs"]
    assert "workspace" in build["needs"] and "python" in build["needs"]
    assert "outputs.mode == 'DEPLOY'" in build["if"]
    assert "outputs.mode == 'REHEARSAL'" in build["if"]
    assert "environment" not in build
    assert "packages: write" in WORKFLOW.read_text(encoding="utf-8")
    assert {"workspace", "python", "build", "release_intent"}.issubset(deploy["needs"])


def test_release_event_routing_is_fail_closed_and_does_not_give_prs_prod_access():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    intent = workflow["jobs"]["release_intent"]
    assert "pull_request" in intent["if"]
    assert intent["permissions"] == {"contents": "read"}
    intent_script = next(step.get("run", "") for step in intent["steps"]
                         if step.get("id") == "intent")
    assert "--backup-maintenance" in intent_script
    assert "GPOINT_MIGRATION_REHEARSAL_PREP" not in intent_script
    assert 'return 2' in (ROOT / "scripts/native_release_intent.py").read_text()
    deploy = workflow["jobs"]["deploy"]
    assert "needs.release_intent.outputs.deploy == 'true'" in deploy["if"]
    assert "pull_request" not in deploy["if"]
    assert "production" not in workflow["jobs"]["workspace"].get("environment", {})
    assert "production" not in workflow["jobs"]["python"].get("environment", {})
    assert workflow["permissions"] == {"contents": "read"}


def test_runtime_verify_rechecks_public_and_all_six_kamal_roles_after_deploy():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy = workflow.split("  deploy:\n", 1)[1]
    deploy_step = deploy.index("name: Deploy exact immutable images with stock Kamal")
    verify_step = deploy.index("name: Verify public routes and exact running services")
    status_step = deploy.index("name: Record GitHub Deployment result against the same attempt")
    verify = deploy[verify_step:status_step]
    assert deploy_step < verify_step < status_step
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


def test_production_preflight_requires_one_consistent_running_version_and_persistent_identities():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy = workflow.split("  deploy:\n", 1)[1].split("  backup_maintenance:\n", 1)[0]
    preflight = deploy.split("name: Verify installed Kamal topology before ordinary release", 1)[1].split(
        "name: Verify candidate compatibility with the live database baseline", 1
    )[0]
    assert 'test "${#matches[@]}" = 1' in preflight
    assert '"$version" == "$source_sha"-*' in preflight
    assert "GPOINT_BASELINE_ROLE_VERSION" in preflight
    assert "GPOINT_BASELINE_ROLE_SOURCE_SHA" in preflight
    assert "DB_MOUNTS_SHA256" in preflight
    assert "REDIS_MOUNTS_SHA256" in preflight
    verify = deploy.split("name: Verify public routes and exact running services", 1)[1].split(
        "name: Record GitHub Deployment result against the same attempt", 1
    )[0]
    assert "DB_MOUNTS_SHA256" in verify and "REDIS_MOUNTS_SHA256" in verify
    assert "BASELINE_ROLE_VERSION" in verify
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
    status = deploy.split("name: Record GitHub Deployment result against the same attempt", 1)[1]
    assert "RECOVERY_REQUIRED" in status
    assert "automatic rollback is prohibited" in status


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
    status_step = deploy.index("name: Record GitHub Deployment result against the same attempt")
    ordinary_deploy = deploy[:deploy.index("  backup_maintenance:")]
    status = ordinary_deploy[status_step:]
    assert "Verify public routes and exact running services" in ordinary_deploy
    assert "Verify existing scheduled VPS backups" not in ordinary_deploy
    assert "gpoint-backup.timer" not in ordinary_deploy
    assert "gpoint-backup-policy.py" not in ordinary_deploy
    assert "freshness" not in ordinary_deploy
    assert "BACKUP_OUTCOME" not in status
    assert '"$DEPLOY_JOB_STATUS" == success && "$VERIFY_OUTCOME" == success' in status


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
    assert "needs.release_intent.outputs.deploy == 'true'" in deploy["if"]
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
        assert 'docker image inspect "$app_image" --format \'{{json .RepoDigests}}\' | grep -Fq "$app_image"' in body
        assert 'docker image inspect "$agata_image" --format \'{{json .RepoDigests}}\' | grep -Fq "$agata_image"' in body
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


def test_scheduled_vps_backup_is_local_encrypted_fresh_and_restored_without_remote_config():
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
    assert "offsite_readback" not in policy
    assert "rclone" not in runner and "rclone" not in helper
    assert "gpoint-backup.agekey" in runner and "age-keygen" in runner
    assert "/opt/gpoint/media" in runner and "media-tree" in runner


def test_protected_rollback_uses_only_verified_prior_target_and_shared_lock():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    rollback = workflow["jobs"]["rollback"]
    assert "outputs.mode == 'ROLLBACK'" in rollback["if"]
    assert rollback["environment"] == {"name": "production", "deployment": False}
    assert rollback["concurrency"] == {"group": "gpoint-production-release", "cancel-in-progress": False}
    assert rollback["permissions"] == {"contents": "read", "packages": "read", "deployments": "write"}
    body = yaml.safe_dump(rollback)
    names = [step.get("name", step.get("uses", "")) for step in rollback["steps"]]
    assert names.index("Validate linked build, successful Runtime Verify and six-role manifest") < names.index("Verify target digests, platform, revision and provenance")
    assert names.index("Verify target digests, platform, revision and provenance") < names.index("Verify live DB compatibility and retained target containers before mutation")
    assert names.index("Verify live DB compatibility and retained target containers before mutation") < names.index("Create rollback Deployment only after all target evidence passes")
    assert names.index("Create rollback Deployment only after all target evidence passes") < names.index("Roll back consumers and then APIs with stock Kamal")
    assert names.index("Roll back consumers and then APIs with stock Kamal") < names.index("Verify rollback Runtime and preserve databases before SUCCESS")
    for gate in (
        "rollback_target_sha", "rollback_deployment_id", "verify_native_rollback_evidence.py",
        "verify_native_image_readback.py", "verify_native_image_attestation.py",
        "verify-kamal-migration-parity.py", "BASELINE_ROLE_VERSION=$live_version",
        "BASELINE_ROLE_SOURCE_SHA=$live_source_sha", "KAMAL_ROLES=worker", "KAMAL_ROLES=bot",
        "KAMAL_ROLES=memory-worker", "kamal app stop --roles",
        "RECOVERY_REQUIRED", "state=success",
    ):
        assert gate in body
    events = workflow.get("on", workflow.get(True))
    inputs = events["workflow_dispatch"]["inputs"]
    assert "rollback" in inputs and inputs["rollback"]["default"] is False
    assert "rollback_target_sha" in inputs
    assert "rollback_deployment_id" in inputs
    assert "outputs.deploy == 'true'" in workflow["jobs"]["deploy"]["if"]
