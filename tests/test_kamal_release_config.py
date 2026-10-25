from pathlib import Path
import os
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


def test_kamal_deploy_uses_native_verified_images_and_preserves_first_cutover_guard():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    kamal = workflow.split("  deploy:\n", 1)[1]
    assert "runs-on: ubuntu-latest" in kamal
    assert "environment: production" in kamal
    assert "kamal deploy --skip-push" in kamal
    api_deploy = kamal.index("--roles api --config-file config/deploy.yml")
    agata_deploy = kamal.index("--roles api --config-file config/deploy.agata.yml")
    miniapp_deploy = kamal.index("--roles web --config-file config/deploy.miniapp.yml")
    consumers_deploy = kamal.index("--roles worker,bot --config-file config/deploy.yml")
    agata_worker_deploy = kamal.index("--roles memory-worker --config-file config/deploy.agata.yml")
    assert api_deploy < agata_deploy < miniapp_deploy < consumers_deploy < agata_worker_deploy
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
    assert kamal.index("name: Block unsafe first cutover") < kamal.index("name: Deploy API and web roles, then consumers with pinned Kamal 2.12")
    guard = kamal.split("name: Block unsafe first cutover", 1)[1].split("name: Create exact-SHA in-progress GitHub Deployment", 1)[0]
    assert 'if [[ "$running" == true ]]; then' in guard
    assert '"$name" == compose-*' not in guard
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
    assert "ssh -n -T root@38.180.110.92 bash -s <<'REMOTE'" not in workflow
    assert "ssh -n -T root@38.180.110.92 bash -s --" not in workflow
    assert 'ssh -n -T -i "$ssh_key"' not in hook


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
