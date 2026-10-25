from pathlib import Path

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
    assert "run" not in miniapp.get("proxy", {})
    assert set(miniapp["servers"]) == {"web"}
    assert miniapp["servers"]["web"]["proxy"]["host"] == "gpoint.amiora.one"
    dockerfile = (ROOT / "infra/production/Gpoint.Dockerfile").read_text()
    assert "FROM nginx:1.27-alpine AS miniapp-runtime" in dockerfile
    assert "COPY --from=miniapp-build /workspace/apps/miniapp/dist/ /usr/share/nginx/html/" in dockerfile
    assert "COPY --from=miniapp-build /workspace/apps/miniapp/dist /app/apps/miniapp/dist" not in dockerfile
    prepare = (ROOT / ".github/workflows/gpoint-prepare.yml").read_text()
    assert "name: GPoint — Kamal Prepare" in prepare
    assert "name: GPoint — Prepare" not in prepare
    assert "runs-on: ubuntu-latest" in prepare
    assert "runs-on: [self-hosted, macOS, gpoint-production]" not in prepare
    assert "prepare_coalescing.py" not in prepare
    assert "docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8" in prepare
    assert "id: core-image" in prepare and "target: runtime" in prepare
    assert "id: agata-image" in prepare and "context: services/agata" in prepare
    assert "target: miniapp-runtime" in prepare
    assert "ghcr.io/akhripunov/gpoint-miniapp:${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}" in prepare
    assert "MINIAPP_DIGEST: ${{ steps.miniapp-image.outputs.digest }}" in prepare
    assert "CORE_DIGEST: ${{ steps.core-image.outputs.digest }}" in prepare
    assert "AGATA_DIGEST: ${{ steps.agata-image.outputs.digest }}" in prepare


def test_prepare_checks_exact_main_before_publishing_any_image():
    prepare = (ROOT / ".github/workflows/gpoint-prepare.yml").read_text()
    check = prepare.index("name: Require candidate to remain exact main before GHCR publication")
    first_push = prepare.index("docker/build-push-action@")
    assert check < first_push
    assert "gh api \"repos/$GITHUB_REPOSITORY/git/ref/heads/main\" --jq .object.sha" in prepare
    assert 'test "$main_sha" = "$CANDIDATE_SHA"' in prepare


def test_prepare_uses_registry_digest_readback_without_actions_artifact_transport():
    prepare = (ROOT / ".github/workflows/gpoint-prepare.yml").read_text()
    digest_check = prepare.index("name: Verify and record exact Kamal image digests")
    old_receipt = prepare.index("name: Set up pinned ORAS CLI")
    assert digest_check < old_receipt
    assert "${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}" in prepare
    assert 'docker buildx imagetools inspect "$tag"' in prepare
    assert "actions/upload-artifact@" not in prepare
    assert "gpoint-kamal-image-manifest.json" not in prepare
    assert "GPOINT_IMAGE_DIGEST name=%s ref=%s digest=%s sha=%s run=%s attempt=%s" in prepare


def test_kamal_deploy_mode_cannot_run_the_legacy_v5_job():
    workflow = (ROOT / ".github/workflows/gpoint-deploy.yml").read_text()
    assert "options: [preflight, production, kamal-production," in workflow
    legacy = workflow.split("  deploy:\n", 1)[1].split("  deploy-eros-pilot:", 1)[0]
    assert "inputs.mode != 'kamal-production'" in legacy
    kamal = workflow.split("  deploy-kamal:\n", 1)[1].split("  deploy-eros-pilot:", 1)[0]
    assert "if: inputs.mode == 'kamal-production'" in kamal
    assert "runs-on: ubuntu-latest" in kamal
    assert "environment: production" in kamal
    assert "kamal deploy --skip-push" in kamal
    api_deploy = kamal.index("--roles api --config-file config/deploy.yml")
    agata_deploy = kamal.index("--roles api --config-file config/deploy.agata.yml")
    miniapp_deploy = kamal.index("--roles web --config-file config/deploy.miniapp.yml")
    consumers_deploy = kamal.index("--roles worker,bot --config-file config/deploy.yml")
    agata_worker_deploy = kamal.index("--roles memory-worker --config-file config/deploy.agata.yml")
    assert api_deploy < agata_deploy < miniapp_deploy < consumers_deploy < agata_worker_deploy
    assert "gh run view" in kamal
    assert "GPOINT_KAMAL_SSH_PRIVATE_KEY" in kamal
    assert "GPOINT_KAMAL_KNOWN_HOSTS" in kamal
    assert "gsales-api" in kamal
    assert "docker inspect" in kamal
    assert "apps/worker/dist/main.js" in kamal
    assert "apps/bot/dist/main.js" in kamal
    assert "/health/ready" in kamal
    assert "python -m app.memory.worker --healthcheck" in kamal
    assert "wget --spider --quiet http://127.0.0.1/" in kamal
    assert "deployments: write" in kamal
    assert 'ghcr.io/akhripunov/gpoint-runtime|$CORE_DIGEST' in kamal
    assert 'ghcr.io/akhripunov/gpoint-agata-runtime|$AGATA_DIGEST' in kamal
    assert 'ghcr.io/akhripunov/gpoint-miniapp|$MINIAPP_DIGEST' in kamal
    assert "group: gpoint-production-release" in workflow
    assert "status: ${{ steps.verify.outputs.status }}" in kamal
    assert "org.gpoint.release.sha" in kamal
    assert "ghcr.io/basecamp/kamal:v2.12.0@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625" in kamal
    assert "gem install kamal" not in kamal
    assert kamal.index("name: Block unsafe first cutover") < kamal.index("name: Deploy Kamal APIs, route Mini App, then start consumers")
    assert "inputs.mode != 'kamal-production'" in workflow.split("  publish:\n", 1)[1]


def test_caddy_keeps_tls_and_routes_api_and_miniapp_through_kamal_proxy():
    caddy = (ROOT / "infra/production/Caddyfile").read_text()
    assert "uri strip_prefix /api" not in caddy
    assert "reverse_proxy 127.0.0.1:3001" in caddy
    assert "rewrite * /api{uri}" in caddy
    assert caddy.count("reverse_proxy 127.0.0.1:3001") == 3
    assert "reverse_proxy 127.0.0.1:3003" not in caddy
    assert "root * /opt/gpoint/app/miniapp" not in caddy
    assert "compose-gsales-api-1" not in caddy


def test_kamal_network_hook_joins_only_allowed_core_and_agata_roles():
    hook = (ROOT / ".kamal/hooks/post-app-boot").read_text()
    assert 'candidate="${GPOINT_COMMIT_SHA:?GPOINT_COMMIT_SHA is required}"' in hook
    assert '[[ "$version" == "$candidate-"* && "$version" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]' in hook
    assert '[[ "$candidate" =~ ^[0-9a-f]{40}$ ]]' in hook
    assert "gpoint-core:api) networks='gpoint_app:api,shared_internal:'" in hook
    assert "gpoint-agata:api) networks='gpoint_app:agata-api,shared_internal:agata-api'" in hook
    assert "docker network connect --alias \"$alias\" \"$network\" \"$id\"" in hook
    assert "StrictHostKeyChecking=yes" in hook
    assert "gsales" not in hook.lower()
    assert "gpoint-miniapp)" in hook and "exit 0" in hook
    assert 'if [[ "$service:$role" == "gpoint-agata:memory-worker" ]]' in hook
    assert "python -m app.memory.worker --healthcheck" in hook
