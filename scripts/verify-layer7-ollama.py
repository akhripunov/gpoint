#!/usr/bin/env python3
"""Fail-closed smoke check for the pinned Layer7 Ollama embedding service."""

import json
import re
import subprocess
import sys
from urllib.request import Request, urlopen


BASE_URL = "http://127.0.0.1:11435"
MODEL_NAME = "qwen3-embedding:0.6b"
IMAGE = "ollama/ollama@sha256:a717bfc1c56acfc2e85d3109bd40a7646258ec9062c40cea83b9ed0927eb4223"
CONTAINER = "gpoint-eros-relay-ollama"
CONTAINER_IMAGES = {
    CONTAINER: IMAGE,
    "ollama-layer7": "ollama/ollama@sha256:0e59999e0e8f9fd4e704bd788a89b7e4cad399367fb1365f8c102ac662288761",
}


def verify(expected_digest: str, base_url: str = BASE_URL, docker_inspect=None,
           container_name: str = CONTAINER) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
        raise ValueError("invalid expected model digest")
    expected_image = CONTAINER_IMAGES.get(container_name)
    if expected_image is None:
        raise ValueError("unrecognized Ollama container identity")
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
    if container["HostConfig"]["PortBindings"].get("11434/tcp") != [
        {"HostIp": "127.0.0.1", "HostPort": "11435"}
    ]:
        raise ValueError("Ollama accessory is not loopback-only")
    if container["HostConfig"]["RestartPolicy"]["Name"] != "unless-stopped":
        raise ValueError("Ollama accessory restart policy mismatch")
    if not any(
        mount.get("Name") == "ollama-layer7-models"
        and mount.get("Destination") == "/root/.ollama"
        and mount.get("RW") is True
        for mount in container["Mounts"]
    ):
        raise ValueError("Ollama accessory model volume identity mismatch")
    with urlopen(f"{base_url}/api/tags", timeout=10) as response:
        models = json.load(response).get("models")
    if not isinstance(models, list):
        raise ValueError("invalid Ollama model inventory")
    model = next((entry for entry in models if entry.get("name") == MODEL_NAME), None)
    if not model or model.get("digest") != expected_digest:
        raise ValueError("pinned Ollama model digest is unavailable")

    body = json.dumps({
        "model": MODEL_NAME,
        "input": "Synthetic Layer7 embedding readiness probe.",
        "dimensions": 512,
    }).encode()
    request = Request(
        f"{base_url}/api/embed",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
    embeddings = payload.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != 1:
        raise ValueError("invalid embedding response")
    vector = embeddings[0]
    if not isinstance(vector, list) or len(vector) != 512 or not any(
        isinstance(value, (int, float)) and abs(value) > 1e-8 for value in vector
    ):
        raise ValueError("embedding dimension or values are invalid")


def main() -> int:
    container_name = sys.argv[2] if len(sys.argv) == 3 else CONTAINER
    if len(sys.argv) not in (2, 3) or container_name not in CONTAINER_IMAGES:
        print("usage: verify-layer7-ollama.py <expected-model-sha256> [gpoint-eros-relay-ollama|ollama-layer7]", file=sys.stderr)
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
