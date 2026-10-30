from pathlib import Path
import os
import hashlib
import subprocess
import tempfile
import textwrap

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_shared_proxy_hosts_are_not_redeclared_by_kamal_roles():
    core = yaml.safe_load((ROOT / "config/deploy.yml").read_text())
    miniapp = yaml.safe_load((ROOT / "config/deploy.miniapp.yml").read_text())

    assert core["proxy"]["hosts"] == ["gpoint.amiora.one"]
    assert "host" not in core["servers"]["api"]["proxy"]
    assert "hosts" not in core["servers"]["api"]["proxy"]
    assert miniapp["proxy"]["hosts"] == ["gpoint.amiora.one"]
    assert "host" not in miniapp["servers"]["web"]["proxy"]
    assert "hosts" not in miniapp["servers"]["web"]["proxy"]


def test_kamal_core_config_uses_public_tls_proxy_and_only_core_roles():
    config = yaml.safe_load((ROOT / "config/deploy.yml").read_text())
    assert not (ROOT / "Gemfile.kamal").exists()
    assert not (ROOT / "Gemfile.kamal.lock").exists()
    assert config["service"] == "gpoint-core"
    assert config["image"] == "akhripunov/gpoint-runtime"
    assert config["registry"]["server"] == "ghcr.io"
    assert config["proxy"]["run"]["version"] == "v0.9.2"
    assert set(config["servers"]) == {"api", "worker", "bot"}
    api = config["servers"]["api"]
    assert "host" not in api["proxy"]
    assert api["proxy"]["path_prefixes"] == ["/api"]
    assert api["options"]["volume"] == "/opt/gpoint/media:/data/media"
    assert config["proxy"]["hosts"] == ["gpoint.amiora.one"]
    assert config["proxy"]["ssl"] is True
    assert config["proxy"]["ssl_redirect"] is True
    assert set(config["proxy"]["run"]) == {"version"}
    assert api["proxy"]["path_prefixes"] == ["/api"]
    assert api["proxy"]["strip_path_prefix"] is True
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
    assert miniapp["proxy"]["hosts"] == ["gpoint.amiora.one"]
    assert miniapp["proxy"]["ssl"] is True
    assert miniapp["proxy"]["ssl_redirect"] is True
    assert miniapp["proxy"]["run"] == core["proxy"]["run"]
    assert set(miniapp["servers"]) == {"web"}
    assert "host" not in miniapp["servers"]["web"]["proxy"]
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
    assert "name: Verify GHCR candidate tags resolve to exact amd64 digests" in deploy
    assert 'docker pull --platform linux/amd64 "$repository:$IMAGE_VERSION"' in deploy
    assert 'test "$actual" = "$repository@$digest"' in deploy
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$APP_DIGEST"' in deploy
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$AGATA_DIGEST"' in deploy
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$MINIAPP_DIGEST"' in deploy
    hook = (ROOT / ".kamal/hooks/pre-app-boot").read_text()
    assert '"$version" == "$candidate-"*' in hook
    assert 'EXPECTED_REF="$repository@$expected_digest"' in hook


def test_kamal_deploy_uses_native_verified_images_and_preserves_first_cutover_guard():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    kamal = workflow.split("  deploy:\n", 1)[1]
    assert "runs-on: ubuntu-latest" in kamal
    assert "environment: production" in kamal
    assert "kamal deploy --skip-push" in kamal
    assert "name: Deploy request-serving Kamal roles" in kamal
    assert "name: Start Kamal consumers after request-serving verification" in kamal
    deploy = kamal.split("name: Deploy request-serving Kamal roles", 1)[1].split(
        "name: Verify request-serving routes before starting consumers", 1
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


def test_kamal_proxy_owns_public_tls_and_routes_api_media_and_miniapp():
    core = yaml.safe_load((ROOT / "config/deploy.yml").read_text())
    miniapp = yaml.safe_load((ROOT / "config/deploy.miniapp.yml").read_text())
    for config in (core, miniapp):
        assert config["proxy"]["hosts"] == ["gpoint.amiora.one"]
        assert config["proxy"]["ssl"] is True
        assert config["proxy"]["ssl_redirect"] is True
        assert "http_port" not in config["proxy"]["run"]
        assert "https_port" not in config["proxy"]["run"]
        assert "bind_ips" not in config["proxy"]["run"]
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    assert "scripts/retire-gpoint-caddy.sh" in workflow
    assert "KAMAL_PROXY_TLS_VERIFY=PASS" in workflow
    assert "systemctl reload caddy" not in workflow
    media_route = (ROOT / "apps/api/src/media-read.ts").read_text()
    assert "@Controller()" in media_route
    assert "@Get(['media/assets/:id', 'assets/:id'])" in media_route


def test_miniapp_nginx_proxies_media_without_rewriting_uri_or_losing_auth():
    core = yaml.safe_load((ROOT / "config/deploy.yml").read_text())
    assert core["servers"]["api"]["proxy"]["path_prefixes"] == ["/api"]
    assert core["servers"]["api"]["proxy"]["strip_path_prefix"] is True
    nginx = (ROOT / "infra/production/nginx-miniapp.conf").read_text()
    assert "location ^~ /media/" in nginx
    assert "resolver 127.0.0.11 valid=10s ipv6=off;" in nginx
    assert "set $gpoint_api api:3000;" in nginx
    assert "proxy_pass http://$gpoint_api$request_uri;" in nginx
    assert "proxy_set_header Authorization $http_authorization;" in nginx
    assert "proxy_set_header Cookie $http_cookie;" in nginx
    assert "location = /media" in nginx and "return 404;" in nginx
    assert "try_files $uri $uri/ /index.html;" in nginx
    dockerfile = (ROOT / "infra/production/Gpoint.Dockerfile").read_text()
    assert "COPY infra/production/nginx-miniapp.conf /etc/nginx/conf.d/default.conf" in dockerfile


def test_isolated_restore_drops_only_verified_pristine_public_schema_before_pg_restore():
    helper = (ROOT / "scripts/verify-gpoint-backup-restore.sh").read_text()
    start = helper.index('public_schema_state="')
    end = helper.index("age -d -i \"$age_identity\"", start)
    restore_gate = helper[start:end]
    assert 'docker exec -i "$restore_container_id" psql' in restore_gate
    assert "PUBLIC_SCHEMA_PRISTINE" in restore_gate
    assert "PUBLIC_SCHEMA_NOT_EMPTY" in restore_gate
    assert "PUBLIC_SCHEMA_OWNER_CHANGED" in restore_gate
    assert "PUBLIC_SCHEMA_ACL_CHANGED" in restore_gate
    assert "pg_statistic_ext" in restore_gate
    assert "DROP SCHEMA public RESTRICT" in restore_gate
    assert "DROP SCHEMA public CASCADE" not in helper
    assert restore_gate.index("test \"$public_schema_state\" = PUBLIC_SCHEMA_PRISTINE") < restore_gate.index(
        "DROP SCHEMA public RESTRICT"
    )
    assert helper.index("DROP SCHEMA public RESTRICT") < helper.index(
        "pg_restore --exit-on-error --no-owner -U gpoint_restore_admin -d gpoint"
    )
    assert '[[ "$restore_container_id" =~ ^[0-9a-f]{64}$ ]]' in helper
    assert "HostConfig.NetworkMode" in helper
    assert 'docker exec -i "$restore_container_id" pg_restore' in helper
    assert 'pg_restore --exit-on-error --no-owner -U gpoint_restore_admin -d gpoint' in helper[end:]


def test_kamal_network_hook_joins_only_allowed_core_and_agata_roles():
    hook = (ROOT / ".kamal/hooks/post-app-boot").read_text()
    assert 'candidate="${GPOINT_COMMIT_SHA:?GPOINT_COMMIT_SHA is required}"' in hook
    assert 'app_source_sha="${GPOINT_APP_SOURCE_SHA:?GPOINT_APP_SOURCE_SHA is required}"' in hook
    assert '[[ "$version" == "$candidate-"* && "$version" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]' in hook
    assert '"$version" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$' in hook
    assert '"$version" =~ ^[0-9a-f]{40}$' not in hook
    assert '[[ "$candidate" =~ ^[0-9a-f]{40}$ ]]' in hook
    assert 'test "$(docker inspect --format' in hook and '"$app_source_sha"' in hook
    assert "gpoint-core:api) networks='gpoint_app:api,shared_internal:'" in hook
    assert "gpoint-agata:api) networks='gpoint_app:agata-api,shared_internal:agata-api'" in hook
    assert "docker network connect --alias \"$alias\" \"$network\" \"$id\"" in hook
    assert "StrictHostKeyChecking=yes" in hook
    assert "gsales" not in hook.lower()
    assert "gpoint-miniapp:web) networks='gpoint_app:'" in hook
    assert "getent hosts api" in hook
    assert 'api_container="gpoint-core-api-${version}"' in hook
    assert "resolved_ip" in hook and "test \"$resolved_ip\" = \"$api_ip\"" in hook
    assert "nginx -t" in hook
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
    native_workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    assert "GPOINT_APP_SOURCE_SHA: ${{ inputs.candidate_sha }}" in native_workflow
    assert "--env GPOINT_APP_SOURCE_SHA" in native_workflow


def test_kamal_pre_app_boot_checks_the_pulled_digest_before_boot():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    hook = (ROOT / ".kamal/hooks/pre-app-boot").read_text()
    assert 'expected_digest="${GPOINT_KAMAL_EXPECTED_DIGEST:?GPOINT_KAMAL_EXPECTED_DIGEST is required}"' in hook
    assert 'docker image inspect --format' in hook and '"$image"' in hook
    assert 'org.opencontainers.image.revision' in hook
    assert 'app_source_sha="${GPOINT_APP_SOURCE_SHA:?GPOINT_APP_SOURCE_SHA is required}"' in hook
    assert 'test "$source_sha" = "$app_source_sha"' in hook
    assert 'test "$platform" = linux/amd64' in hook
    assert "--env GPOINT_KAMAL_EXPECTED_DIGEST" in workflow
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$APP_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-core' in workflow
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$AGATA_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-agata' in workflow
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$MINIAPP_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-miniapp' in workflow


def test_first_cutover_stops_caddy_before_binding_public_kamal_proxy():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    assert "name: Stop Caddy for Kamal Proxy public bind" in deploy
    assert "scripts/retire-gpoint-caddy.sh" in deploy
    assert "CADDYFILE_SHA256" in deploy
    assert deploy.index("name: Stop Caddy for Kamal Proxy public bind") < deploy.index(
        "name: Deploy request-serving Kamal roles"
    ) < deploy.index("name: Verify request-serving routes before starting consumers")
    assert "systemctl stop caddy" in (ROOT / "scripts/retire-gpoint-caddy.sh").read_text()
    assert "kamal-proxy" in deploy
    assert deploy.index("name: Verify request-serving routes before starting consumers") < deploy.index(
        "name: Start Kamal consumers after request-serving verification"
    ) < deploy.index("name: Verify public routes and exact running services")
    consumer_step = deploy.split("name: Start Kamal consumers after request-serving verification", 1)[1].split(
        "name: Verify public routes and exact running services", 1
    )[0]
    assert "if: always()" not in consumer_step


def test_retire_caddy_script_checksums_recovery_copy_before_stopping_service(tmp_path):
    candidate_sha = "a" * 40
    config = tmp_path / "Caddyfile"
    config.write_text("gpoint.amiora.one { respond /health 200 }\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    stopped = tmp_path / "caddy-stopped"
    (bin_dir / "systemctl").write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import os, sys
        with open(os.environ['TEST_CALLS'], 'a') as output:
            output.write(' '.join(sys.argv[1:]) + '\\n')
        marker = os.environ['TEST_STOPPED']
        if sys.argv[1:] == ['stop', 'caddy']:
            open(marker, 'w').close()
        elif sys.argv[1:] == ['start', 'caddy']:
            if os.path.exists(marker): os.unlink(marker)
        if sys.argv[1:] == ['is-active', '--quiet', 'caddy']:
            raise SystemExit(3 if os.path.exists(marker) else 0)
    """))
    (bin_dir / "systemctl").chmod(0o755)
    environment = os.environ.copy()
    environment.update({
        "PATH": f"{bin_dir}:{environment['PATH']}",
        "GPOINT_CADDY_CONFIG_PATH": str(config),
        "GPOINT_CADDY_BACKUP_DIR": str(tmp_path / "backups"),
        "TEST_CALLS": str(calls),
        "TEST_STOPPED": str(stopped),
    })
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/retire-gpoint-caddy.sh"), candidate_sha, "12345", "2",
         hashlib.sha256(config.read_bytes()).hexdigest()],
        env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    backup = tmp_path / "backups" / f"caddy-before-kamal-{candidate_sha}-12345-2"
    assert backup.read_text() == config.read_text()
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == Path(f"{backup}.sha256").read_text().split()[0]
    assert calls.read_text().splitlines() == ["is-active --quiet caddy", "stop caddy", "is-active --quiet caddy"]
    assert "GPOINT_CADDY_RETIRED=PASS" in result.stdout


def test_native_workflow_quotes_production_host_in_direct_ssh_targets():
    for workflow_name in (
        "gpoint-native-release.yml",
        "gpoint-first-kamal-cutover.yml",
    ):
        workflow = (ROOT / ".github/workflows" / workflow_name).read_text()
        for line in workflow.splitlines():
            if ("root@${GPOINT_PRODUCTION_HOST}" in line
                    and ("ssh " in line or "scp " in line)):
                assert '"root@${GPOINT_PRODUCTION_HOST}' in line, (workflow_name, line)
                assert line.count('"root@${GPOINT_PRODUCTION_HOST}') == 1, (workflow_name, line)


def test_cutover_summary_printf_avoids_shellcheck_single_quote_interpolation_warning():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    assert "printf -- '- %s: %s\\n'" in workflow
    assert "printf -- '- %s: `%s`\\n'" not in workflow


def test_kamal_success_requires_database_media_consumer_and_no_eros_readback():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    assert "name: Capture PostgreSQL and no-Eros cutover baseline" in deploy
    assert "sanitize-kamal-inventory.py" in deploy
    assert "sanitize-kamal-resources.py" in deploy
    assert "docker stats --no-stream --format '{{json .}}'" in deploy
    assert "POSTGRES_SYSTEM_IDENTIFIER" in deploy
    assert "POSTGRES_ACL_FINGERPRINT" in deploy
    assert "NO_EROS_STATE_SHA256" in deploy
    assert "EROS_CONTAINER_ID" not in deploy and "EROS_NETWORK_FINGERPRINT" not in deploy
    assert 'python3 "$eros_collector" --output "$eros_state_file"' in deploy
    assert 'python3 "$eros_state_helper" compare --before "$no_eros_baseline_file" --after "$no_eros_postflight_file"' in deploy
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
    assert "GPOINT_NO_EROS_BASELINE_POSTFLIGHT=PASS" in verify
    assert "EROS_CONTAINER_ID" not in verify and "EROS_NETWORK_FINGERPRINT" not in verify
    assert "NO_EROS_STATE_PATH: ${{ steps.baseline.outputs.no_eros_state_path }}" in verify
    assert "NO_EROS_STATE_SHA: ${{ steps.baseline.outputs.no_eros_state_sha }}" in verify
    assert '"$NO_EROS_STATE_PATH" "$NO_EROS_STATE_SHA"' in verify
    assert "--postgres-acl-fingerprint \"$(baseline_value POSTGRES_ACL_FINGERPRINT)\"" in verify
    assert "noErosBaseline" in (ROOT / "scripts/write-kamal-production-manifest.py").read_text()
    assert "gsales-api" in verify and "State.Running" in verify
    assert "resource sample does not match the fresh running-container inventory" in verify


def test_first_cutover_stops_only_freshly_inventoried_legacy_services_after_restore():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    assert "name: Verify fresh PostgreSQL and media backup with isolated restore" in deploy
    assert deploy.index("name: Capture PostgreSQL and no-Eros cutover baseline") < deploy.index(
        "name: Verify fresh PostgreSQL and media backup with isolated restore"
    ) < deploy.index("name: Pause legacy GPoint services after restore gates")
    backup_step = deploy.split("name: Verify fresh PostgreSQL and media backup with isolated restore", 1)[1].split(
        "name: Pause legacy GPoint services", 1
    )[0]
    assert "scripts/verify-gpoint-backup-restore.sh" in backup_step
    assert "backup_sha256" in backup_step
    assert "GPOINT_ISOLATED_RESTORE=PASS" in backup_step
    assert "GPOINT_MEDIA_BACKUP=PASS" in backup_step
    assert "GPOINT_MEDIA_ISOLATED_RESTORE=PASS" in backup_step
    verify = deploy.split("name: Verify public routes and exact running services", 1)[1].split(
        "name: Record GitHub Deployment result", 1
    )[0]
    assert "BACKUP_PATH: ${{ steps.backuprestore.outputs.backup_path }}" in verify
    assert "BACKUP_SHA: ${{ steps.backuprestore.outputs.backup_sha256 }}" in verify
    assert "ISOLATED_RESTORE=PASS" in verify
    assert "MEDIA_BACKUP_SHA" in verify and "MEDIA_SOURCE_MANIFEST_SHA" in verify
    assert "MEDIA_RESTORE_MANIFEST_SHA" in verify
    assert "MEDIA_ISOLATED_RESTORE=PASS" in verify
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
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    assert deploy.index("name: Verify fresh PostgreSQL and media backup with isolated restore") < deploy.index(
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
    assert "compare --before" in helper
    assert "NO_EROS_STATE_FILE" in helper
    assert "gsales-api" in helper


def test_cutover_backup_is_encrypted_offsite_and_restore_reads_ciphertext_only():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    backup_step = deploy.split("name: Verify fresh PostgreSQL and media backup with isolated restore", 1)[1].split(
        "name: Create exact-SHA in-progress GitHub Deployment", 1
    )[0]
    assert "GPOINT_KAMAL_BACKUP_AGE_RECIPIENT" in backup_step
    assert "GPOINT_KAMAL_BACKUP_OFFSITE_REMOTE" in backup_step
    helper = (ROOT / "scripts/verify-gpoint-backup-restore.sh").read_text()
    assert 'test "$(stat -f -c %T /run)" = tmpfs' in helper
    assert 'age-keygen -y "$age_identity"' in helper
    assert 'age --recipient "$age_recipient"' in helper
    assert "rclone copyto --immutable" in helper
    assert "rclone check --download" in helper
    assert 'age -d -i "$age_identity" "$offsite_copy"' in helper
    assert ".dump.age" in helper
    assert 'pg_restore --exit-on-error --no-owner -U gpoint_restore_admin -d gpoint \\\n  < "$backup_file"' not in helper


def test_legacy_pause_rechecks_media_after_stopping_consumers_and_recovers_on_mismatch():
    pause = (ROOT / "scripts/pause-legacy-gpoint.sh").read_text()
    no_eros_recheck = pause.index('python3 "$eros_state_helper" compare --before')
    stop_loop = pause.index('for service in "${stop_order[@]}"; do')
    media_recheck = pause.index('media_manifest_output="$(python3 "$media_tree_helper" manifest')
    complete = pause.index("cutover_complete=1")
    assert no_eros_recheck < stop_loop < media_recheck < complete
    assert 'test "$(sed -n \'s/^GPOINT_MEDIA_MANIFEST_SHA256=//p\' <<< "$media_manifest_output")" = "$media_manifest_sha"' in pause
    assert 'if [[ "$status" -ne 0 && "$cutover_complete" -ne 1 ]]; then' in pause
    assert "restore_legacy_services" in pause


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


def test_baseline_capture_keeps_attempt_evidence_and_does_not_rmdir_nonempty_directory():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    section = workflow.split("name: Capture PostgreSQL and no-Eros cutover baseline", 1)[1].split(
        "name: Verify application source matches the live production SHA", 1
    )[0]
    assert 'rm -f "$inventory_script" "$resource_script" "$eros_collector"' in section
    assert 'rmdir "$(dirname "$inventory_script")"' not in section
    assert 'test -s "$baseline_file"' in section
    assert 'test -s "$inventory_file"' in section
    assert 'test -s "$eros_state_file"' in section
    assert 'test "$(sha256sum "$baseline_file"' in section


def test_successful_runtime_verify_does_not_require_empty_attempt_helper_dir():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    section = workflow.split("name: Verify public routes and exact running services", 1)[1].split(
        "name: Record GitHub Deployment result against the same attempt", 1
    )[0]
    assert 'rm -f -- "$eros_collector" "$eros_state_helper"' in section
    assert 'rmdir "$no_eros_dir"' not in section
    assert 'test -s "$manifest_file"' in section
    assert 'test "$(sha256sum "$manifest_file"' in section


def test_first_cutover_readonly_prisma_baseline_runs_from_api_workspace():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    baseline = workflow.split("name: Capture PostgreSQL and no-Eros cutover baseline", 1)[1].split(
        "name: Verify application source matches the live production SHA", 1
    )[0]
    # The Node runtime image is a pnpm workspace. The root /app cannot resolve
    # @prisma/client, whereas /app/apps/api declares it as a dependency.
    api = (ROOT / "apps/api/package.json").read_text()
    assert '"@prisma/client"' in api
    assert 'docker run --rm --network shared_internal --env-file /opt/gpoint/env/production.env --workdir /app/apps/api "$api_image" node -e' in baseline
    assert 'READ_ONLY_DATABASE_BASELINE_FAILED=' in baseline
    assert 'PRISMA_MIGRATIONS=' in baseline


def test_native_workflow_runs_migration_parity_before_deployment_or_pause():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    parity = deploy.index("name: Verify application source migrations match the live database")
    deployment = deploy.index("name: Create exact-SHA in-progress GitHub Deployment")
    pause = deploy.index("name: Pause legacy GPoint services")
    assert parity < deployment < pause


def test_first_cutover_keeps_workflow_and_application_source_identities_separate():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    assert "app_source_sha:" in workflow
    assert "APP_SOURCE_SHA: ${{ inputs.app_source_sha }}" in workflow
    assert "GPOINT_APP_SOURCE_SHA: ${{ inputs.app_source_sha }}" in workflow
    assert "ref: ${{ inputs.app_source_sha }}" in workflow
    assert "path: app-source" in workflow
    assert 'test "$(git -C app-source rev-parse HEAD)" = "$APP_SOURCE_SHA"' in workflow
    build = workflow.split("  build:\n", 1)[1].split("  deploy:\n", 1)[0]
    assert "python3 app-source/scripts/docker_dependency_manifests.py" in build
    assert "context: https://github.com/akhripunov/gpoint.git#${{ inputs.app_source_sha }}" in build
    assert "context: https://github.com/akhripunov/gpoint.git#${{ inputs.app_source_sha }}:services/agata" in build
    assert "build-args: GPOINT_BUILD_SHA=${{ inputs.app_source_sha }}" in build
    assert "org.opencontainers.image.revision=${{ inputs.app_source_sha }}" in build
    miniapp_build = build.split("name: Build and publish immutable Mini App image", 1)[1].split(
        "name: Build and publish immutable AGATA image", 1
    )[0]
    assert "context: https://github.com/akhripunov/gpoint.git#${{ github.sha }}" in miniapp_build
    assert "file: infra/production/Gpoint.Miniapp.Dockerfile" in miniapp_build
    assert "app-source=https://github.com/akhripunov/gpoint.git#${{ inputs.app_source_sha }}" in miniapp_build
    assert "GPOINT_WORKFLOW_SHA=${{ github.sha }}" in miniapp_build
    assert "org.gpoint.workflow.revision=${{ github.sha }}" in miniapp_build
    miniapp_dockerfile = (ROOT / "infra/production/Gpoint.Miniapp.Dockerfile").read_text()
    assert "COPY --from=app-source /apps/miniapp ./apps/miniapp" in miniapp_dockerfile
    assert "COPY infra/production/nginx-miniapp.conf /etc/nginx/conf.d/default.conf" in miniapp_dockerfile
    assert 'source_sha="$APP_SOURCE_SHA"' in build
    assert '"$CANDIDATE_SHA" https://github.com/akhripunov/gpoint.git \\' in build
    assert "--required-material-sha \"$APP_SOURCE_SHA\"" in build
    assert '"$source_sha" https://github.com/akhripunov/gpoint.git' in build
    deploy = workflow.split("  deploy:\n", 1)[1]
    owner_gate = deploy.index("name: Recheck owner authorization and current main for production mode")
    ssh_credentials = deploy.index("name: Install verified Kamal SSH credentials")
    assert owner_gate < ssh_credentials
    assert "Block production until Kamal runtime honors separate application source SHA" not in deploy
    assert '[[ "$CANDIDATE_SHA" != "$APP_SOURCE_SHA" ]]' not in deploy
    source_gate = deploy.index('test "$APP_SOURCE_SHA" = "$LIVE_SOURCE_SHA"')
    migration_gate = deploy.index("name: Verify application source migrations match the live database")
    assert source_gate < migration_gate
    parity_step = deploy[migration_gate:deploy.index("name: Verify fresh PostgreSQL and media backup", migration_gate)]
    assert "app-source/packages/db/prisma/migrations" in parity_step
    assert "app-source/services/agata/migrations/versions" in parity_step
    image_verification = deploy.split("name: Verify GHCR candidate tags resolve to the exact digests", 1)[1].split(
        "name: Install verified Kamal SSH credentials", 1
    )[0]
    assert 'test "$source_sha" = "$APP_SOURCE_SHA"' in image_verification
    parity_check = deploy.split("name: Compare built Mini App static files with live production before mutation", 1)[1].split(
        "name: Capture PostgreSQL and no-Eros cutover baseline", 1
    )[0]
    assert '"$container_id:/usr/share/nginx/html/."' in parity_check
    assert 'container_id="$(docker create --name "$container_name" "$repository@$MINIAPP_DIGEST")"' in parity_check
    assert 'docker cp "$container_id:/usr/share/nginx/html/."' in parity_check
    assert 'docker rm -f "$container_id"' in parity_check
    assert 'docker rm -f "$container_name"' not in parity_check
    assert '--reject-symlink-ancestors /opt/gpoint/app/miniapp' in parity_check
    assert "scripts/gpoint-miniapp-static-manifest.py" in parity_check
    assert "/opt/gpoint/app/miniapp" in parity_check
    assert "GPOINT_MINIAPP_LIVE_STATIC_PARITY=PASS" in parity_check
    assert deploy.index("name: Compare built Mini App static files with live production before mutation") < deploy.index(
        "name: Create exact-SHA in-progress GitHub Deployment"
    )
    assert "scripts/verify-kamal-migration-parity.py" in deploy
    assert "PRISMA_MIGRATIONS=" in deploy
    assert "prisma_migrations|agata_head" in deploy


def test_kamal_cutover_checks_candidate_image_disk_headroom_before_deployment():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    check = deploy.index("name: Check VPS disk headroom for the exact candidate images")
    deployment = deploy.index("name: Create exact-SHA in-progress GitHub Deployment")
    pause = deploy.index("name: Pause legacy GPoint services after restore gates")
    assert check < deployment < pause
    assert "candidate_image_bytes" in deploy
    assert "1024 * 1024 * 1024" in deploy
    assert "KAMAL_CUTOVER_DISK_HEADROOM=PASS" in deploy


def test_kamal_production_verify_persists_attempt_bound_manifest():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    verify = deploy.split("name: Verify public routes and exact running services", 1)[1].split(
        "name: Record GitHub Deployment result against the same attempt", 1
    )[0]
    record = deploy.split("name: Record GitHub Deployment result against the same attempt", 1)[1]
    manifest_writer = (ROOT / "scripts/write-kamal-production-manifest.py").read_text()
    assert "scripts/write-kamal-production-manifest.py" in verify
    assert "scp scripts/write-kamal-production-manifest.py" in verify
    assert 'trap \'rm -f -- "$manifest_writer"\' EXIT' in verify
    assert "gpoint-kamal-production-manifest/v2" in manifest_writer
    assert '"postgresAclFingerprint"' in manifest_writer
    assert '"noErosPostflight"' in manifest_writer
    assert "kamal-production-${run_id}-${attempt}.json" in verify
    assert "CANDIDATE_SHA" in verify and "APP_DIGEST" in verify
    assert "INVENTORY_SHA256" in verify and "BACKUP_SHA256" in verify
    assert "ISOLATED_RESTORE=PASS" in verify
    assert "GPOINT_KAMAL_MANIFEST_SHA256=" in verify
    assert '"$GITHUB_OUTPUT"' in verify and "manifest_sha" in verify
    assert "MANIFEST_SHA: ${{ steps.verify.outputs.manifest_sha }}" in record
    assert 'description="Kamal $state workflow $CANDIDATE_SHA app-source $APP_SOURCE_SHA manifest $MANIFEST_SHA"' in record
    assert '"runtimeServices"' in manifest_writer
    assert '"appSourceSha"' in manifest_writer
    assert '"containerId"' in manifest_writer and '"repositoryDigest"' in manifest_writer
    assert '"mediaBackup"' in manifest_writer and '"mediaRestore"' in manifest_writer


def test_manifest_writer_behavior_tests_run_in_python_ci():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    python_job = workflow.split("  python:\n", 1)[1].split("  build:\n", 1)[0]
    assert "tests/test_kamal_production_manifest.py" in python_job
    oras_setup = python_job.index("name: Install pinned ORAS for release evidence round-trip test")
    evidence_tests = python_job.index("tests/test_deploy_evidence_workflow.py")
    assert oras_setup < evidence_tests
    assert "oras_1.3.0_linux_amd64.tar.gz" in python_job
    assert "6cdc692f929100feb08aa8de584d02f7bcc30ec7d88bc2adc2054d782db57c64" in python_job
    assert '"$GITHUB_ENV"' in python_job


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


def test_pre_app_boot_reads_the_ssh_heredoc_and_fails_closed_on_digest_mismatch():
    candidate = "a" * 40
    app_source_sha = "d" * 40
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
                    print(os.environ["GPOINT_APP_SOURCE_SHA"])
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
                "GPOINT_APP_SOURCE_SHA": app_source_sha,
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
