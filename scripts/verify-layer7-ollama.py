#!/usr/bin/env python3
"""Fail-closed smoke check for the pinned Layer7 Ollama embedding service."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time


BASE_URL = "http://ollama:11434"
MODEL_NAME = "qwen3-embedding:0.6b"
IMAGE = "ollama/ollama@sha256:a717bfc1c56acfc2e85d3109bd40a7646258ec9062c40cea83b9ed0927eb4223"
CONTAINER = "gpoint-ollama-ollama"
LEGACY_CONTAINER = "gpoint-eros-relay-ollama"
NETWORK = "gpoint_ollama_internal"
LEGACY_PORT_BINDINGS = {"11434/tcp": [{"HostIp": "127.0.0.1", "HostPort": "11435"}]}


def _inspect_optional(kind: str, name: str, docker_run) -> dict | None:
    command = ["docker", "network", "inspect", name] if kind == "network" else ["docker", "inspect", name]
    result = docker_run(command, check=False,
                        capture_output=True, text=True)
    if result.returncode:
        message = result.stderr or ""
        missing = ((kind == "network" and
                    (f"network {name} not found" in message or f"No such network: {name}" in message))
                   or (kind == "container" and
                       (f"No such object: {name}" in message or f"No such container: {name}" in message)))
        if missing:
            return None
        raise ValueError(f"Docker {kind} inventory could not be read")
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"Docker {kind} inventory is invalid JSON") from error
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError(f"Docker {kind} inventory is ambiguous")
    return rows[0]


def _run_docker(args: list[str], docker_run) -> None:
    result = docker_run(args, check=False, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"Docker operation failed: {args[1]}")


def prepare(docker_run=None) -> str:
    """Ensure the private network and safely migrate the known legacy accessory name."""
    run = docker_run or subprocess.run
    network = _inspect_optional("network", NETWORK, run)
    if network is None:
        _run_docker(["docker", "network", "create", "--internal", NETWORK], run)
        network = _inspect_optional("network", NETWORK, run)
    if network is None or network.get("Internal") is not True:
        raise ValueError("Ollama Docker network is not internal")

    current = _inspect_optional("container", CONTAINER, run)
    legacy = _inspect_optional("container", LEGACY_CONTAINER, run)
    if current is not None and legacy is not None:
        raise ValueError("Both current and legacy Ollama containers exist")
    if current is None and legacy is None:
        return "absent"

    if current is not None:
        state = current.get("State", {}).get("Status")
        if state not in {"running", "exited", "created"}:
            raise ValueError("Ollama accessory is in an unexpected state")
        if current.get("Config", {}).get("Image") != IMAGE:
            raise ValueError("Ollama accessory image differs from the pinned model server")
        if current.get("HostConfig", {}).get("PortBindings") != LEGACY_PORT_BINDINGS:
            raise ValueError("Ollama accessory loopback binding differs from the approved configuration")
        if not any(m.get("Name") == "ollama-layer7-models" and
                   m.get("Destination") == "/root/.ollama" and m.get("RW") is True
                   for m in current.get("Mounts", [])):
            raise ValueError("Ollama accessory model volume is not the approved persistent volume")
        networks = current.get("NetworkSettings", {}).get("Networks") or {}
        private = networks.get(NETWORK)
        if (set(networks) - {NETWORK, "kamal"} or not private
                or "ollama" not in (private.get("Aliases") or [])):
            raise ValueError("Ollama accessory is missing its private network alias")
        return state

    state = legacy.get("State", {}).get("Status")
    if state not in {"running", "exited", "created"}:
        raise ValueError("Legacy Ollama accessory is in an unexpected state")
    if legacy.get("Config", {}).get("Image") != IMAGE:
        raise ValueError("Legacy Ollama accessory image differs from the pinned model server")
    if legacy.get("HostConfig", {}).get("PortBindings") != LEGACY_PORT_BINDINGS:
        raise ValueError("Legacy Ollama accessory loopback binding differs from the approved configuration")
    if not any(m.get("Name") == "ollama-layer7-models" and
               m.get("Destination") == "/root/.ollama" and m.get("RW") is True
               for m in legacy.get("Mounts", [])):
        raise ValueError("Legacy Ollama model volume is not the approved persistent volume")

    networks = legacy.get("NetworkSettings", {}).get("Networks") or {}
    private = networks.get(NETWORK)
    if (private and set(networks) <= {NETWORK, "kamal"}
            and "ollama" in (private.get("Aliases") or [])):
        pass
    elif set(networks) == {"kamal"} and state == "running":
        _run_docker(["docker", "network", "connect", "--alias", "ollama", NETWORK,
                     LEGACY_CONTAINER], run)
    else:
        raise ValueError("Legacy Ollama network topology is not recognized")

    _run_docker(["docker", "rename", LEGACY_CONTAINER, CONTAINER], run)
    migrated = _inspect_optional("container", CONTAINER, run)
    if migrated is None or migrated.get("State", {}).get("Status") != state:
        raise ValueError("Legacy Ollama accessory identity changed during rename")
    migrated_networks = migrated.get("NetworkSettings", {}).get("Networks") or {}
    private = migrated_networks.get(NETWORK)
    if (set(migrated_networks) - {NETWORK, "kamal"} or not private
            or "ollama" not in (private.get("Aliases") or [])):
        raise ValueError("Migrated Ollama accessory lost its private network alias")
    return state


def verify(expected_digest: str, base_url: str = BASE_URL, docker_inspect=None,
           container_name: str = CONTAINER) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
        raise ValueError("invalid expected model digest")
    if container_name != CONTAINER:
        raise ValueError("unrecognized Ollama container identity")
    expected_image = IMAGE
    inspect = docker_inspect or subprocess.run
    inspected = inspect(
        ["docker", "inspect", container_name], check=True, capture_output=True, text=True
    )
    container = json.loads(inspected.stdout)[0]
    if container["Config"]["Image"] != expected_image:
        raise ValueError("Ollama accessory image identity mismatch")
    image_result = inspect(
        ["docker", "image", "inspect", expected_image],
        check=True,
        capture_output=True,
        text=True,
    )
    image = json.loads(image_result.stdout)[0]
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64":
        raise ValueError("Ollama accessory platform mismatch")
    if expected_image not in image.get("RepoDigests", []):
        raise ValueError("Ollama accessory OCI digest mismatch")
    if container["State"]["Running"] is not True:
        raise ValueError("Ollama accessory is not running")
    bindings = container["HostConfig"].get("PortBindings") or {}
    if bindings != {"11434/tcp": [{"HostIp": "127.0.0.1", "HostPort": "11435"}]}:
        raise ValueError("Ollama accessory port binding is not loopback-only")
    network = container.get("NetworkSettings", {}).get("Networks", {}).get(NETWORK)
    if not network or "ollama" not in network.get("Aliases", []):
        raise ValueError("Ollama accessory is missing its private Docker network alias")
    network_result = inspect(["docker", "network", "inspect", NETWORK], check=True, capture_output=True, text=True)
    network_config = json.loads(network_result.stdout)[0]
    if network_config.get("Internal") is not True:
        raise ValueError("Ollama Docker network is not internal")
    allowed_names = {container_name}
    for row in network_config.get("Containers", {}).values():
        name = row.get("Name", "")
        if name == container_name or name.startswith("gpoint-core-api-"):
            allowed_names.add(name)
        else:
            raise ValueError("unrelated container is attached to the Ollama network")
    if container_name not in allowed_names:
        raise ValueError("Ollama accessory is absent from its private network inventory")
    if container["HostConfig"]["RestartPolicy"]["Name"] != "unless-stopped":
        raise ValueError("Ollama accessory restart policy mismatch")
    if not any(
        mount.get("Name") == "ollama-layer7-models"
        and mount.get("Destination") == "/root/.ollama"
        and mount.get("RW") is True
        for mount in container["Mounts"]
    ):
        raise ValueError("Ollama accessory model volume identity mismatch")
    api_containers = inspect(
        ["docker", "ps", "-q", "--filter", "name=^/gpoint-core-api-"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    if len(api_containers) != 1:
        raise ValueError("expected exactly one running Core API for the embedding route probe")
    probe = r'''const base = "http://ollama:11434";
const expected = process.argv[1];
const fail = (reason) => { console.error(`OLLAMA_PRIVATE_ROUTE_VERIFY=FAIL reason=${reason}`); process.exit(1); };
try {
  const tags = await fetch(`${base}/api/tags`, { signal: AbortSignal.timeout(10000) });
  if (!tags.ok) fail(`tags_http_${tags.status}`);
  const models = (await tags.json()).models;
  if (!Array.isArray(models) || !models.some((m) => m.name === "qwen3-embedding:0.6b" && m.digest === expected)) fail("model_digest_mismatch");
  const response = await fetch(`${base}/api/embed`, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "qwen3-embedding:0.6b", input: "Synthetic Layer7 embedding readiness probe.", dimensions: 512, truncate: false }),
    signal: AbortSignal.timeout(60000) });
  if (!response.ok) fail(`embed_http_${response.status}`);
  const rows = (await response.json()).embeddings;
  if (!Array.isArray(rows) || rows.length !== 1 || !Array.isArray(rows[0]) || rows[0].length !== 512 ||
      !rows[0].every((v) => typeof v === "number" && Number.isFinite(v)) || !rows[0].some((v) => Math.abs(v) > 1e-8)) fail("embedding_shape");
  console.log("OLLAMA_PRIVATE_ROUTE_VERIFY=PASS model=qwen3-embedding:0.6b dimensions=512");
} catch (error) { fail(`request_${error?.name || "unknown"}`); }'''
    probe_command = ["docker", "exec", api_containers[0], "node", "-e", probe, expected_digest]
    retryable_probe_failure = re.compile(
        r"(?:tags|embed)_http_(?:429|5[0-9]{2})|request_(?:AbortError|TimeoutError|TypeError)"
    )
    for attempt in range(1, 4):
        result = inspect(probe_command, check=False, capture_output=True, text=True)
        if result.returncode == 0 and "OLLAMA_PRIVATE_ROUTE_VERIFY=PASS" in result.stdout:
            break
        detail = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        detail = detail or f"exit_{result.returncode}"
        match = re.search(r"OLLAMA_PRIVATE_ROUTE_VERIFY=FAIL reason=([a-zA-Z0-9_]+)", detail)
        reason = match.group(1) if match else f"exit_{result.returncode}"
        if attempt == 3 or not retryable_probe_failure.fullmatch(reason):
            raise ValueError(f"Core API private Ollama probe failed: {reason}")
        print(f"OLLAMA_PRIVATE_ROUTE_VERIFY=RETRY attempt={attempt} reason={reason}", file=sys.stderr)
        time.sleep(attempt)


def verify_and_detach(expected_digest: str, docker_inspect=None, docker_mutate=None) -> bool:
    """Verify the accessory, then remove only its obsolete Kamal-network attachment."""
    inspect = docker_inspect or subprocess.run
    mutate = docker_mutate or subprocess.run
    verify(expected_digest, docker_inspect=inspect)
    container = _inspect_optional("container", CONTAINER, inspect)
    if container is None:
        raise ValueError("Ollama accessory disappeared after verification")
    networks = set((container.get("NetworkSettings", {}).get("Networks") or {}).keys())
    if networks == {NETWORK}:
        return False
    if networks != {NETWORK, "kamal"}:
        raise ValueError("Ollama accessory network isolation cannot be confirmed")
    _run_docker(["docker", "network", "disconnect", "kamal", CONTAINER], mutate)
    verify(expected_digest, docker_inspect=inspect)
    return True


def main() -> int:
    args = sys.argv[1:]
    mode = args[1] if len(args) == 2 and args[1] in {"--prepare", "--verify-and-detach"} else "--verify"
    if len(args) not in (1, 2) or (len(args) == 2 and mode == "--verify" and args[1] != CONTAINER):
        print(f"usage: verify-layer7-ollama.py <expected-model-sha256> [{CONTAINER}|--prepare|--verify-and-detach]",
              file=sys.stderr)
        return 2
    try:
        if mode == "--prepare":
            if not re.fullmatch(r"[a-f0-9]{64}", args[0]):
                raise ValueError("invalid expected model digest")
            print(f"OLLAMA_PREPARE=PASS state={prepare()}")
            return 0
        if mode == "--verify-and-detach":
            detached = verify_and_detach(args[0])
            print(f"LAYER7_OLLAMA_KAMAL_NETWORK={'DETACHED' if detached else 'UNCHANGED'}")
        else:
            container_name = args[1] if len(args) == 2 else CONTAINER
            verify(args[0], container_name=container_name)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"LAYER7_OLLAMA_VERIFY=FAIL reason={type(error).__name__} detail={error}", file=sys.stderr)
        return 1
    print(f"LAYER7_OLLAMA_VERIFY=PASS model={MODEL_NAME} dimensions=512")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
