import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("recover_kamal_cutover", ROOT / "scripts/recover-kamal-cutover.py")
RECOVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVER)

SHA = "a" * 40
RUN = "12345"
ATTEMPT = "1"
POSTGRES_DATABASE_FACTS = json.dumps({
    "systemIdentifier": "7654321",
    "serverVersionNum": 160004,
    "databases": [{"name": "gpoint", "owner": "gpoint", "allowConnections": True, "acl": "{=Tc/gpoint}"}],
    "erosRoles": [],
    "erosMemberships": [],
}, separators=(",", ":"))
POSTGRES_DATABASE_FACTS_SHA = hashlib.sha256(POSTGRES_DATABASE_FACTS.encode()).hexdigest()
BASELINE_PRISMA_HEAD = "20261001090000_baseline"
BASELINE_PRISMA_MIGRATIONS = "20260901090000_previous,20261001090000_baseline"
BASELINE_AGATA_HEAD = "agata0012"


def evidence(tmp_path, *, changed=None):
    release_root = tmp_path / "release-logs"
    inventory_path = release_root / SHA / f"kamal-vps-inventory-{RUN}-{ATTEMPT}.json"
    inventory_path.parent.mkdir(parents=True)
    services = ("api", "worker", "bot", "agata-api", "agata-memory-worker",
                "gsales-api", "gsales-agata-api", "gsales-agata-memory-worker")
    inventory = {
        "schema": "gpoint-vps-inventory/v1",
        "containers": [
            {"id": f"{index:064x}", "name": f"compose-{service}-1", "imageId": f"sha256:{index:064x}",
             "composeProject": "compose", "composeService": service,
             "running": service not in {"gsales-api", "gsales-agata-api", "gsales-agata-memory-worker"}}
            for index, service in enumerate(services, 1)
        ],
    }
    inventory["containers"].extend([
        {"id": "9" * 64, "name": "shared_postgres_1", "imageId": "sha256:" + "9" * 64,
         "image": "postgres:16", "composeProject": "compose", "composeService": "postgres", "running": True},
        {"id": "a" * 64, "name": "compose-gpoint-sensitive-detector-1", "imageId": "sha256:" + "a" * 64,
         "image": "gpoint-sensitive-detector:baseline", "composeProject": "compose",
         "composeService": "gpoint-sensitive-detector", "running": True},
    ])
    inventory_raw = json.dumps(inventory, sort_keys=True).encode()
    inventory_path.write_bytes(inventory_raw)
    inventory_path.chmod(0o600)
    rows = {
        "BASELINE_SCHEMA": "gpoint-kamal-cutover-baseline/v1",
        "CANDIDATE_SHA": SHA,
        "RUN_ID": RUN,
        "ATTEMPT": ATTEMPT,
        "BASELINE_CAPTURED_AT_EPOCH": "1793260800",
        "CURRENT_SHA": "b" * 40,
        "POSTGRES_CONTAINER_ID": "9" * 64,
        "POSTGRES_IMAGE_ID": "sha256:" + "9" * 64,
        "POSTGRES_SYSTEM_IDENTIFIER": "7654321",
        "POSTGRES_SERVER_VERSION_NUM": "160004",
        "POSTGRES_ACL_FINGERPRINT": POSTGRES_DATABASE_FACTS_SHA,
        "PRISMA_HEAD": BASELINE_PRISMA_HEAD,
        "PRISMA_MIGRATIONS": BASELINE_PRISMA_MIGRATIONS,
        "AGATA_HEAD": BASELINE_AGATA_HEAD,
        "KAMAL_PROXY_BASELINE": "ABSENT",
        "CADDYFILE_SHA256": "c" * 64,
        "INVENTORY_FILE": str(inventory_path),
        "INVENTORY_SHA256": hashlib.sha256(inventory_raw).hexdigest(),
    }
    rows.update(changed or {})
    baseline = release_root / SHA / f"kamal-cutover-baseline-{RUN}-{ATTEMPT}.txt"
    baseline.write_text("".join(f"{key}={value}\n" for key, value in rows.items()))
    baseline.chmod(0o600)
    return baseline, release_root


def test_baseline_accepts_exact_attempt_and_complete_old_service_ids(tmp_path):
    path, release_root = evidence(tmp_path)
    values, services = RECOVER.read_baseline(path, hashlib.sha256(path.read_bytes()).hexdigest(), SHA, RUN, ATTEMPT, release_root, path.stat().st_uid)
    assert values["KAMAL_PROXY_BASELINE"] == "ABSENT"
    assert set(services) == {"api", "worker", "bot", "agata-api", "agata-memory-worker",
                             "gsales-api", "gsales-agata-api", "gsales-agata-memory-worker",
                             "gpoint-sensitive-detector"}
    assert services["api"]["id"] == f"{1:064x}"


def test_docker_created_rfc3339_nanoseconds_parse_with_timezone():
    created = RECOVER.parse_docker_created("2026-10-31T04:55:12.388673798Z")
    assert created.isoformat() == "2026-10-31T04:55:12.388673+00:00"


@pytest.mark.parametrize("run,attempt,changed", [
    ("12346", ATTEMPT, {}),
    (RUN, "2", {}),
    (RUN, ATTEMPT, {"KAMAL_PROXY_BASELINE": "PRESENT"}),
])
def test_baseline_rejects_other_attempt_or_existing_proxy(tmp_path, run, attempt, changed):
    path, release_root = evidence(tmp_path, changed=changed)
    with pytest.raises(RECOVER.RecoveryBlocked):
        RECOVER.read_baseline(path, hashlib.sha256(path.read_bytes()).hexdigest(), SHA, run, attempt, release_root, path.stat().st_uid)


def test_baseline_rejects_tampering_and_inventory_gap(tmp_path):
    path, release_root = evidence(tmp_path)
    inventory = json.loads(Path(next(line.split("=", 1)[1] for line in path.read_text().splitlines()
                                    if line.startswith("INVENTORY_FILE="))).read_text())
    inventory["containers"].pop()
    inventory_path = Path(next(line.split("=", 1)[1] for line in path.read_text().splitlines()
                               if line.startswith("INVENTORY_FILE=")))
    inventory_path.write_text(json.dumps(inventory))
    with pytest.raises(RECOVER.RecoveryBlocked):
        RECOVER.read_baseline(path, hashlib.sha256(path.read_bytes()).hexdigest(), SHA, RUN, ATTEMPT, release_root, path.stat().st_uid)


@pytest.mark.parametrize("changed", [
    {"POSTGRES_CONTAINER_ID": ""},
    {"POSTGRES_IMAGE_ID": "not-an-image-id"},
    {"POSTGRES_SYSTEM_IDENTIFIER": ""},
    {"POSTGRES_SERVER_VERSION_NUM": "unknown"},
    {"POSTGRES_ACL_FINGERPRINT": "bad"},
    {"PRISMA_HEAD": ""},
    {"PRISMA_MIGRATIONS": ""},
    {"AGATA_HEAD": ""},
])
def test_baseline_requires_postgres_recovery_identity(tmp_path, changed):
    path, release_root = evidence(tmp_path, changed=changed)
    with pytest.raises(RECOVER.RecoveryBlocked):
        RECOVER.read_baseline(path, hashlib.sha256(path.read_bytes()).hexdigest(), SHA, RUN, ATTEMPT, release_root, path.stat().st_uid)


def test_first_cutover_uses_one_tested_server_owned_recovery_owner():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    hard_stop = "ME-54: VPS-side rollback on SSH loss is not yet implemented or proven. Refusing to pause Compose."
    assert hard_stop not in workflow
    assert "- name: Run one server-owned Kamal cutover transaction" in workflow
    assert "run: bash scripts/run-kamal-cutover-owner.sh" in workflow
    assert "- name: Recover legacy GPoint if cutover Verify fails" not in workflow
    assert "proxy_digest: ${{ steps.proxy.outputs.digest }}" in workflow
    assert "docker buildx imagetools inspect basecamp/kamal-proxy:v0.9.2 --format '{{json .Manifest}}'" in workflow
    assert 'imagetools inspect "basecamp/kamal-proxy@$digest"' in workflow
    assert 'docker image tag "$image_id" basecamp/kamal-proxy:v0.9.2' in (ROOT / "scripts/kamal-first-cutover/requests.sh").read_text()
    owner = (ROOT / "scripts/kamal_cutover_owner.py").read_text()
    assert 'PHASES = ("pause", "retire", "requests", "routes", "consumers", "verify")' in owner
    assert 'recovery_name = PRE_RECOVERY if failed_phase == "pause" else POST_RECOVERY' in owner
    assert 'recovery_script = _safe_path(stage, f"phases/{recovery_name}.sh", owner_uid)' in owner
    starter = (ROOT / "scripts/start_kamal_cutover_owner.py").read_text()
    assert '"--service-type=exec"' in starter
    assert '"--property=Restart=no"' in starter
    assert '"--property=KillMode=control-group"' in starter
    assert '"--property=ExecStopPost={cleanup_command}"' in starter
    packager = (ROOT / "scripts/package-kamal-cutover.py").read_text()
    assert "PHASES = ('pause', 'retire', 'requests', 'routes', 'consumers', 'verify'," in packager
    assert "source(root/'scripts/kamal-first-cutover'/f'{phase}.sh', f'phases/{phase}.sh')" in packager
    assert "GPOINT_KAMAL_OWNER=COMPLETE" in owner
    assert "GPOINT_KAMAL_OWNER=RECOVERED" in owner


def test_hosted_docker_recovery_integration_is_wired_after_immutable_build():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    build = workflow.split("  deploy:\n", 1)[0]
    step = "Exercise isolated Kamal cutover and exact late-failure recovery"
    assert step in build
    assert build.index("Read back exact published images") < build.index(step)
    assert "tests/integration/kamal-cutover-recovery-docker.py" in build
    assert "--proxy-digest \"$PROXY_DIGEST\"" in build
    integration = ROOT / "tests/integration/kamal-cutover-recovery-docker.py"
    source = integration.read_text()
    assert "RECOVER.recover(args)" in source
    assert "KAMAL_CUTOVER_LATE_FAILURE_RECOVERY=PASS" in source
    assert '"--tls", "--tls-certificate-path=/tmp/gpoint-proxy.crt"' in source
    assert '"--tls-private-key-path=/tmp/gpoint-proxy.key"' in source
    assert "https://gpoint.amiora.one/api/health/live" in source
    assert '"-p"' not in source and '"--publish"' not in source
    assert "POSTGRES_DATABASE_SQL" in source and "POSTGRES_MIGRATION_SQL" in source
    assert "BEGIN READ ONLY;" in source and '"POSTGRES_ACL_FINGERPRINT"' in source
    assert '"composeService": "gpoint-sensitive-detector"' in source
    assert '"shared_postgres_1"' in source


def test_integration_accepts_built_digests_before_docker_check(monkeypatch):
    integration_path = ROOT / "tests/integration/kamal-cutover-recovery-docker.py"
    spec = importlib.util.spec_from_file_location("kamal_cutover_recovery_docker", integration_path)
    integration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(integration)
    values = ["sha256:" + char * 64 for char in "abcd"]
    monkeypatch.setattr(sys, "argv", ["integration", "--app-digest", values[0],
                                     "--agata-digest", values[1], "--miniapp-digest", values[2],
                                     "--proxy-digest", values[3]])
    monkeypatch.setattr(integration.shutil, "which", lambda _: None)
    with pytest.raises(SystemExit, match="Docker CLI is required"):
        integration.main()
    assert list(integration.DIGESTS.values()) == values[:3]


def test_integration_allows_absent_kamal_network_without_removing_existing(monkeypatch):
    integration_path = ROOT / "tests/integration/kamal-cutover-recovery-docker.py"
    spec = importlib.util.spec_from_file_location("kamal_cutover_recovery_docker", integration_path)
    integration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(integration)
    values = ["sha256:" + char * 64 for char in "abcd"]
    monkeypatch.setattr(sys, "argv", ["integration", "--app-digest", values[0],
                                     "--agata-digest", values[1], "--miniapp-digest", values[2],
                                     "--proxy-digest", values[3]])
    monkeypatch.setattr(integration.shutil, "which", lambda _: "/usr/bin/docker")

    def fake_docker(*args, **kwargs):
        if args == ("info",):
            return ""
        if args[:2] == ("network", "ls"):
            return ""
        if args[0] == "ps":
            raise RuntimeError("reached container guard")
        raise AssertionError(args)

    monkeypatch.setattr(integration, "docker", fake_docker)
    with pytest.raises(RuntimeError, match="reached container guard"):
        integration.main()


def test_integration_refuses_existing_kamal_network(monkeypatch):
    integration_path = ROOT / "tests/integration/kamal-cutover-recovery-docker.py"
    spec = importlib.util.spec_from_file_location("kamal_cutover_recovery_docker", integration_path)
    integration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(integration)
    value = "sha256:" + "a" * 64
    monkeypatch.setattr(sys, "argv", ["integration", "--app-digest", value,
                                     "--agata-digest", value, "--miniapp-digest", value,
                                     "--proxy-digest", value])
    monkeypatch.setattr(integration.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(integration, "docker", lambda *args, **kwargs: "kamal\n" if args[:2] == ("network", "ls") else "")
    with pytest.raises(SystemExit, match="refusing to touch an existing Kamal network"):
        integration.main()


def test_integration_docker_network_listing_failure_is_fatal(monkeypatch):
    integration_path = ROOT / "tests/integration/kamal-cutover-recovery-docker.py"
    spec = importlib.util.spec_from_file_location("kamal_cutover_recovery_docker", integration_path)
    integration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(integration)
    monkeypatch.setattr(integration.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 1, stdout="", stderr="daemon unavailable"))
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        integration.docker("network", "ls", "--format", "{{.Name}}")


def test_python_c_probe_argv_offsets_and_exact_container_address(monkeypatch, capsys):
    import http.client
    import socket
    import ssl

    path = ROOT / "tests/integration/kamal-cutover-recovery-docker.py"
    spec = importlib.util.spec_from_file_location("cutover_direct_probe", path)
    integration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(integration)
    calls = {}

    def capture_connection(address, timeout=None, source_address=None):
        calls["destination"] = address
        calls["socket_timeout"] = timeout
        return object()

    class Response:
        status = 200

        def read(self):
            return b"legacy:/health/live"

    class Connection:
        def __init__(self, hostname, port, timeout, context):
            calls["host"] = hostname
            calls["port"] = port
            calls["timeout"] = timeout
            self._create_connection = None

        def request(self, method, path, headers):
            calls["request"] = (method, path, headers)
            self._create_connection((calls["host"], calls["port"]), timeout=4)

        def getresponse(self):
            return Response()

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(socket, "create_connection", capture_connection)
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(ssl, "_create_unverified_context", lambda: object())
    args = integration.python_probe_args("https://gpoint.amiora.one/api/health/live?q=1",
                                         "legacy:/health/live", "gpoint.amiora.one", "172.25.0.7")
    monkeypatch.setattr(sys, "argv", ["-c", *args[2:]])
    exec(integration.PROBE_CODE, {})
    assert capsys.readouterr().out.strip() == "legacy:/health/live"
    assert calls["destination"] == ("172.25.0.7", 443)
    assert calls["host"] == "gpoint.amiora.one"
    assert calls["request"] == ("GET", "/api/health/live?q=1", {"Host": "gpoint.amiora.one"})
    assert calls["timeout"] == calls["socket_timeout"] == 4
    assert calls["closed"]


def test_probe_ignores_ambient_https_proxy_and_requires_literal_container_ip():
    path = ROOT / "tests/integration/kamal-cutover-recovery-docker.py"
    spec = importlib.util.spec_from_file_location("cutover_proxy_probe", path)
    integration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(integration)
    code = """import socket, sys
class Destination(Exception): pass
def capture(address, timeout=None, source_address=None): raise Destination(address)
socket.create_connection = capture
probe_code = sys.argv[1]
sys.argv = ['-c', *sys.argv[2:]]
try:
    exec(probe_code, {'__name__': '__main__'})
except Destination as e:
    print('destination=' + repr(e.args[0]))
"""
    env = {**os.environ, "https_proxy": "http://proxy.test:8123", "no_proxy": ""}
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    result = subprocess.run([sys.executable, "-c", code, integration.PROBE_CODE,
                             "https://gpoint.amiora.one/test", "fixture", "gpoint.amiora.one", "127.0.0.1"],
                            env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "destination=('127.0.0.1', 443)"
    for url, host, address in [
        ("https://gpoint.amiora.one/test", "unrelated.example", "127.0.0.1"),
        ("http://gpoint.amiora.one/test", "gpoint.amiora.one", "127.0.0.1"),
        ("https://gpoint.amiora.one/test", "gpoint.amiora.one", "not-an-ip"),
    ]:
        bad = subprocess.run([sys.executable, "-c", integration.PROBE_CODE,
                              url, "fixture", host, address],
                             env=env, text=True, capture_output=True, timeout=10)
        assert bad.returncode != 0


def test_staged_remote_layout_imports_sibling_route_module(tmp_path):
    stage = tmp_path / "gpoint-kamal-recovery-12345-1"
    stage.mkdir(mode=0o700)
    helper = stage / "recover-kamal-cutover.py"
    route_helper = stage / "kamal_proxy_routes.py"
    shutil.copyfile(ROOT / "scripts/recover-kamal-cutover.py", helper)
    shutil.copyfile(ROOT / "scripts/kamal_proxy_routes.py", route_helper)
    helper.chmod(0o600)
    route_helper.chmod(0o600)
    result = subprocess.run([sys.executable, "-B", str(helper), "--help"], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "--proxy-digest" in result.stdout


def test_wrong_proxy_image_stops_before_any_recovery_mutation(tmp_path, monkeypatch):
    candidate = "a" * 40
    app_source = "b" * 40
    run_id, attempt = "12345", "1"
    version = f"{candidate}-{run_id}-{attempt}"
    caddy_content = b"known caddy config"
    caddy_sha = hashlib.sha256(caddy_content).hexdigest()
    recovery = RECOVER.Path("/opt/gpoint/backups/caddy-before-kamal-" + candidate + "-" + run_id + "-" + attempt)
    checksum = RECOVER.Path(str(recovery) + ".sha256")
    real_stat = Path.stat
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(RECOVER.Path, "is_file", lambda self: str(self) in {str(recovery), str(checksum)})
    def fake_stat(self, *, follow_symlinks=True):
        if str(self) in {str(recovery), str(checksum)}:
            return SimpleNamespace(st_uid=os.getuid(), st_mode=stat.S_IFREG | 0o600)
        try:
            return real_stat(self, follow_symlinks=follow_symlinks)
        except TypeError as exc:
            # Python < 3.10 does not expose Path.stat(follow_symlinks=...).
            if "follow_symlinks" not in str(exc):
                raise
            return real_stat(self)

    monkeypatch.setattr(RECOVER.Path, "stat", fake_stat)
    monkeypatch.setattr(RECOVER.Path, "read_text", lambda self, **kwargs: f"{caddy_sha}  {recovery}\n"
                        if self == checksum else real_read_text(self, **kwargs))
    monkeypatch.setattr(RECOVER.Path, "read_bytes", lambda self: caddy_content if self == recovery else real_read_bytes(self))
    proxy_id = "f" * 64
    services = ("api", "worker", "bot", "agata-api", "agata-memory-worker", "gsales-api", "gsales-agata-api", "gsales-agata-memory-worker")
    mocked_legacy = {service: {"id": f"{i:064x}", "imageId": f"sha256:{i:064x}", "running": not service.startswith("gsales")}
                     for i, service in enumerate(services, 1)}
    detector_id, postgres_id, postgres_image = "e" * 64, "9" * 64, "sha256:" + "9" * 64
    mocked_legacy["gpoint-sensitive-detector"] = {
        "id": detector_id, "imageId": "sha256:" + "e" * 64,
        "image": "gpoint-sensitive-detector:baseline", "running": True,
    }
    monkeypatch.setattr(RECOVER, "read_baseline", lambda *args, **kwargs: (
        {"CURRENT_SHA": app_source, "CADDYFILE_SHA256": caddy_sha,
         "BASELINE_CAPTURED_AT_EPOCH": "1793260800", "POSTGRES_CONTAINER_ID": postgres_id,
         "POSTGRES_IMAGE_ID": postgres_image, "POSTGRES_SYSTEM_IDENTIFIER": "7654321",
         "POSTGRES_SERVER_VERSION_NUM": "160004", "POSTGRES_ACL_FINGERPRINT": POSTGRES_DATABASE_FACTS_SHA,
         "PRISMA_HEAD": BASELINE_PRISMA_HEAD, "PRISMA_MIGRATIONS": BASELINE_PRISMA_MIGRATIONS,
         "AGATA_HEAD": BASELINE_AGATA_HEAD}, mocked_legacy))
    mutations = []

    def fake_command(argv, *, input_text=None, check=True):
        if argv[:5] == ["docker", "exec", "-i", "shared_postgres_1", "sh"]:
            if "pg_control_system" in (input_text or ""):
                return POSTGRES_DATABASE_FACTS
            if "_prisma_migrations" in (input_text or ""):
                return json.dumps({"prismaHead": BASELINE_PRISMA_HEAD,
                                   "prismaMigrations": BASELINE_PRISMA_MIGRATIONS.split(",")}, separators=(",", ":"))
            if "agata.alembic_version" in (input_text or ""):
                return BASELINE_AGATA_HEAD
        if argv[:2] == ["sha256sum", "/etc/caddy/Caddyfile"]:
            return f"{caddy_sha}  /etc/caddy/Caddyfile"
        if argv == ["docker", "ps", "-aq", "--no-trunc"]:
            return ""
        if argv[:3] == ["docker", "ps", "-aq"]:
            if "name=^/gpoint-" in argv:
                return ""
            return proxy_id if "name=^/kamal-proxy$" in argv else ""
        if argv[:2] == ["docker", "inspect"]:
            target = argv[2]
            if target == proxy_id:
                return json.dumps([{"Id": proxy_id, "Name": "/kamal-proxy",
                                    "Config": {"Image": "unexpected/proxy:tag"},
                                    "Created": "2026-10-29T00:00:00Z", "State": {"Running": True}}])
            if target == "shared_postgres_1":
                return json.dumps([{"Id": postgres_id, "Image": postgres_image, "State": {"Running": True}}])
            if target == detector_id:
                return json.dumps([{"Id": detector_id, "Image": "sha256:" + "e" * 64,
                                    "Config": {"Image": "gpoint-sensitive-detector:baseline",
                                               "Labels": {"com.docker.compose.project": "compose",
                                                          "com.docker.compose.service": "gpoint-sensitive-detector"}},
                                    "State": {"Running": True}}])
            service = next((name for name, row in mocked_legacy.items() if row["id"] == target), "")
            return json.dumps([{"Id": target, "Image": "sha256:" + target,
                                "Config": {"Labels": {"com.docker.compose.project": "compose", "com.docker.compose.service": service}},
                                "State": {"Running": not service.startswith("gsales")}}])
        if argv[:2] == ["docker", "exec"] and argv[-2:] and argv[-1] in {"remove", "list"}:
            mutations.append(argv)
        return ""

    def fake_run(argv, **kwargs):
        if argv[:3] == ["systemctl", "is-active", "--quiet"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(RECOVER, "command", fake_command)
    monkeypatch.setattr(RECOVER.subprocess, "run", fake_run)
    args = SimpleNamespace(candidate_sha=candidate, app_source_sha=app_source, version=version,
                           run_id=run_id, attempt=attempt,
                           baseline=f"/opt/gpoint/release-logs/{candidate}/kamal-cutover-baseline-{run_id}-{attempt}.txt",
                           baseline_sha="d" * 64,
                           app_digest="sha256:" + "1" * 64, agata_digest="sha256:" + "2" * 64,
                           miniapp_digest="sha256:" + "3" * 64, proxy_digest="sha256:" + "6" * 64)
    with pytest.raises(RECOVER.RecoveryBlocked, match="Kamal Proxy image reference is unexpected"):
        RECOVER.recover(args)
    assert mutations == []


@pytest.mark.parametrize("failure_stage", ["route_remove", "route_remove_timeout", "route_list", "route_list_post", "proxy_stop", "first_legacy_readiness", "foreign_attempt", "postgres_identity", "detector_identity", "acl_changed", "prisma_head_changed", "prisma_inventory_changed", "agata_head_changed", "precaddy_no_kamal"])
def test_failure_stages_attempt_safe_legacy_recovery_and_preserve_gsales(tmp_path, monkeypatch, failure_stage):
    candidate = "a" * 40
    app_source = "b" * 40
    run_id, attempt = "12345", "1"
    version = f"{candidate}-{run_id}-{attempt}"
    caddy_content = b"known caddy config"
    caddy_sha = hashlib.sha256(caddy_content).hexdigest()
    recovery = RECOVER.Path("/opt/gpoint/backups/caddy-before-kamal-" + candidate + "-" + run_id + "-" + attempt)
    checksum = RECOVER.Path(str(recovery) + ".sha256")
    real_stat, real_read_text, real_read_bytes = Path.stat, Path.read_text, Path.read_bytes
    monkeypatch.setattr(RECOVER.Path, "is_file", lambda self: str(self) in {str(recovery), str(checksum)})
    def fake_stat(self, *, follow_symlinks=True):
        if str(self) in {str(recovery), str(checksum)}:
            return SimpleNamespace(st_uid=os.getuid(), st_mode=stat.S_IFREG | 0o600)
        try:
            return real_stat(self, follow_symlinks=follow_symlinks)
        except TypeError as exc:
            # Python < 3.10 does not expose Path.stat(follow_symlinks=...).
            if "follow_symlinks" not in str(exc):
                raise
            return real_stat(self)

    monkeypatch.setattr(RECOVER.Path, "stat", fake_stat)
    monkeypatch.setattr(RECOVER.Path, "read_text", lambda self, **kwargs: f"{caddy_sha}  {recovery}\n"
                        if self == checksum else real_read_text(self, **kwargs))
    monkeypatch.setattr(RECOVER.Path, "read_bytes", lambda self: caddy_content if self == recovery else real_read_bytes(self))
    legacy = {service: {"id": f"{index:064x}", "imageId": f"sha256:{index:064x}",
                        "running": True, "composeProject": "compose", "composeService": service}
              for index, service in enumerate(("api", "worker", "bot", "agata-api", "agata-memory-worker"), 1)}
    legacy.update({service: {"id": f"{index:064x}", "imageId": f"sha256:{index:064x}",
                            "running": False, "composeProject": "compose", "composeService": service}
                   for index, service in enumerate(("gsales-api", "gsales-agata-api", "gsales-agata-memory-worker"), 10)})
    postgres_id, postgres_image = "9" * 64, "sha256:" + "9" * 64
    detector_id = "e" * 64
    legacy["gpoint-sensitive-detector"] = {
        "id": detector_id, "imageId": "sha256:" + detector_id,
        "image": "gpoint-sensitive-detector:baseline", "running": True,
    }
    monkeypatch.setattr(RECOVER, "read_baseline", lambda *args, **kwargs: (
        {"CURRENT_SHA": app_source, "CADDYFILE_SHA256": caddy_sha, "BASELINE_CAPTURED_AT_EPOCH": "1793260800",
         "POSTGRES_CONTAINER_ID": postgres_id, "POSTGRES_IMAGE_ID": postgres_image,
         "POSTGRES_SYSTEM_IDENTIFIER": "7654321", "POSTGRES_SERVER_VERSION_NUM": "160004",
         "POSTGRES_ACL_FINGERPRINT": POSTGRES_DATABASE_FACTS_SHA,
         "PRISMA_HEAD": BASELINE_PRISMA_HEAD, "PRISMA_MIGRATIONS": BASELINE_PRISMA_MIGRATIONS,
         "AGATA_HEAD": BASELINE_AGATA_HEAD}, legacy))

    proxy_id, api_id, app_id, proxy_image_id = "f" * 64, "1" * 64, "2" * 64, "4" * 64
    api_digest = "sha256:" + "3" * 64
    app_image = "ghcr.io/akhripunov/gpoint-runtime"
    route_table = f"Service Host Path Target State TLS\ngpoint-core gpoint.amiora.one /api {api_id[:12]}:3000 running yes\n"
    mutations = []
    proxy_running = failure_stage != "precaddy_no_kamal"
    stopped = {row["id"] for name, row in legacy.items() if not name.startswith("gsales")}
    caddy_active = failure_stage == "precaddy_no_kamal"
    routes_removed = False
    route_list_calls = 0

    def fake_command(argv, *, input_text=None, check=True):
        nonlocal proxy_running, caddy_active, route_list_calls
        if argv[:5] == ["docker", "exec", "-i", "shared_postgres_1", "sh"]:
            if "pg_control_system" in (input_text or ""):
                facts = json.loads(POSTGRES_DATABASE_FACTS)
                if failure_stage == "acl_changed":
                    facts["databases"][0]["acl"] = "{=CTc/gpoint}"
                return json.dumps(facts, separators=(",", ":"))
            if "_prisma_migrations" in (input_text or ""):
                head = "20261001999999_unexpected" if failure_stage == "prisma_head_changed" else BASELINE_PRISMA_HEAD
                previous = "20260902090000_unexpected" if failure_stage == "prisma_inventory_changed" else "20260901090000_previous"
                migrations = [previous, head]
                return json.dumps({"prismaHead": head, "prismaMigrations": migrations}, separators=(",", ":"))
            if "agata.alembic_version" in (input_text or ""):
                return "agata9999" if failure_stage == "agata_head_changed" else BASELINE_AGATA_HEAD
        if argv[:2] == ["sha256sum", "/etc/caddy/Caddyfile"]:
            return f"{caddy_sha}  /etc/caddy/Caddyfile"
        if argv == ["docker", "ps", "-aq", "--no-trunc"]:
            if failure_stage == "precaddy_no_kamal":
                return ""
            return api_id + ("\n" + "d" * 64 if failure_stage == "foreign_attempt" else "")
        if argv[:3] == ["docker", "ps", "-aq"]:
            if "name=^/kamal-proxy$" in argv:
                return "" if failure_stage == "precaddy_no_kamal" else proxy_id
            if "name=^/gpoint-" in argv:
                return api_id + ("\n" + "d" * 64 if failure_stage == "foreign_attempt" else "")
            if f"name=^/gpoint-core-api-{version}$" in argv:
                return api_id
            return ""
        if argv[:2] == ["docker", "inspect"]:
            target = argv[2]
            if target == proxy_id:
                return json.dumps([{"Id": proxy_id, "Name": "/kamal-proxy",
                                   "Image": proxy_image_id,
                                   "Config": {"Image": "basecamp/kamal-proxy:v0.9.2"},
                                   "Created": "2026-10-29T09:00:00Z", "State": {"Running": proxy_running}}])
            if target == "shared_postgres_1":
                live_id = "8" * 64 if failure_stage == "postgres_identity" else postgres_id
                return json.dumps([{"Id": live_id, "Image": postgres_image, "State": {"Running": True}}])
            if target == detector_id:
                return json.dumps([{"Id": detector_id, "Image": "sha256:" + detector_id,
                                    "Config": {"Image": "gpoint-sensitive-detector:changed" if failure_stage == "detector_identity" else "gpoint-sensitive-detector:baseline",
                                               "Labels": {"com.docker.compose.project": "compose",
                                                          "com.docker.compose.service": "gpoint-sensitive-detector"}},
                                    "State": {"Running": True}}])
            if target == "d" * 64:
                return json.dumps([{"Id": target, "Name": "/gpoint-core-api-" + "c" * 40 + "-12345-1",
                                    "Image": app_id, "Config": {"Labels": {"org.opencontainers.image.revision": app_source}},
                                    "State": {"Running": True}}])
            if target == proxy_image_id:
                return json.dumps([{"Id": proxy_image_id, "RepoDigests": ["basecamp/kamal-proxy@sha256:" + "6" * 64]}])
            if target == api_id:
                return json.dumps([{"Id": api_id, "Name": f"/gpoint-core-api-{version}", "Image": app_id,
                                   "Config": {"Labels": {"org.opencontainers.image.revision": app_source}},
                                   "State": {"Running": api_id not in stopped}}])
            if target == app_id:
                return json.dumps([{"Id": app_id, "RepoDigests": [f"{app_image}@{api_digest}"],
                                   "Config": {"Labels": {"org.opencontainers.image.revision": app_source}}}])
        if argv[:3] == ["docker", "network", "inspect"]:
            return json.dumps([{"Name": "kamal", "Containers": {
                proxy_id: {"Name": "kamal-proxy", "ContainerID": proxy_id},
                api_id: {"Name": f"gpoint-core-api-{version}", "ContainerID": api_id},
            }}])
        if argv[:2] == ["docker", "exec"] and argv[-1] == "list":
            route_list_calls += 1
            if failure_stage == "route_list":
                raise RECOVER.RecoveryBlocked("injected route-list failure")
            if failure_stage == "route_list_post" and route_list_calls == 2:
                raise RECOVER.RecoveryBlocked("injected post-remove route-list failure")
            return "Service Host Path Target State TLS\n" if routes_removed else route_table
        if argv[:2] == ["docker", "inspect"] and argv[2] not in {proxy_id, proxy_image_id, api_id, app_id}:
            cid = argv[2]
            compose_service = next((s for s, row in legacy.items() if row["id"] == cid), "")
            return json.dumps([{"Id": cid, "Image": "sha256:" + cid,
                               "Config": {"Labels": {"com.docker.compose.project": "compose",
                                                         "com.docker.compose.service": compose_service}},
                               "State": {"Running": compose_service not in {"gsales-api", "gsales-agata-api", "gsales-agata-memory-worker"} and cid not in stopped}}])
        if argv[:2] == ["docker", "stop"]:
            mutations.append(tuple(argv))
            stopped.add(argv[-1])
            if argv[-1] == proxy_id:
                proxy_running = False
        if argv[:2] == ["docker", "exec"] and argv[-1] == "list":
            return "Service Host Path Target State TLS\n"
        if argv[:2] == ["docker", "start"] or argv[:2] == ["systemctl", "start"]:
            mutations.append(tuple(argv))
            if argv[:2] == ["docker", "start"]:
                stopped.discard(argv[-1])
            else:
                if proxy_running:
                    raise RECOVER.RecoveryBlocked("injected Caddy port conflict")
                caddy_active = True
        if argv[:2] == ["curl", "--fail"]:
            return "ok"
        return ""

    def fake_run(argv, **kwargs):
        nonlocal proxy_running, caddy_active, routes_removed
        if argv[:2] == ["docker", "exec"] and argv[-2:] == ["remove", "gpoint-core"]:
            mutations.append(tuple(argv))
            if failure_stage == "route_remove":
                return SimpleNamespace(returncode=1, stdout="", stderr="injected route removal failure")
            if failure_stage == "route_remove_timeout":
                raise RECOVER.subprocess.TimeoutExpired(argv, 60)
            routes_removed = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:2] == ["docker", "stop"]:
            mutations.append(tuple(argv))
            if argv[-1] == proxy_id and failure_stage == "proxy_stop":
                return SimpleNamespace(returncode=1, stdout="", stderr="injected proxy stop failure")
            stopped.add(argv[-1])
            if argv[-1] == proxy_id:
                proxy_running = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:2] == ["docker", "kill"]:
            mutations.append(tuple(argv))
            if failure_stage != "proxy_stop":
                proxy_running = False
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="injected proxy kill failure")
        if argv[:3] == ["systemctl", "is-active", "--quiet"]:
            return SimpleNamespace(returncode=0 if caddy_active else 3, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(RECOVER, "command", fake_command)
    monkeypatch.setattr(RECOVER.subprocess, "run", fake_run)
    ready_calls = []
    def fake_wait_ready(cid, *, timeout=90):
        ready_calls.append(cid)
        if failure_stage == "first_legacy_readiness" and len(ready_calls) == 1:
            raise RECOVER.RecoveryBlocked("injected readiness failure")
    monkeypatch.setattr(RECOVER, "wait_container_ready", fake_wait_ready)
    args = SimpleNamespace(candidate_sha=candidate, app_source_sha=app_source, version=version,
                           run_id=run_id, attempt=attempt,
                           baseline=f"/opt/gpoint/release-logs/{candidate}/kamal-cutover-baseline-{run_id}-{attempt}.txt",
                           baseline_sha="d" * 64, app_digest=api_digest,
                           agata_digest="sha256:" + "4" * 64, miniapp_digest="sha256:" + "5" * 64,
                           proxy_digest="sha256:" + "6" * 64)
    expected_error = {
        "route_remove": "Kamal Proxy route removal failed",
        "route_remove_timeout": "Kamal Proxy route removal failed",
        "route_list": "injected route-list failure",
        "route_list_post": "Kamal Proxy route removal failed",
        "proxy_stop": "Kamal Proxy graceful stop failed",
        "first_legacy_readiness": "baseline agata-api did not become ready",
        "foreign_attempt": "foreign or unknown GPoint Kamal container is present",
        "postgres_identity": "shared PostgreSQL identity or running state changed during recovery",
        "detector_identity": "SensitiveDetector identity or state changed during recovery",
        "acl_changed": "PostgreSQL ACL/database facts differ from recovery baseline",
        "prisma_head_changed": "PostgreSQL Prisma migration inventory differs from recovery baseline",
        "prisma_inventory_changed": "PostgreSQL Prisma migration inventory differs from recovery baseline",
        "agata_head_changed": "PostgreSQL AGATA migration head differs from recovery baseline",
        "precaddy_no_kamal": None,
    }[failure_stage]
    if expected_error is None:
        RECOVER.recover(args)
        assert all(any(item[:2] == ("docker", "start") and item[-1] == legacy[service]["id"] for item in mutations)
                   for service in ("api", "worker", "bot", "agata-api", "agata-memory-worker"))
        assert not any(item[:2] in {("docker", "stop"), ("docker", "kill")} for item in mutations)
        assert not any(item[:2] == ("docker", "start") and item[-1] in {row["id"] for name, row in legacy.items() if name.startswith("gsales")} for item in mutations)
        return
    with pytest.raises(RECOVER.RecoveryBlocked, match=expected_error):
        RECOVER.recover(args)
    if failure_stage in {"foreign_attempt", "postgres_identity", "detector_identity", "acl_changed", "prisma_head_changed", "prisma_inventory_changed", "agata_head_changed"}:
        assert not any(item[:2] in {("docker", "start"), ("docker", "stop"), ("docker", "kill"), ("systemctl", "start")} for item in mutations)
        return
    assert ("docker", "exec", proxy_id, "kamal-proxy", "remove", "gpoint-core") in mutations if failure_stage not in {"route_list"} else not any(item[:2] == ("docker", "exec") and item[-2:-1] == ("remove",) for item in mutations)
    if failure_stage in {"route_remove", "route_remove_timeout", "route_list_post", "proxy_stop"}:
        assert ("docker", "stop", "--time", "30", proxy_id) in mutations
    if failure_stage in {"route_list", "proxy_stop"}:
        # Unknown routes or an unstoppable proxy must never re-enable legacy
        # consumers alongside retained Kamal containers.
        assert not any(item[:2] in {("docker", "start"), ("systemctl", "start")} for item in mutations)
        return
    assert all(any(item[:2] == ("docker", "start") and item[-1] == legacy[service]["id"] for item in mutations)
               for service in ("api", "worker", "bot", "agata-api", "agata-memory-worker"))
    assert not any(item[:2] == ("docker", "start") and item[-1] in {row["id"] for name, row in legacy.items() if name.startswith("gsales")} for item in mutations)
    assert all(row["id"] not in stopped for name, row in legacy.items() if name.startswith("gsales"))
    if failure_stage == "route_remove":
        assert ("systemctl", "start", "caddy") in mutations
