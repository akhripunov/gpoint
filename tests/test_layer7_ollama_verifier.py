import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/verify-layer7-ollama.py"
EXPECTED_DIGEST = "ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d"


def load_module():
    spec = importlib.util.spec_from_file_location("verify_layer7_ollama", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Result:
    def __init__(self, value):
        self.stdout = value


def fixture(module, *, host_ip="127.0.0.1", internal=True, alias=True,
            model_digest=EXPECTED_DIGEST, dimension=512, attached=None):
    image = module.IMAGE
    name = module.CONTAINER
    api = "gpoint-core-api-" + "a" * 40 + "-1-1"
    attached = attached or {name, api}
    container = {
        "Config": {"Image": image}, "State": {"Running": True},
        "HostConfig": {"PortBindings": {"11434/tcp": [{"HostIp": host_ip, "HostPort": "11435"}]},
                       "RestartPolicy": {"Name": "unless-stopped"}},
        "Mounts": [{"Name": "ollama-layer7-models", "Destination": "/root/.ollama", "RW": True}],
        "NetworkSettings": {"Networks": {module.NETWORK: {"Aliases": ["ollama"] if alias else []}}},
    }
    network = {"Internal": internal, "Containers": {
        str(i): {"Name": n} for i, n in enumerate(sorted(attached), 1)
    }}
    calls = []

    def inspect(args, **_kwargs):
        calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            return Result(json.dumps([container]))
        if args[:3] == ["docker", "image", "inspect"]:
            return Result(json.dumps([{"Os": "linux", "Architecture": "amd64", "RepoDigests": [image]}]))
        if args[:3] == ["docker", "network", "inspect"]:
            return Result(json.dumps([network]))
        if args[:2] == ["docker", "ps"]:
            return Result("core-id\n")
        if args[:2] == ["docker", "exec"]:
            assert "http://ollama:11434" in args[5]
            assert 'dimensions: 512' in args[5]
            return Result("OLLAMA_PRIVATE_ROUTE_VERIFY=PASS model=qwen3-embedding:0.6b dimensions=512\n")
        raise AssertionError(args)

    return inspect, calls


def test_verifies_pinned_model_and_direct_private_api_embedding_route():
    module = load_module()
    inspect, calls = fixture(module)
    module.verify(EXPECTED_DIGEST, docker_inspect=inspect)
    assert any(call[:3] == ["docker", "network", "inspect"] for call in calls)
    assert calls[-1][1] == "exec"


@pytest.mark.parametrize("options, message", [
    ({"host_ip": "0.0.0.0"}, "loopback-only"),
    ({"internal": False}, "not internal"),
    ({"alias": False}, "alias"),
    ({"attached": {"gpoint-ollama-ollama", "unrelated-container"}}, "unrelated"),
])
def test_rejects_unsafe_public_or_unrelated_network_state(options, message):
    module = load_module()
    inspect, _ = fixture(module, **options)
    with pytest.raises(ValueError, match=message):
        module.verify(EXPECTED_DIGEST, docker_inspect=inspect)


def test_rejects_invalid_model_digest_before_docker_access():
    module = load_module()
    with pytest.raises(ValueError, match="digest"):
        module.verify("bad")
