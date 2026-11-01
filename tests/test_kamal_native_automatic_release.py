from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-native-release.yml"


def test_main_push_runs_exact_candidate_tests_build_kamal_and_verify():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    events = workflow.get("on", workflow.get(True))
    assert events["push"]["branches"] == ["main"]
    deploy = workflow["jobs"]["deploy"]
    assert "build" in deploy["needs"]
    assert "push" in deploy.get("if", "")
    assert "refs/heads/main" in deploy.get("if", "")
    assert "workflow_dispatch" in deploy.get("if", "")
    assert deploy["environment"] == {"name": "production", "deployment": False}


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


def test_native_release_installs_vps_backup_timer_only_after_verify():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy = workflow.split("  deploy:\n", 1)[1]
    verify_step = deploy.index("name: Verify public routes and exact running services")
    install_step = deploy.index("name: Install and enable scheduled VPS backups")
    status_step = deploy.index("name: Record GitHub Deployment result against the same attempt")
    assert verify_step < install_step < status_step
    assert "systemctl enable --now gpoint-backup.timer" in deploy[install_step:status_step]


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
