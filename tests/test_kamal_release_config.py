import pytest

import base64
import json
from pathlib import Path
import os
import hashlib
import subprocess
import tempfile
import textwrap

import yaml

ROOT = Path(__file__).resolve().parents[1]



def all_phases():
    return '\n'.join(phase(name) for name in ('pause','retire','requests','routes','consumers','verify'))

def test_public_routes_use_the_owner_confirmed_gpoint_domain():
    assert "gpoint.one {" in (ROOT / "infra/production/Caddyfile").read_text()
    assert "https://gpoint.one${modelProfilePath(handle)}" in (ROOT / "apps/miniapp/src/model-routing.js").read_text()
    assert "gpoint.amiora.one" not in (ROOT / "config/deploy.yml").read_text()
    assert "gpoint.amiora.one" not in (ROOT / "config/deploy.miniapp.yml").read_text()


def test_shared_proxy_hosts_are_not_redeclared_by_kamal_roles():
    core = yaml.safe_load((ROOT / "config/deploy.yml").read_text())
    miniapp = yaml.safe_load((ROOT / "config/deploy.miniapp.yml").read_text())

    assert core["proxy"]["hosts"] == ["gpoint.one"]
    assert "host" not in core["servers"]["api"]["proxy"]
    assert "hosts" not in core["servers"]["api"]["proxy"]
    assert miniapp["proxy"]["hosts"] == ["gpoint.one"]
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
    assert config["proxy"]["hosts"] == ["gpoint.one"]
    assert config["proxy"]["ssl"] is False
    assert config["proxy"]["ssl_redirect"] is False
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
    assert miniapp["proxy"]["hosts"] == ["gpoint.one"]
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
    assert "MINIAPP_DIGEST: ${{ steps.miniapp.outputs.digest || steps.prepared.outputs.miniapp_digest }}" in workflow
    assert "APP_DIGEST: ${{ steps.app.outputs.digest || steps.prepared.outputs.app_digest }}" in workflow
    assert "AGATA_DIGEST: ${{ steps.agata.outputs.digest || steps.prepared.outputs.agata_digest }}" in workflow


def test_native_first_ci_build_is_preserved_and_kamal_deploy_is_downstream():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    jobs = workflow["jobs"]
    assert {"tests", "build", "deploy"}.issubset(jobs)
    assert not ({"workspace", "python"} & set(jobs))
    assert jobs["build"]["needs"] == ["tests"]
    assert jobs["build"]["if"] == (
        "needs.tests.outputs.mode == 'DEPLOY' "
        "|| needs.tests.outputs.mode == 'ARTIFACT_PREPARE'"
    )
    build_steps = yaml.safe_dump(jobs["build"]["steps"])
    assert "cache-to: type=gha,scope=gpoint-runtime,mode=max" in build_steps
    assert "sbom: true" in build_steps
    assert "provenance: mode=max" in build_steps
    assert jobs["deploy"]["needs"] == ["tests", "build"]
    assert jobs["deploy"]["environment"] == {
        "name": "production",
        "url": "${{ steps.verify.outputs.environment_url }}",
    }
    assert "deployment" not in jobs["deploy"]["environment"]


def test_kamal_uses_attempt_scoped_tag_with_buildkit_digest_verification():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    assert "IMAGE_VERSION: ${{ needs.build.outputs.image_version }}" in deploy
    assert "image_version: ${{ steps.image_version.outputs.image_version }}" in workflow
    assert 'image_version="${CANDIDATE_SHA}-${CURRENT_RUN_ID}-${CURRENT_RUN_ATTEMPT}"' in workflow
    assert "ghcr.io/akhripunov/gpoint-runtime:${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "name: Verify immutable candidate images selected for the live scope" in deploy
    assert 'docker pull --platform linux/amd64 "$repository:$IMAGE_VERSION"' in deploy
    assert 'docker image inspect "$repository:$IMAGE_VERSION" --format' in deploy
    verify_images = deploy.split(
        "name: Verify immutable candidate images selected for the live scope", 1
    )[1].split("name: Verify candidate compatibility with the live database baseline", 1)[0]
    assert '''--format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$CANDIDATE_SHA"''' in verify_images
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$APP_DIGEST"' in deploy
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$AGATA_DIGEST"' in deploy
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST="$MINIAPP_DIGEST"' in deploy
    hook = (ROOT / ".kamal/hooks/pre-app-boot").read_text()
    assert hook.startswith("#!/bin/sh\n")
    assert 'case "$version" in "$candidate"-*)' in hook
    assert 'EXPECTED_REF="$repository@$expected_digest"' in hook
    assert "gpoint-eros-pilot) repository=ghcr.io/akhripunov/gpoint-eros ;;" in hook
    assert 'com.gpoint.source.revision' in hook


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


def test_kamal_network_hook_joins_only_allowed_core_and_agata_roles():
    hook = (ROOT / ".kamal/hooks/post-app-boot").read_text()
    assert hook.startswith("#!/bin/sh\n")
    assert 'case "$version" in "$candidate"-*)' in hook
    assert 'candidate="${GPOINT_COMMIT_SHA:?GPOINT_COMMIT_SHA is required}"' in hook
    assert 'app_source_sha="${GPOINT_APP_SOURCE_SHA:?GPOINT_APP_SOURCE_SHA is required}"' in hook
    assert '[[ "$version" == "$candidate-"* && "$version" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]' in hook
    assert '"$version" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$' in hook
    assert '"$version" =~ ^[0-9a-f]{40}$' not in hook
    assert "printf '%s\\n' \"$candidate\" | grep -Eq '^[0-9a-f]{40}$'" in hook
    assert 'test "$(docker inspect --format' in hook and '"$app_source_sha"' in hook
    assert "gpoint-core:api) networks='gpoint_app:api,shared_internal:'" in hook
    assert "gpoint-agata:api) networks='gpoint_app:agata-api,shared_internal:agata-api'" in hook
    assert "docker network connect --alias \"$alias\" \"$network\" \"$id\"" in hook
    assert "StrictHostKeyChecking=yes" in hook
    assert "gsales" not in hook.lower()
    assert "gpoint-miniapp:web) networks='gpoint_app:'" in hook
    assert "gpoint-eros-pilot)" in hook and "gpoint-eros-relay)" in hook
    assert "gpoint-eros-pilot:engine|gpoint-eros-relay:relay) networks=''" in hook
    assert "Eros engine is not attached to its data network" in hook
    assert "getent hosts api" in hook
    assert 'GPOINT_KAMAL_ROOT_ROUTE_BOOTSTRAP' in hook
    assert 'DEFERRED_FOR_FIRST_CUTOVER' in hook
    assert hook.index('docker exec "$id" nginx -t') < hook.index('GPOINT_KAMAL_ROOT_ROUTE_BOOTSTRAP')
    assert 'api_container="gpoint-core-api-${core_version}"' in hook
    assert "resolved_ip" in hook and "test \"$resolved_ip\" = \"$api_ip\"" in hook
    assert "nginx -t" in hook
    assert 'if [[ "$service:$role" == "gpoint-agata:memory-worker" ]]' in hook
    assert "python -m app.memory.worker --healthcheck" in hook


@pytest.mark.parametrize("name", ["pre-app-boot", "post-app-boot"])
def test_kamal_hooks_parse_with_the_shell_available_in_the_pinned_cli(name):
    hook = ROOT / ".kamal/hooks" / name
    assert hook.read_text().startswith("#!/bin/sh\n")
    result = subprocess.run(["sh", "-n", str(hook)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("wrong_sha,wrong_dns,expected", [(False, False, 0), (True, False, 1), (False, True, 1)])
def test_miniapp_post_boot_checks_independent_core_identity(tmp_path, wrong_sha, wrong_dns, expected):
    candidate, core_sha = "a" * 40, "b" * 40
    core_version = core_sha + "-100-1"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("key", "known_hosts"):
        (tmp_path / name).write_text("synthetic fixture\n")
    (bin_dir / "ssh").write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import subprocess, sys
        args = sys.argv[1:]
        host = next(i for i, arg in enumerate(args) if arg.startswith('root@'))
        raise SystemExit(subprocess.run(args[host + 1:]).returncode)
    '''))
    (bin_dir / "docker").write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import json, os, sys
        args = sys.argv[1:]
        with open(os.environ['CALLS'], 'a') as log:
            log.write(json.dumps(args) + '\\n')
        if args[:2] == ['network', 'inspect']:
            raise SystemExit(0)
        if args[:1] == ['inspect']:
            template, target = args[2], args[3]
            core = target.startswith('gpoint-core-api-')
            if core and target != 'gpoint-core-api-' + os.environ['GPOINT_KAMAL_CORE_API_VERSION']:
                raise SystemExit('Core must retain its independent version')
            if template == '{{.Id}}': print('miniapp-id')
            elif template == '{{.State.Running}}': print('true')
            elif 'org.opencontainers.image.revision' in template:
                print(('c' * 40 if os.environ['WRONG_SHA'] == 'true' else os.environ['GPOINT_KAMAL_CORE_API_SOURCE_SHA'])
                      if core else os.environ['GPOINT_APP_SOURCE_SHA'])
            elif 'NetworkSettings.Networks' in template:
                print('172.20.0.8' if core else json.dumps({'gpoint_app': {'Aliases': []}}))
            else: raise SystemExit('unexpected inspect: ' + template)
            raise SystemExit(0)
        if args[:1] == ['exec'] and args[2:] == ['nginx', '-t']:
            raise SystemExit(0)
        if args[:1] == ['exec'] and args[2:] == ['sh', '-ec', 'getent hosts api']:
            print(('172.20.0.9' if os.environ['WRONG_DNS'] == 'true' else '172.20.0.8') + ' api')
            raise SystemExit(0)
        raise SystemExit('unexpected Docker call: ' + repr(args))
    '''))
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
               GPOINT_KAMAL_SERVICE="gpoint-miniapp", KAMAL_ROLES="web", KAMAL_HOSTS="127.0.0.1",
               KAMAL_VERSION=candidate + "-200-1", GPOINT_COMMIT_SHA=candidate,
               GPOINT_APP_SOURCE_SHA=candidate, GPOINT_KAMAL_CORE_API_VERSION=core_version,
               GPOINT_KAMAL_CORE_API_SOURCE_SHA=core_sha, GPOINT_SSH_KEY=str(tmp_path / "key"),
               GPOINT_KNOWN_HOSTS_FILE=str(tmp_path / "known_hosts"), CALLS=str(tmp_path / "calls"),
               WRONG_SHA=str(wrong_sha).lower(), WRONG_DNS=str(wrong_dns).lower())
    result = subprocess.run(["sh", str(ROOT / ".kamal/hooks/post-app-boot")], env=env,
                            text=True, capture_output=True)
    assert (result.returncode == 0) == (expected == 0), result.stderr
    if expected == 0:
        assert 'GPOINT_MINIAPP_API_NETWORK=PASS' in result.stdout


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
    assert "GPOINT_APP_SOURCE_SHA: ${{ github.event_name == 'workflow_dispatch' && inputs.candidate_sha || github.sha }}" in native_workflow
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


def test_kamal_migration_parity_uses_alembic_script_directory_for_revision_graph(tmp_path):
    prisma_dir = tmp_path / "prisma"
    migration = prisma_dir / "20261015130000_baseline"
    migration.mkdir(parents=True)
    (migration / "migration.sql").write_text("SELECT 1;\n", encoding="utf-8")
    agata_dir = tmp_path / "agata"
    agata_dir.mkdir()
    (agata_dir / "agata0001_baseline.py").write_text(
        'revision = f"agata{1:04d}"\ndown_revision = None\n', encoding="utf-8"
    )
    (agata_dir / "agata0002_messages.py").write_text(
        'revision = "agata0002"\ndown_revision = "agata0001"\n', encoding="utf-8"
    )

    result = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--prisma-directory", str(prisma_dir), "--prisma-current-head", "20261015130000_baseline",
         "--prisma-current-migrations", "20261015130000_baseline",
         "--prisma-current-checksums", "20261015130000_baseline=" + hashlib.sha256(b"SELECT 1;\n").hexdigest(),
         "--agata-directory", str(agata_dir), "--agata-current-head", "agata0002"],
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "GPOINT_KAMAL_MIGRATION_PARITY=PASS" in result.stdout
    assert "AGATA_HEAD=agata0002" in result.stdout


def test_kamal_migration_parity_rejects_changed_applied_prisma_sql(tmp_path):
    prisma_dir = tmp_path / "prisma"
    migration = prisma_dir / "20261015130000_codex_telegram_challenge"
    migration.mkdir(parents=True)
    (migration / "migration.sql").write_text("SELECT 1;\n", encoding="utf-8")
    agata_dir = tmp_path / "agata"
    agata_dir.mkdir()
    (agata_dir / "agata0010_baseline.py").write_text("revision = 'agata0010'\ndown_revision = None\n")
    recorded = hashlib.sha256(b"SELECT 2;\n").hexdigest()
    result = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--prisma-directory", str(prisma_dir), "--prisma-current-head", "20261015130000_codex_telegram_challenge",
         "--prisma-current-migrations", "20261015130000_codex_telegram_challenge",
         "--prisma-current-checksums", "20261015130000_codex_telegram_challenge=" + recorded,
         "--agata-directory", str(agata_dir), "--agata-current-head", "agata0010"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "PRISMA_MIGRATION_CHECKSUM_MISMATCH" in result.stderr


def test_kamal_migration_parity_fails_closed_for_pending_candidate_revision(tmp_path):
    prisma_dir = tmp_path / "prisma"
    prisma_dir.mkdir()
    (prisma_dir / "20261015130000_codex_telegram_challenge").mkdir()
    (prisma_dir / "20261015130000_codex_telegram_challenge" / "migration.sql").write_text("SELECT 1;\n")
    (prisma_dir / "20261022110000_eros_gpoint_message_state").mkdir()
    (prisma_dir / "20261022110000_eros_gpoint_message_state" / "migration.sql").write_text("SELECT 1;\n")
    agata_dir = tmp_path / "agata"
    agata_dir.mkdir()
    (agata_dir / "agata0010_baseline.py").write_text("revision = 'agata0010'\ndown_revision = None\n")
    (agata_dir / "agata0011_messages.py").write_text("revision = 'agata0011'\ndown_revision = 'agata0010'\n")
    (agata_dir / "agata0012_eros.py").write_text("revision = 'agata0012'\ndown_revision = 'agata0011'\n")
    result = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--prisma-directory", str(prisma_dir), "--prisma-current-head", "20261015130000_codex_telegram_challenge",
         "--prisma-current-migrations", "20261015130000_codex_telegram_challenge",
         "--agata-directory", str(agata_dir), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "PENDING" in result.stderr


def test_kamal_schema_compatibility_allows_unrelated_code_with_pending_migrations(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    prisma = repo / "packages/db/prisma/migrations"
    base_sql = "SELECT 1;\n"
    base_name = "20261015130000_baseline"
    (prisma / base_name).mkdir(parents=True)
    (prisma / base_name / "migration.sql").write_text(base_sql, encoding="utf-8")
    pending_name = "20261022110000_eros_worker_queue"
    (prisma / pending_name).mkdir()
    (prisma / pending_name / "migration.sql").write_text(
        "CREATE TABLE engine.eros_inference_jobs (id UUID PRIMARY KEY, error_code VARCHAR(64), status TEXT);\n"
        "ALTER TABLE engine.eros_inference_jobs RENAME COLUMN legacy_code TO normalized_code;\n", encoding="utf-8")
    smoke_name = "20261104144700_native_release_smoke"
    (prisma / smoke_name).mkdir()
    (prisma / smoke_name / "migration.sql").write_text(
        "-- Production release pipeline smoke test: ledger-only, no schema or data changes.\n"
        "SELECT 1;\n", encoding="utf-8")
    agata = repo / "services/agata/migrations/versions"
    agata.mkdir(parents=True)
    (agata / "agata0011_messages.py").write_text("revision = 'agata0011'\ndown_revision = None\n", encoding="utf-8")
    (agata / "agata0012_eros.py").write_text(
        "revision = 'agata0012'\ndown_revision = 'agata0011'\n"
        "SQL = \"UPDATE agata.regional_policy_profiles SET status='retired' "
        "WHERE rules->>'ai_disclosure_required'='true'\"\n", encoding="utf-8")
    agata_models = repo / "services/agata/app/db/models.py"
    agata_models.parent.mkdir(parents=True)
    agata_models.write_text(
        "class RegionalPolicyProfile:\n"
        "    __tablename__ = 'regional_policy_profiles'\n"
        "    status: Mapped[str]\n"
        "    rules: Mapped[dict]\n", encoding="utf-8")
    agata_app_file = repo / "services/agata/app/compliance/policy.py"
    agata_app_file.parent.mkdir(parents=True)
    agata_app_file.write_text("policy_check = True\n", encoding="utf-8")
    app_file = repo / "apps/api/src/health.ts"
    app_file.parent.mkdir(parents=True)
    app_file.write_text("export const health = true;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
    baseline_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    app_file.write_text("export const health = true;\nexport const releaseCheck = true;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "unrelated app update"], check=True)
    candidate_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    result = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--policy", "compatible", "--repo-root", str(repo),
         "--baseline-source-sha", baseline_sha, "--candidate-source-sha", candidate_sha,
         "--prisma-directory", str(prisma), "--prisma-current-head", base_name,
         "--prisma-current-migrations", base_name,
         "--prisma-current-checksums", base_name + "=" + hashlib.sha256(base_sql.encode()).hexdigest(),
         "--agata-directory", str(agata), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "GPOINT_KAMAL_SCHEMA_COMPATIBILITY=PASS" in result.stdout
    assert "GPOINT_KAMAL_DB_MIGRATION_REQUIRED=YES" in result.stdout
    native_release = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--policy", "compatible", "--require-no-agata-pending", "--repo-root", str(repo),
         "--baseline-source-sha", baseline_sha, "--candidate-source-sha", candidate_sha,
         "--prisma-directory", str(prisma), "--prisma-current-head", base_name,
         "--prisma-current-migrations", base_name,
         "--prisma-current-checksums", base_name + "=" + hashlib.sha256(base_sql.encode()).hexdigest(),
         "--agata-directory", str(agata), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert native_release.returncode != 0
    assert "PENDING_AGATA_MIGRATION" in native_release.stderr
    app_file.write_text("export const errorCode = 'error_code';\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "app depends on pending varchar field"], check=True)
    field_dependent_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    field_blocked = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--policy", "compatible", "--repo-root", str(repo),
         "--baseline-source-sha", baseline_sha, "--candidate-source-sha", field_dependent_sha,
         "--prisma-directory", str(prisma), "--prisma-current-head", base_name,
         "--prisma-current-migrations", base_name,
         "--prisma-current-checksums", base_name + "=" + hashlib.sha256(base_sql.encode()).hexdigest(),
         "--agata-directory", str(agata), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert field_blocked.returncode != 0
    assert "PENDING_SCHEMA_DEPENDENCY:20261022110000_eros_worker_queue:error_code" in field_blocked.stderr
    app_file.write_text("export const state = 'engine.eros_inference_jobs.status';\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "app depends on pending common status field"], check=True)
    status_dependent_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    status_blocked = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--policy", "compatible", "--repo-root", str(repo),
         "--baseline-source-sha", baseline_sha, "--candidate-source-sha", status_dependent_sha,
         "--prisma-directory", str(prisma), "--prisma-current-head", base_name,
         "--prisma-current-migrations", base_name,
         "--prisma-current-checksums", base_name + "=" + hashlib.sha256(base_sql.encode()).hexdigest(),
         "--agata-directory", str(agata), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert status_blocked.returncode != 0
    assert "PENDING_SCHEMA_DEPENDENCY:20261022110000_eros_worker_queue:" in status_blocked.stderr
    assert "apps/api/src/health.ts" in status_blocked.stderr
    app_file.write_text("export const code = 'normalized_code';\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "app depends on renamed pending field"], check=True)
    renamed_field_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    renamed_field_blocked = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--policy", "compatible", "--repo-root", str(repo),
         "--baseline-source-sha", baseline_sha, "--candidate-source-sha", renamed_field_sha,
         "--prisma-directory", str(prisma), "--prisma-current-head", base_name,
         "--prisma-current-migrations", base_name,
         "--prisma-current-checksums", base_name + "=" + hashlib.sha256(base_sql.encode()).hexdigest(),
         "--agata-directory", str(agata), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert renamed_field_blocked.returncode != 0
    assert "PENDING_SCHEMA_DEPENDENCY:20261022110000_eros_worker_queue:normalized_code" in renamed_field_blocked.stderr
    checksum_drift = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--policy", "compatible", "--repo-root", str(repo),
         "--baseline-source-sha", baseline_sha, "--candidate-source-sha", candidate_sha,
         "--prisma-directory", str(prisma), "--prisma-current-head", base_name,
         "--prisma-current-migrations", base_name, "--prisma-current-checksums", base_name + "=" + "0" * 64,
         "--agata-directory", str(agata), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert checksum_drift.returncode != 0
    assert "PRISMA_MIGRATION_CHECKSUM_MISMATCH" in checksum_drift.stderr

    (agata / "agata0011_messages.py").write_text(
        "revision = 'agata0011'\ndown_revision = None\n# altered after production apply\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "alter applied AGATA revision"], check=True)
    changed_revision_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    changed_revision = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--policy", "compatible", "--repo-root", str(repo),
         "--baseline-source-sha", baseline_sha, "--candidate-source-sha", changed_revision_sha,
         "--prisma-directory", str(prisma), "--prisma-current-head", base_name,
         "--prisma-current-migrations", base_name,
         "--prisma-current-checksums", base_name + "=" + hashlib.sha256(base_sql.encode()).hexdigest(),
         "--agata-directory", str(agata), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert changed_revision.returncode != 0
    assert "APPLIED_ALEMBIC_MIGRATION_CHANGED:agata0011" in changed_revision.stderr

    (agata / "agata0011_messages.py").write_text("revision = 'agata0011'\ndown_revision = None\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "restore applied AGATA migration identity"], check=True)
    app_file.write_text("export const health = true;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "restore unrelated Core application"], check=True)
    agata_app_file.write_text("policy_check = RegionalPolicyProfile.rules['ai_disclosure_required']\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "app depends on pending AGATA policy key"], check=True)
    agata_dependent_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    agata_blocked = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--policy", "compatible", "--repo-root", str(repo),
         "--baseline-source-sha", baseline_sha, "--candidate-source-sha", agata_dependent_sha,
         "--prisma-directory", str(prisma), "--prisma-current-head", base_name,
         "--prisma-current-migrations", base_name,
         "--prisma-current-checksums", base_name + "=" + hashlib.sha256(base_sql.encode()).hexdigest(),
         "--agata-directory", str(agata), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert agata_blocked.returncode != 0
    assert "PENDING_SCHEMA_DEPENDENCY:agata0012:" in agata_blocked.stderr
    assert "services/agata/app/compliance/policy.py" in agata_blocked.stderr

    agata_app_file.write_text("policy_state = RegionalPolicyProfile.status\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "app depends on pending AGATA policy state"], check=True)
    agata_state_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    agata_state_blocked = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--policy", "compatible", "--repo-root", str(repo),
         "--baseline-source-sha", baseline_sha, "--candidate-source-sha", agata_state_sha,
         "--prisma-directory", str(prisma), "--prisma-current-head", base_name,
         "--prisma-current-migrations", base_name,
         "--prisma-current-checksums", base_name + "=" + hashlib.sha256(base_sql.encode()).hexdigest(),
         "--agata-directory", str(agata), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert agata_state_blocked.returncode != 0
    assert "PENDING_SCHEMA_DEPENDENCY:agata0012:" in agata_state_blocked.stderr
    assert "services/agata/app/compliance/policy.py" in agata_state_blocked.stderr

    agata_app_file.write_text("policy_check = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "restore unrelated AGATA application"], check=True)
    app_file.write_text("export const health = true;\nexport const queue = 'engine.eros_inference_jobs';\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "app depends on pending queue"], check=True)
    dependent_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    blocked = subprocess.run(
        ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
         "--policy", "compatible", "--repo-root", str(repo),
         "--baseline-source-sha", baseline_sha, "--candidate-source-sha", dependent_sha,
         "--prisma-directory", str(prisma), "--prisma-current-head", base_name,
         "--prisma-current-migrations", base_name,
         "--prisma-current-checksums", base_name + "=" + hashlib.sha256(base_sql.encode()).hexdigest(),
         "--agata-directory", str(agata), "--agata-current-head", "agata0011"],
        capture_output=True, text=True,
    )
    assert blocked.returncode != 0
    assert "PENDING_SCHEMA_DEPENDENCY" in blocked.stderr


def test_kamal_schema_compatibility_accepts_divergent_live_source_only_when_tree_changes_are_preserved(tmp_path):
    repo = tmp_path / "divergent-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)

    prisma = repo / "packages/db/prisma/migrations"
    base_name = "20261015130000_baseline"
    base_sql = "SELECT 1;\n"
    (prisma / base_name).mkdir(parents=True)
    (prisma / base_name / "migration.sql").write_text(base_sql, encoding="utf-8")
    pending_name = "20261022110000_pending_queue"
    (prisma / pending_name).mkdir()
    (prisma / pending_name / "migration.sql").write_text(
        "CREATE TABLE engine.pending_queue (id UUID PRIMARY KEY);\n", encoding="utf-8")
    agata = repo / "services/agata/migrations/versions"
    agata.mkdir(parents=True)
    (agata / "agata0011_messages.py").write_text("revision = 'agata0011'\ndown_revision = None\n", encoding="utf-8")
    (agata / "agata0012_eros.py").write_text(
        "revision = 'agata0012'\ndown_revision = 'agata0011'\n"
        "from alembic import op\nimport sqlalchemy as sa\n"
        "def upgrade():\n    op.add_column('regional_policy_profiles', sa.Column('new_field', sa.String()))\n",
        encoding="utf-8")
    live_app = repo / "apps/miniapp/src/model-routing.js"
    live_app.parent.mkdir(parents=True)
    live_app.write_text("export const host = 'gpoint.one';\n", encoding="utf-8")
    live_test = repo / "apps/miniapp/test/model-routing.test.js"
    live_test.parent.mkdir(parents=True)
    live_test.write_text("assert.equal(host, 'gpoint.one');\n", encoding="utf-8")
    api_file = repo / "apps/api/src/health.ts"
    api_file.parent.mkdir(parents=True)
    api_file.write_text("export const healthy = true;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "common production ancestor"], check=True)
    common_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    subprocess.run(["git", "-C", str(repo), "switch", "-qc", "live-source"], check=True)
    live_app.write_text("export const host = 'gpoint.one';\nexport const routing = 'live';\n", encoding="utf-8")
    live_test.write_text("assert.equal(host, 'gpoint.one');\nassert.equal(routing, 'live');\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "live source routing update"], check=True)
    baseline_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    subprocess.run(["git", "-C", str(repo), "switch", "-qc", "candidate", common_sha], check=True)
    live_app.write_text("export const host = 'gpoint.one';\nexport const routing = 'live';\n", encoding="utf-8")
    live_test.write_text("assert.equal(host, 'gpoint.one');\nassert.equal(routing, 'live');\n", encoding="utf-8")
    api_file.write_text("export const healthy = true;\nexport const releaseCheck = true;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "candidate includes live tree"], check=True)
    candidate_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    def run(candidate):
        return subprocess.run(
            ["python3", str(ROOT / "scripts/verify-kamal-migration-parity.py"),
             "--policy", "compatible", "--repo-root", str(repo),
             "--baseline-source-sha", baseline_sha, "--candidate-source-sha", candidate,
             "--prisma-directory", str(prisma), "--prisma-current-head", base_name,
             "--prisma-current-migrations", base_name,
             "--prisma-current-checksums", base_name + "=" + hashlib.sha256(base_sql.encode()).hexdigest(),
             "--agata-directory", str(agata), "--agata-current-head", "agata0011"],
            capture_output=True, text=True,
        )

    compatible = run(candidate_sha)
    assert compatible.returncode == 0, compatible.stderr
    assert "GPOINT_KAMAL_SCHEMA_COMPATIBILITY=PASS" in compatible.stdout
    assert "GPOINT_KAMAL_DB_MIGRATION_REQUIRED=YES" in compatible.stdout

    live_app.write_text("export const host = 'gpoint.one';\nexport const routing = 'candidate-only';\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "candidate drops live source change"], check=True)
    missing_live_change = run(subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip())
    assert missing_live_change.returncode != 0
    assert "LIVE_SOURCE_TREE_NOT_PRESERVED" in missing_live_change.stderr


def test_manifest_writer_behavior_tests_run_in_python_ci():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    tests_job = workflow.split("  tests:\n", 1)[1].split("  build:\n", 1)[0]
    assert "tests/test_kamal_production_manifest.py" in tests_job
    oras_setup = tests_job.index("name: Install pinned ORAS for release evidence round-trip test")
    assert "Install pinned ORAS for release evidence round-trip test" in tests_job
    assert "oras_1.3.0_linux_amd64.tar.gz" in tests_job
    assert "6cdc692f929100feb08aa8de584d02f7bcc30ec7d88bc2adc2054d782db57c64" in tests_job
    assert "--retry 5 --retry-all-errors --retry-delay 2 --retry-max-time 60" in tests_job
    assert "api.github.com/repos/oras-project/oras/releases/assets/290827355" in tests_job
    assert "Accept: application/octet-stream" in tests_job
    assert '"$GITHUB_ENV"' in tests_job


def test_pinned_setup_go_uses_official_digest_pinned_tool_cache_seed():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    steps = workflow["jobs"]["tests"]["steps"]
    seed_index = next(i for i, step in enumerate(steps)
                      if step.get("name") == "Seed pinned Go 1.25.3 in Actions tool cache")
    setup_index = next(i for i, step in enumerate(steps)
                       if step.get("uses", "").startswith("actions/setup-go@"))
    seed = steps[seed_index]
    setup = steps[setup_index]
    assert seed_index < setup_index
    assert seed["if"] == "matrix.suite == 'workspace'"
    assert "api.github.com/repos/actions/go-versions/releases/assets/303957986" in seed["run"]
    assert "Accept: application/octet-stream" in seed["run"]
    assert "1b6c5c84f587ab036ca904638a39cf4bd272d1e8d439eb9c8bc60f91793e0765" in seed["run"]
    assert "$RUNNER_TOOL_CACHE/go/1.25.3/x64" in seed["run"]
    assert '"$RUNNER_TOOL_CACHE/go/1.25.3/x64.complete"' in seed["run"]
    assert setup["uses"] == "actions/setup-go@d35c59abb061a4a6fb18e82ac0862c26744d6ab5"
    assert setup["with"]["go-version"] == "1.25.3"


def test_native_release_runs_kamal_after_successful_main_push():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    deploy = workflow["jobs"]["deploy"]
    assert deploy["if"] == "needs.tests.outputs.deploy == 'true' && needs.build.outputs.deploy_scope != 'NONE' && github.ref == 'refs/heads/main' && (github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    assert deploy["environment"] == {
        "name": "production",
        "url": "${{ steps.verify.outputs.environment_url }}",
    }
    deploy_script = "\n".join(step.get("run", "") for step in deploy["steps"])
    assert 'test "$APPROVE_PRODUCTION" = true' in deploy_script
    assert 'test "$REQUESTED_SHA" = "$WORKFLOW_SHA"' in deploy_script
    assert "Resolve exact GitHub environment Deployment ID" in [step.get("name") for step in deploy["steps"]]
    assert 'repos/$GITHUB_REPOSITORY/deployments/$DEPLOYMENT_ID/statuses' not in deploy_script

    rollback_workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-operations.yml').read_text())
    rollback = rollback_workflow["jobs"]["rollback"]
    assert rollback["environment"] == {
        "name": "production", "url": "${{ steps.verify.outputs.environment_url }}",
    }
    rollback_script = "\n".join(step.get("run", "") for step in rollback["steps"])
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/deployments' not in rollback_script
    assert "gpoint-runtime-target-sha=" in rollback_script


def test_native_migration_parity_runs_prisma_from_api_workspace():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    step = workflow.split("name: Verify candidate compatibility with the live database baseline", 1)[1].split(
        "name: Create exact-SHA in-progress GitHub Deployment", 1)[0]
    assert "--workdir /app/apps/api" in step
    assert "--policy compatible" in step
    assert "--baseline-source-sha \"$BASELINE_SOURCE_SHA\"" in step
    assert "--candidate-source-sha \"$CANDIDATE_SHA\"" in step
    assert "prisma_migrations=$prisma_migrations" in step


def test_native_runtime_verify_compares_live_database_to_its_predeploy_baseline():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1].split("  backup_maintenance:\n", 1)[0]
    verify = deploy.split("name: Verify public routes and exact running services", 1)[1].split(
        "name: Record GitHub Deployment result against the same attempt", 1
    )[0]
    assert "BASELINE_PRISMA_MIGRATIONS: ${{ steps.migration_baseline.outputs.prisma_migrations }}" in verify
    assert "BASELINE_PRISMA_CHECKSUMS: ${{ steps.migration_baseline.outputs.prisma_checksums }}" in verify
    assert 'test "$prisma_migrations" = "$expected_prisma_migrations"' in verify
    assert 'test "$prisma_checksums" = "$expected_prisma_checksums"' in verify
    assert 'test "$agata_head" = "$expected_agata_head"' in verify
    assert "find packages/db/prisma/migrations" not in verify
    assert "alembic_head('services/agata/migrations/versions')" not in verify


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
                ["sh", str(ROOT / ".kamal/hooks/pre-app-boot")],
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


def test_post_migration_head_requires_checksum_proof_when_old_runtime_lacks_revision(tmp_path):
    repo=tmp_path / "post-migration"; repo.mkdir()
    for args in (("init","-q"),("config","user.name","Test"),("config","user.email","test@example.invalid")):
        subprocess.run(["git","-C",str(repo),*args],check=True)
    prisma=repo / "packages/db/prisma/migrations"; name="20261015130000_baseline"
    (prisma/name).mkdir(parents=True); (prisma/name/"migration.sql").write_text("SELECT 1;\n")
    agata=repo / "services/agata/migrations/versions"; agata.mkdir(parents=True)
    (agata/"0011.py").write_text("revision='agata0011'\ndown_revision=None\n")
    subprocess.run(["git","-C",str(repo),"add","."],check=True)
    subprocess.run(["git","-C",str(repo),"commit","-qm","old runtime"],check=True)
    baseline=subprocess.check_output(["git","-C",str(repo),"rev-parse","HEAD"],text=True).strip()
    (agata/"0012.py").write_text("revision='agata0012'\ndown_revision='agata0011'\n")
    subprocess.run(["git","-C",str(repo),"add","."],check=True)
    subprocess.run(["git","-C",str(repo),"commit","-qm","approved migration"],check=True)
    candidate=subprocess.check_output(["git","-C",str(repo),"rev-parse","HEAD"],text=True).strip()
    command=["python3",str(ROOT/"scripts/verify-kamal-migration-parity.py"),"--policy","compatible",
             "--repo-root",str(repo),"--baseline-source-sha",baseline,"--candidate-source-sha",candidate,
             "--prisma-directory",str(prisma),"--prisma-current-head",name,"--prisma-current-migrations",name,
             "--prisma-current-checksums",name+"="+hashlib.sha256((prisma/name/"migration.sql").read_bytes()).hexdigest(),
             "--agata-directory",str(agata),"--agata-current-head","agata0012"]
    checksum=hashlib.sha256((agata/"0012.py").read_bytes()).hexdigest()
    assert subprocess.run(command,capture_output=True).returncode != 0
    accepted=subprocess.run(command+["--agata-current-checksums","agata0012="+checksum],capture_output=True,text=True)
    assert accepted.returncode == 0, accepted.stderr
    assert subprocess.run(command+["--agata-current-checksums","agata0012="+"0"*64],capture_output=True).returncode != 0
    (agata/"0011.py").write_text("revision='agata0011'\ndown_revision=None\n# changed old applied revision\n")
    subprocess.run(["git","-C",str(repo),"add","."],check=True)
    subprocess.run(["git","-C",str(repo),"commit","-qm","forbidden rewrite"],check=True)
    command[command.index("--candidate-source-sha")+1]=subprocess.check_output(["git","-C",str(repo),"rev-parse","HEAD"],text=True).strip()
    rejected=subprocess.run(command+["--agata-current-checksums","agata0012="+checksum],capture_output=True,text=True)
    assert rejected.returncode != 0 and "APPLIED_ALEMBIC_MIGRATION_CHANGED:agata0011" in rejected.stderr
    (agata/"0011.py").write_text("revision='agata0011'\ndown_revision=None\n")
    (agata/"0012.py").rename(agata/"renamed.py")
    (agata/"renamed.py").write_text("revision='agata0012'\ndown_revision='agata0011'\n# rewritten under another filename\n")
    subprocess.run(["git","-C",str(repo),"add","-A"],check=True)
    subprocess.run(["git","-C",str(repo),"commit","-qm","rename applied revision"],check=True)
    command[command.index("--baseline-source-sha")+1]=candidate
    command[command.index("--candidate-source-sha")+1]=subprocess.check_output(["git","-C",str(repo),"rev-parse","HEAD"],text=True).strip()
    renamed_checksum=hashlib.sha256((agata/"renamed.py").read_bytes()).hexdigest()
    renamed=subprocess.run(command+["--agata-current-checksums","agata0012="+renamed_checksum],capture_output=True,text=True)
    assert renamed.returncode != 0 and "APPLIED_ALEMBIC_MIGRATION_CHANGED:agata0012" in renamed.stderr


def test_native_kamal_keeps_absolute_runner_ssh_config_paths_available():
    workflow = (ROOT / '.github/workflows/gpoint-layer7-staging.yml').read_text()
    wrappers = workflow.split('kamal() {')[1:]
    assert wrappers
    for wrapper in wrappers:
        command = wrapper.split('\n          }', 1)[0]
        if 'command kamal "$@"' in command:
            assert 'docker run' not in command
        elif 'gpoint_layer7:/root/.ssh/gpoint_layer7:ro' in command:
            assert '--volume "$GITHUB_WORKSPACE/infra/staging/layer7_known_hosts:/root/.ssh/known_hosts:ro"' in command
            assert '--volume "$HOME/.ssh:/root/.ssh:ro"' not in command
            assert '--volume "$HOME/.ssh:$HOME/.ssh:ro"' not in command
        else:
            assert '--volume "$HOME/.ssh:/root/.ssh:ro"' in command
            assert '--volume "$HOME/.ssh:$HOME/.ssh:ro"' in command
    assert 'export GPOINT_SSH_KEY="$HOME/.ssh/gpoint_layer7"' in workflow
    assert 'export GPOINT_KNOWN_HOSTS_FILE="$HOME/.ssh/known_hosts"' in workflow
    native = (ROOT / '.github/workflows/gpoint-native-release.yml').read_text()
    assert 'git rev-list --first-parent --max-count=64 "$CANDIDATE_SHA"' in native
    assert '--read-agata-ancestor-receipt' in native


@pytest.mark.parametrize('name', ['pre-app-boot', 'post-app-boot'])
def test_kamal_hooks_use_only_the_explicit_pinned_ssh_configuration(name):
    hook = (ROOT / '.kamal/hooks' / name).read_text()
    ssh_calls = [line.strip() for line in hook.splitlines() if line.strip().startswith('ssh ')]
    assert ssh_calls
    assert all(call.startswith('ssh -F /dev/null -T -i "$ssh_key"') for call in ssh_calls)
    assert '-o StrictHostKeyChecking=yes' in hook
    assert '-o UserKnownHostsFile="$known_hosts"' in hook


def test_memory_worker_has_database_network_before_process_start():
    agata = yaml.safe_load((ROOT / 'config/deploy.agata.yml').read_text())
    assert agata['servers']['memory-worker']['options']['network'] == 'shared_internal'
    # API aliases remain assigned by the existing post-boot hook.
    assert 'network' not in agata['servers']['api']['options']


def test_ordinary_verify_receives_baseline_facts_in_remote_shell():
    import re
    workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-native-release.yml').read_text())
    step = next(s for j in workflow['jobs'].values() for s in j.get('steps', [])
                if s.get('name') == 'Verify public routes and exact running services')
    match = re.search(r'facts="\$\(ssh([^\n]+)<<\x27REMOTE\x27\n(.*?)\nREMOTE\n', step['run'], re.S)
    assert match
    values = {
        'CANDIDATE_SHA': 'a' * 40, 'IMAGE_VERSION': 'a' * 40 + '-123-1',
        'APP_DIGEST': 'sha256:' + 'b' * 64, 'AGATA_DIGEST': 'sha256:' + 'c' * 64,
        'MINIAPP_DIGEST': 'sha256:' + 'd' * 64, 'EROS_DIGEST': 'sha256:' + 'e' * 64,
        'DB_CONTAINER_ID': 'e' * 64,
        'REDIS_CONTAINER_ID': 'f' * 64, 'DEPLOYMENT_ID': '7', 'GITHUB_RUN_ID': '123',
        'GITHUB_RUN_ATTEMPT': '1', 'writer_remote': '/run/gpoint-ordinary-manifest-123-1.py',
        'BASELINE_PRISMA_MIGRATIONS': 'migration', 'BASELINE_PRISMA_CHECKSUMS': 'migration=checksum',
        'BASELINE_PRISMA_HEAD': 'migration', 'BASELINE_AGATA_HEAD': 'agata0012',
        'EXPECTED_PRISMA_MIGRATIONS': 'migration', 'EXPECTED_PRISMA_CHECKSUMS': 'migration=checksum',
        'EXPECTED_PRISMA_HEAD': 'migration', 'EXPECTED_AGATA_HEAD': 'agata0012',
        'PENDING_PRISMA_MIGRATIONS': '', 'PENDING_PRISMA_CHECKSUMS': '',
        'EXPECTED_ROLE_IDENTITIES_B64': base64.b64encode(json.dumps({
            'schema': 1, 'candidateSha': 'a' * 40, 'scope': 'FULL',
            'updatedComponents': ['AGATA', 'CORE', 'MINIAPP'],
            'roles': {
                'gpoint-core-api': {'version': 'a' * 40 + '-123-1', 'sourceSha': 'a' * 40, 'repositoryDigest': 'ghcr.io/akhripunov/gpoint-runtime@sha256:' + 'b' * 64, 'baselineImageId': None},
                'gpoint-core-worker': {'version': 'a' * 40 + '-123-1', 'sourceSha': 'a' * 40, 'repositoryDigest': 'ghcr.io/akhripunov/gpoint-runtime@sha256:' + 'b' * 64, 'baselineImageId': None},
                'gpoint-core-bot': {'version': 'a' * 40 + '-123-1', 'sourceSha': 'a' * 40, 'repositoryDigest': 'ghcr.io/akhripunov/gpoint-runtime@sha256:' + 'b' * 64, 'baselineImageId': None},
                'gpoint-agata-api': {'version': 'a' * 40 + '-123-1', 'sourceSha': 'a' * 40, 'repositoryDigest': 'ghcr.io/akhripunov/gpoint-agata-runtime@sha256:' + 'c' * 64, 'baselineImageId': None},
                'gpoint-agata-memory-worker': {'version': 'a' * 40 + '-123-1', 'sourceSha': 'a' * 40, 'repositoryDigest': 'ghcr.io/akhripunov/gpoint-agata-runtime@sha256:' + 'c' * 64, 'baselineImageId': None},
                'gpoint-miniapp-web': {'version': 'a' * 40 + '-123-1', 'sourceSha': 'a' * 40, 'repositoryDigest': 'ghcr.io/akhripunov/gpoint-miniapp@sha256:' + 'd' * 64, 'baselineImageId': None},
            },
        }, separators=(',', ':')).encode()).decode(),
        'BASELINE_ROLE_VERSION': 'b' * 40 + '-100-1', 'BASELINE_ROLE_SOURCE_SHA': 'b' * 40,
        'DB_MOUNTS_SHA256': 'c' * 64, 'REDIS_MOUNTS_SHA256': 'd' * 64,
    }
    arguments = re.findall(
        r'"(\$\{([A-Za-z_][A-Za-z0-9_]*):--\}|\$([A-Za-z_][A-Za-z0-9_]*))"',
        match.group(1),
    )
    remote_args = [
        values[default_name] or '-' if default_name else values[name]
        for _expression, default_name, name in arguments
    ]
    prefix = match.group(2).split('db_mounts_sha=', 1)[0]
    harness = '''
    docker() {
      case "$*" in
        *shared_postgres_1*) if [[ "$*" == *'.Id'* ]]; then printf '%s' "${6}"; else printf true; fi ;;
        *shared_redis_1*) if [[ "$*" == *'.Id'* ]]; then printf '%s' "${7}"; else printf true; fi ;;
        *) return 1 ;;
      esac
    }
    test() { if [[ "$1" == -s ]]; then return 0; fi; builtin test "$@"; }
    '''
    # Mock only Docker inspection and the existing writer file; execute the
    # actual quoted-heredoc assignments and validation with the real SSH args.
    harness = harness.replace('"${6}"', "'" + values['DB_CONTAINER_ID'] + "'")
    harness = harness.replace('"${7}"', "'" + values['REDIS_CONTAINER_ID'] + "'")
    result = subprocess.run(['bash', '-c', harness + prefix, 'verify', *remote_args],
                            env={'PATH': os.environ['PATH']}, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_consumer_process_checks_do_not_require_ps_inside_slim_images():
    workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-native-release.yml').read_text())
    deploy = "\n".join(step.get("run", "") for step in workflow["jobs"]["deploy"]["steps"])
    rollback_workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-operations.yml').read_text())
    rollback = "\n".join(step.get("run", "") for step in rollback_workflow["jobs"]["rollback"]["steps"])
    for role in ['worker', 'bot']:
        assertion = f'docker top "gpoint-core-{role}-$core_version" -eo pid,args | grep -F "apps/{role}/dist/main.js"'
        assert deploy.count(assertion) == 1
        assert rollback.count(assertion) == 1
    assert "sh -ec 'ps aux" not in workflow


def test_proxy_route_probe_drains_docker_output_under_pipefail():
    workflow = (ROOT / '.github/workflows/gpoint-native-release.yml').read_text()
    probe = next(line.strip() for line in workflow.splitlines()
                 if 'docker exec kamal-proxy kamal-proxy list |' in line and 'gpoint-core' in line)
    producer = '''docker() { python3 -c 'import sys; sys.stdout.write("gpoint-core\\n" + "x" * 1048576)'; }; '''
    result = subprocess.run(['bash', '-o', 'pipefail', '-c', producer + probe],
                            env={'PATH': os.environ['PATH']}, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''


def test_runtime_prisma_readbacks_resolve_the_api_workspace_dependencies():
    workflow = (ROOT / '.github/workflows/gpoint-native-release.yml').read_text()
    rollback_text = (ROOT / '.github/workflows/gpoint-operations.yml').read_text()
    calls = [line for line in (workflow + rollback_text).splitlines()
             if 'docker exec' in line and 'require("@prisma/client")' in line]
    assert len(calls) == 3
    assert all('docker exec --workdir /app/apps/api ' in line for line in calls)
