#!/usr/bin/env python3
"""Fail-closed smoke check for the pinned Layer7 Ollama embedding service."""

import json
import re
import subprocess
import sys


BASE_URL = "http://ollama:11434"
MODEL_NAME = "qwen3-embedding:0.6b"
IMAGE = "ollama/ollama@sha256:a717bfc1c56acfc2e85d3109bd40a7646258ec9062c40cea83b9ed0927eb4223"
CONTAINER = "gpoint-eros-relay-ollama"
NETWORK = "gpoint_ollama_internal"


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
const fail = () => { console.error("OLLAMA_PRIVATE_ROUTE_VERIFY=FAIL"); process.exit(1); };
try {
  const tags = await fetch(`${base}/api/tags`, { signal: AbortSignal.timeout(10000) });
  if (!tags.ok) fail();
  const models = (await tags.json()).models;
  if (!Array.isArray(models) || !models.some((m) => m.name === "qwen3-embedding:0.6b" && m.digest === expected)) fail();
  const response = await fetch(`${base}/api/embed`, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "qwen3-embedding:0.6b", input: "Synthetic Layer7 embedding readiness probe.", dimensions: 512, truncate: false }),
    signal: AbortSignal.timeout(60000) });
  if (!response.ok) fail();
  const rows = (await response.json()).embeddings;
  if (!Array.isArray(rows) || rows.length !== 1 || !Array.isArray(rows[0]) || rows[0].length !== 512 ||
      !rows[0].every((v) => typeof v === "number" && Number.isFinite(v)) || !rows[0].some((v) => Math.abs(v) > 1e-8)) fail();
  console.log("OLLAMA_PRIVATE_ROUTE_VERIFY=PASS model=qwen3-embedding:0.6b dimensions=512");
} catch { fail(); }'''
    result = inspect(
        ["docker", "exec", api_containers[0], "node", "-e", probe, expected_digest],
        check=True, capture_output=True, text=True,
    )
    if "OLLAMA_PRIVATE_ROUTE_VERIFY=PASS" not in result.stdout:
        raise ValueError("Core API could not verify the private Ollama embedding route")


def main() -> int:
    container_name = sys.argv[2] if len(sys.argv) == 3 else CONTAINER
    if len(sys.argv) not in (2, 3) or container_name != CONTAINER:
        print(f"usage: verify-layer7-ollama.py <expected-model-sha256> [{CONTAINER}]", file=sys.stderr)
        return 2
    try:
        verify(sys.argv[1], container_name=container_name)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"LAYER7_OLLAMA_VERIFY=FAIL reason={type(error).__name__}", file=sys.stderr)
        return 1
    print(f"LAYER7_OLLAMA_VERIFY=PASS model={MODEL_NAME} dimensions=512")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
