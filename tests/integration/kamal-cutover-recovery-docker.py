#!/usr/bin/env python3
"""Hosted-only, isolated real-Docker exercise of the production recovery helper.

The only substituted operations are host systemd/Caddy-file paths and the
OCI registry read-back for local fixtures. Proxy routing, Caddy TLS, Docker
identity, container stop/start, and HTTP traffic use real containers.
"""
from __future__ import annotations

import hashlib
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("recover_kamal_cutover", ROOT / "scripts/recover-kamal-cutover.py")
RECOVER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(RECOVER)

SHA = "a" * 40
APP_SHA = "b" * 40
RUN_ID = str(os.getpid() + 10000)
ATTEMPT = "1"
VERSION = f"{SHA}-{RUN_ID}-{ATTEMPT}"
PROXY_REPO = "basecamp/kamal-proxy"
PYTHON_IMAGE = "python:3.12.8-alpine"
CADDY_IMAGE = "caddy:2.9.1-alpine"
CORE_REPO = "ghcr.io/akhripunov/gpoint-runtime"
MINI_REPO = "ghcr.io/akhripunov/gpoint-miniapp"
AGATA_REPO = "ghcr.io/akhripunov/gpoint-agata-runtime"
DIGESTS = {"gpoint-core": "sha256:" + "1" * 64,
           "gpoint-agata": "sha256:" + "2" * 64,
           "gpoint-miniapp": "sha256:" + "3" * 64}


def run(argv: list[str], *, capture: bool = True, check: bool = True) -> str:
    p = subprocess.run(argv, text=True, capture_output=capture, check=False, timeout=90)
    if check and p.returncode:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(argv[:3])}: {p.stderr[-1000:] if p.stderr else ''}")
    return p.stdout.strip() if capture else ""


def docker(*args: str, check: bool = True) -> str:
    return run(["docker", *args], check=check)


def ensure_image(image: str) -> None:
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
        docker("pull", image)


PROBE_CODE = ("import socket,ssl,sys,urllib.request; "
              "old=socket.getaddrinfo; socket.getaddrinfo=lambda h,p,*a,**k: old(sys.argv[4] if h=='gpoint.amiora.one' and sys.argv[4] else h,p,*a,**k); "
              "ctx=ssl._create_unverified_context(); "
              "r=urllib.request.Request(sys.argv[1],headers={'Host':sys.argv[3]} if sys.argv[3] else {}); "
              "print(urllib.request.urlopen(r,context=ctx,timeout=4).read().decode())")


def python_probe_args(url: str, expected: str, host: str, resolve_ip: str) -> list[str]:
    """Python argv after executable; note `-c` is sys.argv[0]."""
    return ["-c", PROBE_CODE, url, expected, host, resolve_ip]


def wait_http(client: str, url: str, expected: str, *, host: str = "", resolve_ip: str = "") -> None:
    last = ""
    for _ in range(30):
        p = subprocess.run(["docker", "exec", client, "python", *python_probe_args(url, expected, host, resolve_ip)],
                           text=True, capture_output=True, timeout=15)
        if p.returncode == 0 and p.stdout.strip() == expected:
            return
        last = p.stderr[-500:] or p.stdout[-500:]
        time.sleep(1)
    raise RuntimeError(f"HTTP probe mismatch: {url} -> {last!r}, expected {expected!r}")


def serve_code(label: str, port: int = 80) -> str:
    return ("from http.server import BaseHTTPRequestHandler,HTTPServer\n"
            f"LABEL={label!r}\n"
            "class H(BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  self.send_response(200); self.end_headers(); self.wfile.write((LABEL+':'+self.path).encode())\n"
            f"HTTPServer(('0.0.0.0',{port}),H).serve_forever()\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-digest", required=True)
    parser.add_argument("--agata-digest", required=True)
    parser.add_argument("--miniapp-digest", required=True)
    parser.add_argument("--proxy-digest", required=True)
    inputs = parser.parse_args()
    digest_values = (inputs.app_digest, inputs.agata_digest, inputs.miniapp_digest, inputs.proxy_digest)
    if any(not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in digest_values):
        raise SystemExit("all image digests must be immutable sha256 values")
    DIGESTS["gpoint-core"] = inputs.app_digest
    DIGESTS["gpoint-agata"] = inputs.agata_digest
    DIGESTS["gpoint-miniapp"] = inputs.miniapp_digest
    proxy_digest = inputs.proxy_digest
    if not shutil.which("docker"):
        raise SystemExit("Docker CLI is required")
    docker("info", check=True)
    # Production recovery proves the public ingress targets these two names.
    # Containers are reachable only on private Docker networks; no host ports.
    suffix = f"{os.getpid()}-{int(time.time())}"
    test_net = f"gpoint-recovery-{suffix}"
    container_ids: list[str] = []
    networks: list[str] = []
    owned_image_refs: list[str] = []
    fixture_tags: list[str] = []
    if docker("network", "inspect", "kamal", check=False):
        raise SystemExit("refusing to touch an existing Kamal network")
    if docker("ps", "-aq", "--filter", "name=^/kamal-proxy$", check=False):
        raise SystemExit("refusing to touch an existing kamal-proxy container")
    proxy_tag = f"{PROXY_REPO}:v0.9.2"
    proxy_digest_ref = f"{PROXY_REPO}@{proxy_digest}"

    def ensure_owned_image(image: str) -> None:
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
            docker("pull", image)
            owned_image_refs.append(image)

    def start(name: str, image: str, net: str, *, command: list[str] | None = None,
              aliases: tuple[str, ...] = (), labels: dict[str, str] | None = None,
              entrypoint: str | None = None, mounts: tuple[tuple[Path, str], ...] = ()) -> str:
        args = ["run", "-d", "--name", name, "--network", net]
        for alias in aliases:
            args += ["--network-alias", alias]
        for key, value in (labels or {}).items():
            args += ["--label", f"{key}={value}"]
        for source, target in mounts:
            args += ["--mount", f"type=bind,src={source},dst={target},readonly"]
        if entrypoint:
            args += ["--entrypoint", entrypoint]
        args.append(image)
        if command:
            args.extend(command)
        cid = docker(*args)
        container_ids.append(cid)
        return cid

    try:
        if subprocess.run(["docker", "image", "inspect", proxy_tag], capture_output=True).returncode == 0:
            raise SystemExit("refusing to overwrite an existing Kamal Proxy v0.9.2 tag")
        ensure_owned_image(PYTHON_IMAGE)
        ensure_owned_image(CADDY_IMAGE)
        ensure_owned_image(proxy_digest_ref)
        docker("image", "tag", docker("image", "inspect", "--format", "{{.Id}}", proxy_digest_ref), proxy_tag)
        owned_image_refs.append(proxy_tag)
        docker("network", "create", "kamal")
        networks.append("kamal")
        docker("network", "create", test_net)
        networks.append(test_net)
        # Baseline Compose-shaped containers: the original five GPoint
        # consumers are running; all GSales consumers stay stopped.
        baseline_rows = []
        service_images = {}
        for i, service in enumerate(("api", "worker", "bot", "agata-api", "agata-memory-worker",
                                     "gsales-api", "gsales-agata-api", "gsales-agata-memory-worker"), 1):
            labels = {"com.docker.compose.project": "compose", "com.docker.compose.service": service}
            name = f"test-compose-{service}-{suffix}"
            if service in ("api", "agata-api"):
                alias = "legacy-api" if service == "api" else "legacy-agata"
                cid = start(name, PYTHON_IMAGE, test_net, command=["python", "-u", "-c", serve_code("legacy")], aliases=(alias,), labels=labels)
            else:
                cid = start(name, PYTHON_IMAGE, test_net, command=["sleep", "infinity"], labels=labels)
                if service.startswith("gsales-"):
                    docker("stop", cid)
            inspect = json.loads(docker("inspect", cid))[0]
            service_images[service] = inspect["Image"]
            baseline_rows.append({"id": cid, "name": name, "imageId": inspect["Image"],
                                  "composeProject": "compose", "composeService": service,
                                  "running": not service.startswith("gsales-")})

        # Real Caddy TLS fronts the legacy backend before switching.
        tmp = Path(tempfile.mkdtemp(prefix="gpoint-kamal-recovery-"))
        tmp.chmod(0o755)
        release_root = tmp / "release-logs"
        backup_root = tmp / "backups"
        (release_root / SHA).mkdir(parents=True, mode=0o700)
        backup_root.mkdir(mode=0o700)
        fixture_images = {}
        for role in ("core", "agata", "miniapp"):
            dockerfile = tmp / f"Dockerfile.{role}"
            dockerfile.write_text(
                f"FROM {PYTHON_IMAGE}\n"
                f"LABEL org.opencontainers.image.revision=\"{APP_SHA}\"\n"
                f"LABEL com.gpoint.integration.role=\"{role}-{suffix}\"\n"
            )
            fixture_images[role] = f"gpoint-cutover-fixture-{role}:{suffix}"
            docker("build", "--quiet", "-t", fixture_images[role], "-f", str(dockerfile), str(tmp))
            fixture_tags.append(fixture_images[role])
        proxy_cert = tmp / "proxy.crt"
        proxy_key = tmp / "proxy.key"
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(proxy_key),
             "-out", str(proxy_cert), "-days", "1", "-subj", "/CN=gpoint.amiora.one",
             "-addext", "subjectAltName=DNS:gpoint.amiora.one"])
        # This is a one-day, self-signed integration fixture key with no
        # production value; the pinned proxy runs as its unprivileged UID.
        proxy_key.chmod(0o644)
        caddyfile = tmp / "Caddyfile"
        caddy_text = """{
  auto_https disable_redirects
}
https://gpoint.amiora.one {
  tls internal
  handle_path /api/* { reverse_proxy legacy-api:80 }
  handle { reverse_proxy legacy-api:80 }
}
"""
        caddyfile.write_text(caddy_text)
        caddy_sha = hashlib.sha256(caddy_text.encode()).hexdigest()
        caddy_name = f"gpoint-test-caddy-{suffix}"
        caddy_container = run(["docker", "run", "-d", "--name", caddy_name, "--network", test_net,
                               "--network-alias", "legacy-caddy", "-v", f"{caddyfile}:/etc/caddy/Caddyfile:ro",
                               CADDY_IMAGE, "caddy", "run", "--config", "/etc/caddy/Caddyfile"])
        container_ids.append(caddy_container)
        client = start(f"gpoint-test-client-{suffix}", PYTHON_IMAGE, test_net, command=["sleep", "infinity"])
        caddy_ip = docker("inspect", "--format", "{{(index .NetworkSettings.Networks \"" + test_net + "\").IPAddress}}", caddy_container)
        wait_http(client, "https://gpoint.amiora.one/api/health/live", "legacy:/health/live", host="gpoint.amiora.one", resolve_ip=caddy_ip)
        wait_http(client, "https://gpoint.amiora.one/", "legacy:/", host="gpoint.amiora.one", resolve_ip=caddy_ip)

        # Candidate app fixtures use real HTTP servers, Kamal Proxy and route
        # control; immutable registry metadata is read-back fixture data only.
        candidate_labels = {"org.opencontainers.image.revision": APP_SHA,
                            "com.docker.compose.project": "", "com.docker.compose.service": ""}
        # Reuse python image for candidate traffic and wrap inspect image readback
        # to provide the exact digest aliases emitted by hosted registry builds.
        core_name = f"gpoint-core-api-{VERSION}"
        mini_name = f"gpoint-miniapp-web-{VERSION}"
        agata_name = f"gpoint-agata-api-{VERSION}"
        worker_names = [f"gpoint-core-worker-{VERSION}", f"gpoint-core-bot-{VERSION}",
                        f"gpoint-agata-memory-worker-{VERSION}"]
        core = start(core_name, fixture_images["core"], test_net, command=["python", "-u", "-c", serve_code("candidate-core", 3000)],
                     aliases=("candidate-core",), labels=candidate_labels)
        mini = start(mini_name, fixture_images["miniapp"], test_net, command=["python", "-u", "-c", serve_code("candidate-mini")],
                     aliases=("candidate-mini",), labels=candidate_labels)
        agata = start(agata_name, fixture_images["agata"], test_net, command=["sleep", "infinity"], labels=candidate_labels)
        extra = [start(n, fixture_images["core" if n.startswith("gpoint-core-") else "agata"], test_net,
                       command=["sleep", "infinity"], labels=candidate_labels) for n in worker_names]
        # Join API and Mini App to the isolated recovery network using attempt
        # scoped aliases. Never touch any pre-existing production network.
        for cid, alias in ((core, core[:12]), (mini, mini[:12]), (agata, agata[:12]), *((x, x[:12]) for x in extra)):
            docker("network", "connect", "--alias", alias, "kamal", cid)
        # Create test alias image references for helper's registry digest check.
        # In-process inspect shim below binds fixture image IDs to the exact
        # digests emitted by the hosted build; no registry artifact is written.

        baseline_epoch = int(time.time())
        proxy_name = "kamal-proxy"
        proxy = start(proxy_name, proxy_tag, "kamal", command=["run", "--http-port", "80", "--https-port", "443"],
                      entrypoint="kamal-proxy", mounts=((proxy_cert, "/tmp/gpoint-proxy.crt"),
                                                         (proxy_key, "/tmp/gpoint-proxy.key")))
        # Add proxy to the private network for internal probes.
        docker("network", "connect", test_net, proxy)
        for _ in range(45):
            if subprocess.run(["docker", "exec", proxy, "kamal-proxy", "list"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Kamal Proxy did not become ready")
        for service, host, target, prefix in (("gpoint-core", "gpoint.amiora.one", f"{core[:12]}:3000", "/api"),
                                               ("gpoint-miniapp", "gpoint.amiora.one", f"{mini[:12]}:80", "")):
            args = ["docker", "exec", proxy, "kamal-proxy", "deploy", service, "--host", host,
                    "--target", target, "--health-check-path=/"]
            if prefix:
                args += ["--path-prefix", prefix, "--strip-path-prefix"]
            else:
                # Configure TLS on the root route using a local certificate;
                # this avoids ACME or any external certificate service.
                args += ["--tls", "--tls-certificate-path=/tmp/gpoint-proxy.crt",
                         "--tls-private-key-path=/tmp/gpoint-proxy.key"]
            run(args)
        proxy_ip = docker("inspect", "--format", "{{(index .NetworkSettings.Networks \"" + test_net + "\").IPAddress}}", proxy)
        wait_http(client, "https://gpoint.amiora.one/api/health/live", "candidate-core:/health/live",
                  host="gpoint.amiora.one", resolve_ip=proxy_ip)
        wait_http(client, "https://gpoint.amiora.one/", "candidate-mini:/", host="gpoint.amiora.one", resolve_ip=proxy_ip)
        wait_http(client, "https://gpoint.amiora.one/media/test-object", "candidate-mini:/media/test-object",
                  host="gpoint.amiora.one", resolve_ip=proxy_ip)

        # Construct the exact on-disk format consumed by production recovery.
        inventory_path = release_root / SHA / f"kamal-vps-inventory-{RUN_ID}-{ATTEMPT}.json"
        inventory_raw = json.dumps({"schema": "gpoint-vps-inventory/v1", "containers": baseline_rows}, sort_keys=True).encode()
        inventory_path.write_bytes(inventory_raw)
        inventory_path.chmod(0o600)
        baseline_path = release_root / SHA / f"kamal-cutover-baseline-{RUN_ID}-{ATTEMPT}.txt"
        values = {"BASELINE_SCHEMA": "gpoint-kamal-cutover-baseline/v1", "CANDIDATE_SHA": SHA,
                  "RUN_ID": RUN_ID, "ATTEMPT": ATTEMPT, "BASELINE_CAPTURED_AT_EPOCH": str(baseline_epoch),
                  "CURRENT_SHA": APP_SHA, "KAMAL_PROXY_BASELINE": "ABSENT", "CADDYFILE_SHA256": caddy_sha,
                  "INVENTORY_FILE": str(inventory_path), "INVENTORY_SHA256": hashlib.sha256(inventory_raw).hexdigest()}
        baseline_raw = "".join(f"{k}={v}\n" for k, v in values.items()).encode()
        baseline_path.write_bytes(baseline_raw)
        baseline_path.chmod(0o600)
        caddy_backup = backup_root / f"caddy-before-kamal-{SHA}-{RUN_ID}-{ATTEMPT}"
        shutil.copyfile(caddyfile, caddy_backup)
        caddy_backup.chmod(0o600)
        checksum = Path(str(caddy_backup) + ".sha256")
        checksum.write_text(f"{caddy_sha}  {caddy_backup}\n")
        checksum.chmod(0o600)

        # Simulate failure after old ingress has stopped. Keep old app
        # consumers stopped and Caddy container stopped; invoke exact helper.
        for service in ("api", "worker", "bot", "agata-api", "agata-memory-worker"):
            row = next(r for r in baseline_rows if r["composeService"] == service)
            docker("stop", row["id"])
        docker("stop", caddy_container)

        original_command = RECOVER.command
        original_inspect = RECOVER.inspect_json
        original_run = RECOVER.subprocess.run
        RECOVER.RELEASE_ROOT, RECOVER.BACKUP_ROOT, RECOVER.CADDYFILE = release_root, backup_root, caddyfile
        image_digests = {
            docker("inspect", "--format", "{{.Image}}", core): (CORE_REPO, DIGESTS["gpoint-core"]),
            docker("inspect", "--format", "{{.Image}}", mini): (MINI_REPO, DIGESTS["gpoint-miniapp"]),
            docker("inspect", "--format", "{{.Image}}", agata): (AGATA_REPO, DIGESTS["gpoint-agata"]),
            docker("inspect", "--format", "{{.Image}}", extra[0]): (CORE_REPO, DIGESTS["gpoint-core"]),
            docker("inspect", "--format", "{{.Image}}", extra[1]): (CORE_REPO, DIGESTS["gpoint-core"]),
            docker("inspect", "--format", "{{.Image}}", extra[2]): (AGATA_REPO, DIGESTS["gpoint-agata"]),
        }
        proxy_image_id = docker("inspect", "--format", "{{.Image}}", proxy)
        image_digests[proxy_image_id] = (PROXY_REPO, proxy_digest)
        def command_bridge(argv, **kw):
            if argv[0] == "sha256sum":
                return hashlib.sha256(caddyfile.read_bytes()).hexdigest() + "  " + str(caddyfile)
            if argv[0] == "caddy":
                return "valid"
            if argv[0] == "curl":
                # Match the production hostname inside this private Docker network.
                path = argv[-1].split("gpoint.amiora.one", 1)[-1] or "/"
                url = "https://gpoint.amiora.one" + path
                return run(["docker", "exec", client, "python",
                            *python_probe_args(f"https://gpoint.amiora.one{path}", "", "gpoint.amiora.one", caddy_ip)])
            return original_command(argv, **kw)
        RECOVER.command = command_bridge
        def inspect_fixture(identifier: str) -> dict:
            row = original_inspect(identifier)
            if row.get("Id") == identifier and identifier in image_digests:
                row["RepoDigests"] = [f"{image_digests[identifier][0]}@{image_digests[identifier][1]}"]
            return row
        RECOVER.inspect_json = inspect_fixture
        def bridged_run(argv, **kwargs):
            if argv[0] == "systemctl":
                if argv[1] == "is-active":
                    active = docker("inspect", "--format", "{{.State.Running}}", caddy_container, check=False) == "true"
                    return subprocess.CompletedProcess(argv, 0 if active else 3)
                if argv[1] == "start":
                    docker("start", caddy_container)
                    return subprocess.CompletedProcess(argv, 0)
            return original_run(argv, **kwargs)
        RECOVER.subprocess.run = bridged_run
        args = SimpleNamespace(candidate_sha=SHA, app_source_sha=APP_SHA, version=VERSION, run_id=RUN_ID,
                               attempt=ATTEMPT, baseline=str(baseline_path), baseline_sha=hashlib.sha256(baseline_raw).hexdigest(),
                               app_digest=DIGESTS["gpoint-core"], agata_digest=DIGESTS["gpoint-agata"],
                               miniapp_digest=DIGESTS["gpoint-miniapp"], proxy_digest=proxy_digest)
        try:
            RECOVER.recover(args)
        finally:
            RECOVER.command, RECOVER.inspect_json, RECOVER.subprocess.run = original_command, original_inspect, original_run

        wait_http(client, "https://gpoint.amiora.one/api/health/live", "legacy:/health/live", host="gpoint.amiora.one", resolve_ip=caddy_ip)
        wait_http(client, "https://gpoint.amiora.one/", "legacy:/", host="gpoint.amiora.one", resolve_ip=caddy_ip)
        if docker("inspect", "--format", "{{.State.Running}}", proxy) != "false":
            raise RuntimeError("recovery did not stop the current Kamal Proxy")
        for row in baseline_rows:
            running = docker("inspect", "--format", "{{.State.Running}}", row["id"]) == "true"
            expected_running = not row["composeService"].startswith("gsales-")
            if running != expected_running:
                raise RuntimeError(f"baseline service running state differs: {row['composeService']}")
        print("KAMAL_CUTOVER_SWITCH_TRAFFIC=PASS")
        print("KAMAL_CUTOVER_LATE_FAILURE_RECOVERY=PASS")
        print("KAMAL_CUTOVER_HOST_PORTS=NONE")
        return 0
    finally:
        # Exact unique names only; never use broad labels or global prune.
        for cid in reversed(container_ids):
            docker("rm", "-f", cid, check=False)
        for network in reversed(networks):
            docker("network", "rm", network, check=False)
        for tag in reversed(fixture_tags):
            docker("image", "rm", tag, check=False)
        for image_ref in reversed(owned_image_refs):
            docker("image", "rm", image_ref, check=False)
        if "tmp" in locals():
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
