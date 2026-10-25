from pathlib import Path
import os
import hashlib
import subprocess
import tempfile
import textwrap

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_kamal_core_config_preserves_caddy_entry_and_only_core_roles():
    config = yaml.safe_load((ROOT / "config/deploy.yml").read_text())
    assert not (ROOT / "Gemfile.kamal").exists()
    assert not (ROOT / "Gemfile.kamal.lock").exists()
    assert config["service"] == "gpoint-core"
    assert config["image"] == "akhripunov/gpoint-runtime"
    assert config["registry"]["server"] == "ghcr.io"
    assert config["proxy"]["run"]["version"] == "v0.9.2"
    assert set(config["servers"]) == {"api", "worker", "bot"}
    api = config["servers"]["api"]
    assert api["proxy"]["host"] == "gpoint.amiora.one"
    assert api["proxy"]["path_prefixes"] == ["/api"]
    assert api["options"]["volume"] == "/opt/gpoint/media:/data/media"
    assert config["proxy"]["run"]["http_port"] == 3001
    assert config["proxy"]["run"]["bind_ips"] == ["127.0.0.1"]
    assert config["ssh"]["keys"] == ["<%= ENV.fetch(\"GPOINT_SSH_KEY\") %>"]
    assert config["ssh"]["config"] is True
    assert config["ssh"]["keys_only"] is True
    assert "gsales" not in config["servers"]
    assert "eros" not in config["servers"]


def test_kamal_agata_and_miniapp_are_separate_services():
    agata = yaml.safe_load((ROOT / "config/deploy.agata.yml").read_text())
    miniapp = yaml.safe_load((ROOT / "config/deploy.miniapp.yml").read_text())
    assert agata["service"] == "gpoint-agata"
    for config in (agata, miniapp):
        assert config["ssh"]["keys"] == ["<%= ENV.fetch(\"GPOINT_SSH_KEY\") %>"]
        assert config["ssh"]["config"] is True
        assert config["ssh"]["keys_only"] is True
    assert agata["image"] == "akhripunov/gpoint-agata-runtime"
    assert set(agata["servers"]) == {"api", "memory-worker"}
    assert miniapp["service"] == "gpoint-miniapp"
    assert miniapp["image"] == "akhripunov/gpoint-miniapp"
    core = yaml.safe_load((ROOT / "config/deploy.yml").read_text())
    assert miniapp["proxy"]["run"] == core["proxy"]["run"]
    assert set(miniapp["servers"]) == {"web"}
    assert miniapp["servers"]["web"]["proxy"]["host"] == "gpoint.amiora.one"
    dockerfile = (ROOT / "infra/production/Gpoint.Dockerfile").read_text()
    assert "FROM nginx:1.27-alpine AS miniapp-runtime" in dockerfile
    assert "COPY --from=miniapp-build /workspace/apps/miniapp/dist/ /usr/share/nginx/html/" in dockerfile
    assert "COPY --from=miniapp-build /workspace/apps/miniapp/dist/ /usr/share/nginx/html/" in dockerfile
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    assert "id: miniapp" in workflow and "target: miniapp-runtime" in workflow
    assert "ghcr.io/akhripunov/gpoint-miniapp:${{ github.sha }}" in workflow
    assert "MINIAPP_DIGEST: ${{ steps.miniapp.outputs.digest }}" in workflow
    assert "APP_DIGEST: ${{ steps.app.outputs.digest }}" in workflow
    assert "AGATA_DIGEST: ${{ steps.agata.outputs.digest }}" in workflow


def test_native_first_ci_build_is_preserved_and_kamal_deploy_is_downstream():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    assert "  workspace:" in workflow
    assert "  python:" in workflow
    assert "  build:" in workflow
    assert "needs: [workspace, python]" in workflow
    assert "cache-to: type=gha,scope=gpoint-runtime,mode=max" in workflow
    assert "sbom: true" in workflow
    assert "provenance: mode=max" in workflow
    assert "  deploy:" in workflow
    assert "needs: build" in workflow
    assert "environment: production" in workflow


def test_kamal_uses_attempt_scoped_tag_with_buildkit_digest_verification():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    version = "${{ inputs.candidate_sha }}-${{ github.run_id }}-${{ github.run_attempt }}"
    assert f"IMAGE_VERSION: {version}" in deploy
    assert "ghcr.io/akhripunov/gpoint-runtime:${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "name: Verify GHCR candidate tags resolve to the exact digests" in deploy
    assert 'docker pull --platform linux/amd64 "$repository:$IMAGE_VERSION"' in deploy
    assert 'test "$actual" = "$repository@$digest"' in deploy
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$APP_DIGEST"' in deploy
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$AGATA_DIGEST"' in deploy
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$MINIAPP_DIGEST"' in deploy
    hook = (ROOT / ".kamal/hooks/pre-app-boot").read_text()
    assert '"$version" == "$candidate-"*' in hook
    assert 'EXPECTED_REF="$repository@$expected_digest"' in hook


def test_kamal_deploy_uses_native_verified_images_and_preserves_first_cutover_guard():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    kamal = workflow.split("  deploy:\n", 1)[1]
    assert "runs-on: ubuntu-latest" in kamal
    assert "environment: production" in kamal
    assert "kamal deploy --skip-push" in kamal
    assert "name: Deploy request-serving Kamal roles" in kamal
    assert "name: Start Kamal consumers after request-serving verification" in kamal
    deploy = kamal.split("name: Deploy request-serving Kamal roles", 1)[1].split(
        "name: Apply candidate Caddyfile", 1
    )[0]
    assert "--roles api --config-file config/deploy.yml" in deploy
    assert "--roles api --config-file config/deploy.agata.yml" in deploy
    assert "--roles web --config-file config/deploy.miniapp.yml" in deploy
    assert "--roles worker,bot" not in deploy
    assert "--roles memory-worker" not in deploy
    consumers = kamal.split("name: Start Kamal consumers after request-serving verification", 1)[1].split(
        "name: Verify public routes", 1
    )[0]
    assert "--roles worker,bot --config-file config/deploy.yml" in consumers
    assert "--roles memory-worker --config-file config/deploy.agata.yml" in consumers
    assert "GPOINT_KAMAL_SSH_PRIVATE_KEY" in kamal
    assert "GPOINT_KAMAL_KNOWN_HOSTS" in kamal
    assert "gsales-api" in kamal
    assert "docker inspect" in kamal
    assert "apps/worker/dist/main.js" in kamal
    assert "apps/bot/dist/main.js" in kamal
    assert "/health/ready" in kamal
    assert "python -m app.memory.worker --healthcheck" in kamal
    assert "wget --spider --quiet http://127.0.0.1/" in kamal
    assert "deployments: write" in workflow
    assert '"ghcr.io/akhripunov/gpoint-runtime|$APP_DIGEST"' in kamal
    assert '"ghcr.io/akhripunov/gpoint-agata-runtime|$AGATA_DIGEST"' in kamal
    assert '"ghcr.io/akhripunov/gpoint-miniapp|$MINIAPP_DIGEST"' in kamal
    assert 'docker image inspect "$image_id"' in kamal
    assert '-f state="$state"' in kamal
    assert "GPOINT_KAMAL_VERIFY=PASS" in kamal
    assert "ghcr.io/basecamp/kamal:v2.12.0@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625" in kamal
    assert "gem install kamal" not in kamal
    assert kamal.index("name: Pause legacy GPoint services after restore gates") < kamal.index(
        "name: Deploy request-serving Kamal roles"
    )
    cutover_helper = (ROOT / "scripts/pause-legacy-gpoint.sh").read_text()
    assert "docker stop --time 120" in cutover_helper
    assert "INVENTORY_SHA256" in cutover_helper
    assert "restore_legacy_services" in cutover_helper
    assert "gsales-api" in cutover_helper
    assert 'index .NetworkSettings.Networks "gpoint_app").Aliases' in kamal
    assert 'index .NetworkSettings.Networks "shared_internal").Aliases' in kamal


def test_caddy_keeps_tls_and_routes_api_and_miniapp_through_one_kamal_proxy():
    caddy = (ROOT / "infra/production/Caddyfile").read_text()
    assert "uri strip_prefix /api" not in caddy
    assert "reverse_proxy 127.0.0.1:3001" in caddy
    assert "reverse_proxy 127.0.0.1:3003" not in caddy
    assert "rewrite * /api{uri}" in caddy
    assert caddy.count("reverse_proxy 127.0.0.1:3001") == 3
    assert "root * /opt/gpoint/app/miniapp" not in caddy
    assert "compose-gsales-api-1" not in caddy


def test_kamal_network_hook_joins_only_allowed_core_and_agata_roles():
    hook = (ROOT / ".kamal/hooks/post-app-boot").read_text()
    assert 'candidate="${GPOINT_COMMIT_SHA:?GPOINT_COMMIT_SHA is required}"' in hook
    assert '[[ "$version" == "$candidate-"* && "$version" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]' in hook
    assert '"$version" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$' in hook
    assert '"$version" =~ ^[0-9a-f]{40}$' not in hook
    assert '[[ "$candidate" =~ ^[0-9a-f]{40}$ ]]' in hook
    assert "gpoint-core:api) networks='gpoint_app:api,shared_internal:'" in hook
    assert "gpoint-agata:api) networks='gpoint_app:agata-api,shared_internal:agata-api'" in hook
    assert "docker network connect --alias \"$alias\" \"$network\" \"$id\"" in hook
    assert "StrictHostKeyChecking=yes" in hook
    assert "gsales" not in hook.lower()
    assert "gpoint-miniapp)" in hook and "exit 0" in hook
    assert 'if [[ "$service:$role" == "gpoint-agata:memory-worker" ]]' in hook
    assert "python -m app.memory.worker --healthcheck" in hook


def test_ssh_heredoc_commands_keep_stdin_available():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    hook = (ROOT / ".kamal/hooks/post-app-boot").read_text()
    assert 'ssh -n -T root@${GPOINT_PRODUCTION_HOST} bash -s <<\'REMOTE\'' not in workflow
    assert 'ssh -n -T root@${GPOINT_PRODUCTION_HOST} bash -s --' not in workflow
    assert 'ssh -n -T -i "$ssh_key"' not in hook


def test_production_host_comes_from_exact_environment_and_known_hosts():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    configs = "\n".join((ROOT / f"config/{name}").read_text()
                         for name in ("deploy.yml", "deploy.agata.yml", "deploy.miniapp.yml"))
    assert "38.180.110.92" not in workflow + configs
    assert "GPOINT_PRODUCTION_HOST: ${{ secrets.GPOINT_KAMAL_HOST }}" in workflow
    assert 'ssh-keygen -F "$GPOINT_PRODUCTION_HOST" -f "$HOME/.ssh/known_hosts"' in workflow
    assert 'ENV.fetch("GPOINT_PRODUCTION_HOST")' in configs
    assert "--env GPOINT_PRODUCTION_HOST" in workflow


def test_kamal_pre_app_boot_checks_the_pulled_digest_before_boot():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    hook = (ROOT / ".kamal/hooks/pre-app-boot").read_text()
    assert 'expected_digest="${GPOINT_KAMAL_EXPECTED_DIGEST:?GPOINT_KAMAL_EXPECTED_DIGEST is required}"' in hook
    assert 'docker image inspect --format' in hook and '"$image"' in hook
    assert 'org.opencontainers.image.revision' in hook
    assert 'test "$platform" = linux/amd64' in hook
    assert "--env GPOINT_KAMAL_EXPECTED_DIGEST" in workflow
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$APP_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-core' in workflow
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$AGATA_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-agata' in workflow
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$MINIAPP_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-miniapp' in workflow


def test_kamal_workflow_applies_candidate_caddyfile_before_production_verify():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    apply_script = (ROOT / "scripts/apply-kamal-caddy.sh").read_text()
    assert "name: Apply candidate Caddyfile after request-serving roles are deployed" in deploy
    assert "if: inputs.mode == 'production'" in deploy
    assert "scp" in deploy
    assert "scripts/apply-kamal-caddy.sh" in deploy
    assert "/etc/caddy/Caddyfile" in apply_script
    assert "/opt/gpoint/backups" in apply_script
    assert "caddy validate" in apply_script
    assert "systemctl reload caddy" in apply_script
    assert "sha256sum" in apply_script
    assert deploy.index("name: Deploy request-serving Kamal roles") < deploy.index(
        "name: Apply candidate Caddyfile after request-serving roles are deployed"
    ) < deploy.index("name: Verify request-serving routes before starting consumers")
    assert deploy.index("name: Verify request-serving routes before starting consumers") < deploy.index(
        "name: Start Kamal consumers after request-serving verification"
    ) < deploy.index("name: Verify public routes and exact running services")
    consumer_step = deploy.split("name: Start Kamal consumers after request-serving verification", 1)[1].split(
        "name: Verify public routes and exact running services", 1
    )[0]
    assert "if: always()" not in consumer_step


def test_kamal_success_requires_database_media_consumer_and_eros_readback():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    assert "name: Capture PostgreSQL and Eros cutover baseline" in deploy
    assert "sanitize-kamal-inventory.py" in deploy
    assert "sanitize-kamal-resources.py" in deploy
    assert "docker stats --no-stream --format '{{json .}}'" in deploy
    assert "POSTGRES_SYSTEM_IDENTIFIER" in deploy
    assert "POSTGRES_ACL_FINGERPRINT" in deploy
    assert "EROS_CONTAINER_ID" in deploy
    assert "DOCKER_FREE_DISK_KB" in deploy
    assert "MEMORY_AVAILABLE_KB" in deploy and "MEMORY_TOTAL_KB" in deploy
    assert "CONTAINER_MEMORY_USED_BYTES" in deploy and "CONTAINER_CPU_PERCENT_SUM" in deploy
    verify = deploy.split("name: Verify public routes and exact running services", 1)[1].split(
        "name: Record GitHub Deployment result", 1
    )[0]
    assert "prisma migrate status" in verify
    assert "alembic heads" in verify
    assert "SELECT version_num FROM agata.alembic_version" in verify
    assert "GPOINT_MEDIA_READ_WRITE=PASS" in verify
    assert "GPOINT_DUPLICATE_CONSUMER_CHECK=PASS" in verify
    assert "POSTGRES_ACL_FINGERPRINT" in verify
    assert "EROS_CONTAINER_ID" in verify
    assert "gsales-api" in verify and "State.Running" in verify
    assert "resource sample does not match the fresh running-container inventory" in verify


def test_first_cutover_stops_only_freshly_inventoried_legacy_services_after_restore():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    assert "name: Verify fresh PostgreSQL backup with isolated restore" in deploy
    assert deploy.index("name: Capture PostgreSQL and Eros cutover baseline") < deploy.index(
        "name: Verify fresh PostgreSQL backup with isolated restore"
    ) < deploy.index("name: Pause legacy GPoint services after restore gates")
    backup_step = deploy.split("name: Verify fresh PostgreSQL backup with isolated restore", 1)[1].split(
        "name: Pause legacy GPoint services", 1
    )[0]
    assert "scripts/verify-gpoint-backup-restore.sh" in backup_step
    assert "backup_sha256" in backup_step
    assert "GPOINT_ISOLATED_RESTORE=PASS" in backup_step
    verify = deploy.split("name: Verify public routes and exact running services", 1)[1].split(
        "name: Record GitHub Deployment result", 1
    )[0]
    assert "BACKUP_PATH: ${{ steps.backuprestore.outputs.backup_path }}" in verify
    assert "BACKUP_SHA: ${{ steps.backuprestore.outputs.backup_sha256 }}" in verify
    assert "ISOLATED_RESTORE=PASS" in verify
    helper = (ROOT / "scripts/verify-gpoint-backup-restore.sh").read_text()
    assert "--network none" in helper
    assert "pg_restore --exit-on-error" in helper
    assert "pg_dumpall --roles-only --no-role-passwords" in helper
    assert "POSTGRES_SYSTEM_IDENTIFIER" in helper
    assert "pre_backup_capacity_check" in helper
    assert helper.index("pre_backup_capacity_check") < helper.index('python3 "$backup_helper" backup')
    assert "pre_restore_capacity_check" in helper
    assert helper.index("pre_restore_capacity_check") < helper.index('docker run -d --name "$restore_name"')
    assert "BACKUP_SHA256" in helper
    assert 'test "$restored_tables" = "$source_tables"' in helper
    assert 'docker rm -f "$restore_container_id"' in helper
    assert 'restore_container_id="$(docker run -d' in helper
    assert "/run/gpoint-kamal/" in helper
    assert "GPOINT_ISOLATED_RESTORE=PASS" in helper
    cutover_step = deploy.split("name: Pause legacy GPoint services after restore gates", 1)[1].split(
        "name: Deploy request-serving Kamal roles", 1
    )[0]
    assert "BASELINE_PATH: ${{ steps.baseline.outputs.baseline_path }}" in cutover_step
    assert "BASELINE_SHA: ${{ steps.baseline.outputs.baseline_sha }}" in cutover_step
    assert "scripts/pause-legacy-gpoint.sh" in cutover_step
    subprocess.run(["bash", "-n", str(ROOT / "scripts/verify-gpoint-backup-restore.sh")], check=True)


def test_cutover_helper_uses_snapshot_bound_graceful_stop_and_pre_deploy_recovery():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    assert deploy.index("name: Verify fresh PostgreSQL backup with isolated restore") < deploy.index(
        "name: Create exact-SHA in-progress GitHub Deployment"
    ) < deploy.index("name: Pause legacy GPoint services after restore gates") < deploy.index(
        "name: Deploy request-serving Kamal roles"
    )
    cutover = deploy.split("name: Pause legacy GPoint services after restore gates", 1)[1].split(
        "name: Deploy request-serving Kamal roles", 1
    )[0]
    assert "scripts/pause-legacy-gpoint.sh" in cutover
    assert 'test "$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/main" --jq .object.sha)" = "$CANDIDATE_SHA"' in cutover
    assert cutover.index("git/ref/heads/main") < cutover.index("scp \"${ssh_opts[@]}\" scripts/pause-legacy-gpoint.sh")
    assert cutover.index('install -d -m 700 "$1"') < cutover.index(
        "scp \"${ssh_opts[@]}\" scripts/pause-legacy-gpoint.sh"
    ) < cutover.index("ssh -T \"${ssh_opts[@]}\" \"root@${GPOINT_PRODUCTION_HOST}\" bash \"$remote_script\"")
    helper = (ROOT / "scripts/pause-legacy-gpoint.sh").read_text()
    assert "INVENTORY_SHA256" in helper
    assert "INVENTORY_FILE" in helper
    assert "docker stop --time 120" in helper
    assert 'service_rows_output="$(python3 - "$inventory_file"' in helper
    assert 'mapfile -t service_rows <<< "$service_rows_output"' in helper
    assert 'mapfile -t service_rows < <(python3' not in helper
    assert "compose: {project}" not in helper
    assert "restore_legacy_services" in helper
    assert "stop_order=(worker bot agata-memory-worker api agata-api)" in helper
    assert "restart_order=(agata-api api agata-memory-worker bot worker)" in helper
    assert 'test "$live_running" = "$was_running"' in helper
    assert 'stopped_by_service[$service]="$id"' in helper
    assert "shared_postgres_1" in helper
    assert "compose-eros-1" in helper
    assert "gsales-api" in helper


def test_cutover_backup_is_encrypted_offsite_and_restore_reads_ciphertext_only():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    backup_step = deploy.split("name: Verify fresh PostgreSQL backup with isolated restore", 1)[1].split(
        "name: Create exact-SHA in-progress GitHub Deployment", 1
    )[0]
    assert "GPOINT_KAMAL_BACKUP_AGE_RECIPIENT" in backup_step
    assert "GPOINT_KAMAL_BACKUP_OFFSITE_REMOTE" in backup_step
    helper = (ROOT / "scripts/verify-gpoint-backup-restore.sh").read_text()
    assert 'test "$(stat -f -c %T /run)" = tmpfs' in helper
    assert 'age-keygen -y -i "$age_identity"' in helper
    assert 'age --recipient "$age_recipient"' in helper
    assert "rclone copyto --immutable" in helper
    assert "rclone check --download" in helper
    assert 'age -d -i "$age_identity" "$offsite_copy"' in helper
    assert ".dump.age" in helper
    assert 'pg_restore --exit-on-error --no-owner -U gpoint_restore_admin -d gpoint \\\n  < "$backup_file"' not in helper


def test_cutover_backup_cli_can_write_plaintext_into_explicit_tmpfs_directory():
    result = subprocess.run(
        ["python3", str(ROOT / "scripts/agata-production-migration.py"), "backup", "--sha", "a" * 40, "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--backup-directory" in result.stdout


def test_kamal_cutover_requires_candidate_and_live_migration_heads_to_match(tmp_path):
    prisma_dir = tmp_path / "prisma"
    prisma_dir.mkdir()
    (prisma_dir / "20261015130000_codex_telegram_challenge").mkdir()
    (prisma_dir / "20261015130000_codex_telegram_challenge" / "migration.sql").write_text("SELECT 1;\n")
    agata_dir = tmp_path / "agata"
    agata_dir.mkdir()
    (agata_dir / "0010.py").write_text("revision = 'agata0010'\ndown_revision = None\n")
    (agata_dir / "0011.py").write_text("revision = 'agata0011'\ndown_revision = 'agata0010'\n")
    result = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--prisma-directory", str(prisma_dir), "--prisma-current-head", "20261015130000_codex_telegram_challenge",
         "--prisma-current-migrations", "20261015130000_codex_telegram_challenge",
         "--agata-directory", str(agata_dir), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "GPOINT_KAMAL_MIGRATION_PARITY=PASS" in result.stdout
    assert "GPOINT_KAMAL_DB_MIGRATION_REQUIRED=NO" in result.stdout


def test_kamal_migration_parity_fails_closed_for_pending_candidate_revision(tmp_path):
    prisma_dir = tmp_path / "prisma"
    prisma_dir.mkdir()
    (prisma_dir / "20261015130000_codex_telegram_challenge").mkdir()
    (prisma_dir / "20261015130000_codex_telegram_challenge" / "migration.sql").write_text("SELECT 1;\n")
    (prisma_dir / "20261022110000_eros_gpoint_message_state").mkdir()
    (prisma_dir / "20261022110000_eros_gpoint_message_state" / "migration.sql").write_text("SELECT 1;\n")
    agata_dir = tmp_path / "agata"
    agata_dir.mkdir()
    (agata_dir / "0010.py").write_text("revision = 'agata0010'\ndown_revision = None\n")
    (agata_dir / "0011.py").write_text("revision = 'agata0011'\ndown_revision = 'agata0010'\n")
    (agata_dir / "0012.py").write_text("revision = 'agata0012'\ndown_revision = 'agata0011'\n")
    result = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--prisma-directory", str(prisma_dir), "--prisma-current-head", "20261015130000_codex_telegram_challenge",
         "--prisma-current-migrations", "20261015130000_codex_telegram_challenge",
         "--agata-directory", str(agata_dir), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "PENDING" in result.stderr


def test_native_workflow_runs_migration_parity_before_deployment_or_pause():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    parity = deploy.index("name: Verify candidate migrations match the live database")
    deployment = deploy.index("name: Create exact-SHA in-progress GitHub Deployment")
    pause = deploy.index("name: Pause legacy GPoint services")
    assert parity < deployment < pause
    assert "scripts/verify-kamal-migration-parity.py" in deploy
    assert "PRISMA_MIGRATIONS=" in deploy
    assert "prisma_migrations|agata_head" in deploy


def test_kamal_cutover_checks_candidate_image_disk_headroom_before_deployment():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    check = deploy.index("name: Check VPS disk headroom for the exact candidate images")
    deployment = deploy.index("name: Create exact-SHA in-progress GitHub Deployment")
    pause = deploy.index("name: Pause legacy GPoint services after restore gates")
    assert check < deployment < pause
    assert "candidate_image_bytes" in deploy
    assert "1024 * 1024 * 1024" in deploy
    assert "KAMAL_CUTOVER_DISK_HEADROOM=PASS" in deploy


def test_kamal_production_verify_persists_attempt_bound_manifest():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    verify = deploy.split("name: Verify public routes and exact running services", 1)[1].split(
        "name: Record GitHub Deployment result against the same attempt", 1
    )[0]
    manifest_writer = (ROOT / "scripts/write-kamal-production-manifest.py").read_text()
    assert "scripts/write-kamal-production-manifest.py" in verify
    assert "scp scripts/write-kamal-production-manifest.py" in verify
    assert 'trap \'rm -f -- "$manifest_writer"\' EXIT' in verify
    assert "gpoint-kamal-production-manifest/v1" in manifest_writer
    assert "kamal-production-${run_id}-${attempt}.json" in verify
    assert "CANDIDATE_SHA" in verify and "APP_DIGEST" in verify
    assert "INVENTORY_SHA256" in verify and "BACKUP_SHA256" in verify
    assert "ISOLATED_RESTORE=PASS" in verify
    assert "GPOINT_KAMAL_MANIFEST_SHA256=" in verify
    assert '"runtimeServices"' in manifest_writer
    assert '"containerId"' in manifest_writer and '"repositoryDigest"' in manifest_writer


def test_manifest_writer_behavior_tests_run_in_python_ci():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    python_job = workflow.split("  python:\n", 1)[1].split("  build:\n", 1)[0]
    assert "tests/test_kamal_production_manifest.py" in python_job


def test_kamal_inventory_sanitizer_keeps_runtime_facts_and_drops_environment_and_commands(tmp_path):
    import json

    inspect = [{
        "Id": "a" * 64,
        "Name": "/compose-api-1",
        "Created": "2026-10-25T00:00:00Z",
        "Config": {
            "Image": "gpoint-runtime:oldsha",
            "Env": ["DATABASE_URL=postgres://private", "TOKEN=private"],
            "Cmd": ["node", "--token", "private"],
            "Labels": {"com.docker.compose.project": "compose", "com.docker.compose.service": "api"},
        },
        "Image": "sha256:" + "b" * 64,
        "State": {"Status": "running", "Running": True, "Health": {"Status": "healthy"}},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "NetworkSettings": {
            "Ports": {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3000"}]},
            "Networks": {"gpoint_app": {"Aliases": ["api"], "IPAddress": "172.20.0.2"}},
        },
        "Mounts": [{"Type": "bind", "Source": "/opt/gpoint/media", "Destination": "/data/media", "RW": True}],
    }]
    resources = tmp_path / "resources.json"
    resources.write_text(json.dumps([{
        "containerId": "a" * 12, "containerName": "compose-api-1", "cpuPercent": 2.5,
        "memoryUsedBytes": 268435456, "memoryLimitBytes": 8589934592,
        "memoryPercent": 3.12, "pids": 14,
    }]))
    result = subprocess.run(
        ["python3", str(ROOT / "scripts/sanitize-kamal-inventory.py"), "--resource-facts", str(resources)],
        input=__import__("json").dumps(inspect), text=True, capture_output=True, check=True,
    )
    inventory = __import__("json").loads(result.stdout)
    serialized = result.stdout
    assert inventory["schema"] == "gpoint-vps-inventory/v1"
    container = inventory["containers"][0]
    assert container["name"] == "compose-api-1"
    assert container["composeProject"] == "compose"
    assert container["composeService"] == "api"
    assert container["state"] == "running" and container["health"] == "healthy"
    assert container["networks"]["gpoint_app"]["aliases"] == ["api"]
    assert container["mounts"][0]["destination"] == "/data/media"
    assert inventory["resourceUsage"][0]["containerName"] == "compose-api-1"
    assert "DATABASE_URL" not in serialized and "postgres://private" not in serialized
    assert "TOKEN=private" not in serialized and "--token" not in serialized
    resources.write_text("[]")
    incomplete = subprocess.run(
        ["python3", str(ROOT / "scripts/sanitize-kamal-inventory.py"), "--resource-facts", str(resources)],
        input=json.dumps(inspect), text=True, capture_output=True,
    )
    assert incomplete.returncode != 0
    assert "resource sample is empty" in incomplete.stderr


def test_kamal_inventory_includes_sanitized_per_container_resource_sample(tmp_path):
    import json

    resource_script = ROOT / "scripts/sanitize-kamal-resources.py"
    stats = [{
        "Container": "compose-api-1", "ID": "b" * 12, "Name": "compose-api-1",
        "CPUPerc": "2.50%", "MemUsage": "256MiB / 8GiB", "MemPerc": "3.12%", "PIDs": "14",
        "BlockIO": "private", "NetIO": "private",
    }]
    raw_stats = tmp_path / "docker-stats.jsonl"
    raw_stats.write_text("\n".join(json.dumps(row) for row in stats) + "\n")
    result = subprocess.run(
        ["python3", str(resource_script)], input=raw_stats.read_text(), text=True,
        capture_output=True, check=True,
    )
    resources = json.loads(result.stdout)
    assert resources[0] == {
        "containerId": "b" * 12, "containerName": "compose-api-1", "cpuPercent": 2.5,
        "memoryUsedBytes": 256 * 1024**2, "memoryLimitBytes": 8 * 1024**3,
        "memoryPercent": 3.12, "pids": 14,
    }
    assert "private" not in result.stdout


def test_caddy_apply_script_backs_up_and_reloads_only_a_verified_candidate():
    candidate_sha = "a" * 40
    candidate = "gpoint.amiora.one {\n  respond /health 200\n}\n"
    previous = "gpoint.amiora.one {\n  respond /old 200\n}\n"
    candidate_digest = hashlib.sha256(candidate.encode()).hexdigest()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        current = root / "etc/caddy/Caddyfile"
        current.parent.mkdir(parents=True)
        current.write_text(previous)
        staged = Path("/tmp") / f"gpoint-caddy-{candidate_sha}-12345-2"
        staged.unlink(missing_ok=True)
        staged.write_text(candidate)
        call_log = root / "calls"
        (bin_dir / "caddy").write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import os, sys
            with open(os.environ['TEST_CALL_LOG'], 'a') as log:
                log.write('caddy ' + ' '.join(sys.argv[1:]) + '\\n')
            raise SystemExit(int(os.environ.get('TEST_CADDY_EXIT', '0')))
        """))
        (bin_dir / "systemctl").write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import os, sys
            with open(os.environ['TEST_CALL_LOG'], 'a') as log:
                log.write('systemctl ' + ' '.join(sys.argv[1:]) + '\\n')
            if sys.argv[1:] == ['reload', 'caddy'] and os.environ.get('TEST_FAIL_FIRST_RELOAD') == '1':
                marker = os.environ['TEST_RELOAD_MARKER']
                if not os.path.exists(marker):
                    open(marker, 'w').close()
                    raise SystemExit(1)
            raise SystemExit(int(os.environ.get('TEST_SYSTEMCTL_EXIT', '0')))
        """))
        for executable in bin_dir.iterdir():
            executable.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "GPOINT_CADDY_CONFIG_PATH": str(current),
            "GPOINT_CADDY_BACKUP_DIR": str(root / "backups"),
            "TEST_CALL_LOG": str(call_log),
        })
        command = [
            "bash", str(ROOT / "scripts/apply-kamal-caddy.sh"), candidate_sha,
            candidate_digest, "12345", "2", str(staged),
        ]
        result = subprocess.run(command, env=environment, capture_output=True, text=True)

        assert result.returncode == 0, result.stderr
        assert current.read_text() == candidate
        backup = root / "backups" / f"caddy-before-{candidate_sha}-12345-2"
        assert backup.read_text() == previous
        sidecar = backup.parent / f"{backup.name}.sha256"
        assert sidecar.read_text().split()[0] == hashlib.sha256(previous.encode()).hexdigest()
        assert sidecar.read_text().split()[1] == str(backup)
        calls = call_log.read_text().splitlines()
        assert len([line for line in calls if line.startswith("caddy validate")]) == 2
        assert calls.index("systemctl reload caddy") < calls.index("systemctl is-active --quiet caddy")

        staged.write_text(candidate)
        mismatch = subprocess.run(
            [*command[:3], "0" * 64, *command[4:]], env=environment, capture_output=True, text=True
        )
        assert mismatch.returncode != 0
        assert current.read_text() == candidate
        staged.unlink(missing_ok=True)

        validation_current = root / "validation/etc/caddy/Caddyfile"
        validation_current.parent.mkdir(parents=True)
        validation_current.write_text(previous)
        validation_staged = Path("/tmp") / f"gpoint-caddy-{candidate_sha}-12346-1"
        validation_staged.write_text(candidate)
        validation_env = environment | {
            "GPOINT_CADDY_CONFIG_PATH": str(validation_current),
            "GPOINT_CADDY_BACKUP_DIR": str(root / "validation/backups"),
            "TEST_CADDY_EXIT": "1",
        }
        calls_before_validation = len(call_log.read_text().splitlines())
        validation = subprocess.run(
            ["bash", str(ROOT / "scripts/apply-kamal-caddy.sh"), candidate_sha,
             candidate_digest, "12346", "1", str(validation_staged)],
            env=validation_env, capture_output=True, text=True,
        )
        assert validation.returncode != 0
        assert validation_current.read_text() == previous
        assert not (root / "validation/backups").exists()
        assert not any(
            line.startswith("systemctl ")
            for line in call_log.read_text().splitlines()[calls_before_validation:]
        )
        validation_staged.unlink(missing_ok=True)

        reload_current = root / "reload/etc/caddy/Caddyfile"
        reload_current.parent.mkdir(parents=True)
        reload_current.write_text(previous)
        reload_staged = Path("/tmp") / f"gpoint-caddy-{candidate_sha}-12347-1"
        reload_staged.write_text(candidate)
        reload_env = environment | {
            "GPOINT_CADDY_CONFIG_PATH": str(reload_current),
            "GPOINT_CADDY_BACKUP_DIR": str(root / "reload/backups"),
            "TEST_CADDY_EXIT": "0",
            "TEST_FAIL_FIRST_RELOAD": "1",
            "TEST_RELOAD_MARKER": str(root / "reload-first-failure"),
        }
        reload = subprocess.run(
            ["bash", str(ROOT / "scripts/apply-kamal-caddy.sh"), candidate_sha,
             candidate_digest, "12347", "1", str(reload_staged)],
            env=reload_env, capture_output=True, text=True,
        )
        assert reload.returncode != 0
        assert reload_current.read_text() == previous
        reload_calls = call_log.read_text().splitlines()
        assert reload_calls[-3:] == [
            "caddy validate --config " + str(reload_current) + " --adapter caddyfile",
            "systemctl reload caddy",
            "systemctl is-active --quiet caddy",
        ]
        assert "GPOINT_CADDY_ROLLBACK=PASS" in reload.stdout
        reload_staged.unlink(missing_ok=True)


def test_pre_app_boot_reads_the_ssh_heredoc_and_fails_closed_on_digest_mismatch():
    candidate = "a" * 40
    digest = "sha256:" + "b" * 64
    repository = "ghcr.io/akhripunov/gpoint-runtime"

    def run_with_repo_digest(actual_repo_digest):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary_dir = directory / "bin"
            binary_dir.mkdir()
            key = directory / "key"
            known_hosts = directory / "known_hosts"
            docker_log = directory / "docker-calls"
            key.write_text("stub key\n")
            known_hosts.write_text("stub host key\n")
            (binary_dir / "ssh").write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import subprocess, sys
                args = sys.argv[1:]
                host = next(i for i, arg in enumerate(args) if arg.startswith("root@"))
                command = args[host + 1:]
                stdin = subprocess.DEVNULL if "-n" in args else None
                result = subprocess.run(command, stdin=stdin)
                raise SystemExit(result.returncode)
            """))
            (binary_dir / "docker").write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import json, os, sys
                args = sys.argv[1:]
                with open(os.environ["TEST_DOCKER_LOG"], "a", encoding="utf-8") as log:
                    log.write(json.dumps(args) + "\\n")
                if args[:2] != ["image", "inspect"]:
                    raise SystemExit(2)
                template = args[3]
                if template == "{{json .RepoDigests}}":
                    print(json.dumps([os.environ["TEST_REPO_DIGEST"]]))
                elif template == '{{index .Config.Labels "org.opencontainers.image.revision"}}':
                    print(os.environ["GPOINT_COMMIT_SHA"])
                elif template == "{{.Os}}/{{.Architecture}}":
                    print("linux/amd64")
                else:
                    raise SystemExit(2)
            """))
            for executable in (binary_dir / "ssh", binary_dir / "docker"):
                executable.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "PATH": f"{binary_dir}:{environment['PATH']}",
                "GPOINT_KAMAL_SERVICE": "gpoint-core",
                "KAMAL_VERSION": f"{candidate}-12345-1",
                "GPOINT_COMMIT_SHA": candidate,
                "GPOINT_KAMAL_EXPECTED_DIGEST": digest,
                "KAMAL_HOSTS": "127.0.0.1",
                "GPOINT_SSH_KEY": str(key),
                "GPOINT_KNOWN_HOSTS_FILE": str(known_hosts),
                "TEST_REPO_DIGEST": actual_repo_digest,
                "TEST_DOCKER_LOG": str(docker_log),
            })
            result = subprocess.run(
                ["bash", str(ROOT / ".kamal/hooks/pre-app-boot")],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            calls = docker_log.read_text().splitlines() if docker_log.exists() else []
            return result, calls

    matching, calls = run_with_repo_digest(f"{repository}@{digest}")
    assert matching.returncode == 0, f"{matching.stderr}; docker calls: {calls}"
    assert len(calls) == 3

    mismatch, calls = run_with_repo_digest(f"{repository}@sha256:" + "c" * 64)
    assert mismatch.returncode != 0
    assert "does not match the BuildKit-verified OCI digest" in mismatch.stderr
    assert len(calls) == 3
