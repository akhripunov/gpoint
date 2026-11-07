import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/verify-layer7-ollama.py"
EXPECTED_DIGEST = "ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d"


def load_module():
    spec = importlib.util.spec_from_file_location("verify_layer7_ollama", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class FakeCompletedProcess:
    def __init__(self, stdout):
        self.stdout = stdout


def valid_container():
    return {
        "Config": {"Image": "ollama/ollama@sha256:a717bfc1c56acfc2e85d3109bd40a7646258ec9062c40cea83b9ed0927eb4223"},
        "State": {"Running": True},
        "HostConfig": {
            "PortBindings": {"11434/tcp": [{"HostIp": "127.0.0.1", "HostPort": "11435"}]},
            "RestartPolicy": {"Name": "unless-stopped"},
        },
        "Mounts": [{"Name": "ollama-layer7-models", "Destination": "/root/.ollama", "RW": True}],
    }


def configure_fake_ollama(monkeypatch, module, *, digest=EXPECTED_DIGEST, dimensions=512,
                          container=None, repo_digests=None, os_name="linux", architecture="amd64"):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        url = request.full_url if hasattr(request, "full_url") else request
        if url.endswith("/api/tags"):
            return FakeResponse({"models": [{"name": "qwen3-embedding:0.6b", "digest": digest}]})
        payload = json.loads(request.data)
        assert payload == {
            "model": "qwen3-embedding:0.6b",
            "input": "Synthetic Layer7 embedding readiness probe.",
            "dimensions": 512,
        }
        vector = [0.0] * dimensions
        if vector:
            vector[0] = 0.5
        return FakeResponse({"embeddings": [vector]})

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    def docker_inspect(args, **_kwargs):
        if args[1:3] == ["image", "inspect"]:
            return FakeCompletedProcess(json.dumps([{
                "Os": os_name,
                "Architecture": architecture,
                "RepoDigests": repo_digests or [
                    "ollama/ollama@sha256:a717bfc1c56acfc2e85d3109bd40a7646258ec9062c40cea83b9ed0927eb4223"
                ],
            }]))
        return FakeCompletedProcess(json.dumps([container or valid_container()]))
    return requests, docker_inspect


def test_layer7_ollama_verifier_accepts_exact_digest_and_512_dimensions(monkeypatch):
    module = load_module()
    requests, docker_inspect = configure_fake_ollama(monkeypatch, module)
    module.verify(EXPECTED_DIGEST, "http://ollama.test", docker_inspect)
    assert [request.full_url if hasattr(request, "full_url") else request for request in requests] == [
        "http://ollama.test/api/tags",
        "http://ollama.test/api/embed",
    ]


def test_layer7_ollama_verifier_accepts_only_the_pinned_legacy_container_for_handoff(monkeypatch):
    module = load_module()
    legacy = deepcopy(valid_container())
    legacy["Config"]["Image"] = "ollama/ollama@sha256:0e59999e0e8f9fd4e704bd788a89b7e4cad399367fb1365f8c102ac662288761"
    _, docker_inspect = configure_fake_ollama(
        monkeypatch, module, container=legacy,
        repo_digests=[legacy["Config"]["Image"]],
    )
    module.verify(EXPECTED_DIGEST, "http://ollama.test", docker_inspect,
                  container_name="ollama-layer7")


def test_layer7_ollama_verifier_rejects_unexpected_model_digest(monkeypatch):
    module = load_module()
    _, docker_inspect = configure_fake_ollama(monkeypatch, module, digest="0" * 64)
    with pytest.raises(ValueError, match="digest"):
        module.verify(EXPECTED_DIGEST, "http://ollama.test", docker_inspect)


def test_layer7_ollama_verifier_rejects_wrong_embedding_dimensions(monkeypatch):
    module = load_module()
    _, docker_inspect = configure_fake_ollama(monkeypatch, module, dimensions=384)
    with pytest.raises(ValueError, match="dimension"):
        module.verify(EXPECTED_DIGEST, "http://ollama.test", docker_inspect)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c["Config"].update(Image="ollama/ollama:latest"), "image identity"),
        (lambda c: c["State"].update(Running=False), "not running"),
        (lambda c: c["HostConfig"]["PortBindings"].update({"11434/tcp": [{"HostIp": "0.0.0.0", "HostPort": "11435"}]}), "loopback-only"),
        (lambda c: c["HostConfig"]["RestartPolicy"].update(Name="no"), "restart policy"),
        (lambda c: c.update(Mounts=[]), "volume identity"),
    ],
)
def test_layer7_ollama_verifier_fails_closed_on_accessory_identity(mutate, message, monkeypatch):
    module = load_module()
    container = deepcopy(valid_container())
    mutate(container)
    _, docker_inspect = configure_fake_ollama(monkeypatch, module, container=container)
    with pytest.raises(ValueError, match=message):
        module.verify(EXPECTED_DIGEST, "http://ollama.test", docker_inspect)


def test_layer7_ollama_verifier_fails_closed_on_wrong_oci_digest(monkeypatch):
    module = load_module()
    _, docker_inspect = configure_fake_ollama(monkeypatch, module, repo_digests=["ollama/ollama@sha256:" + "0" * 64])
    with pytest.raises(ValueError, match="OCI digest"):
        module.verify(EXPECTED_DIGEST, "http://ollama.test", docker_inspect)


def test_layer7_ollama_verifier_fails_closed_on_wrong_platform(monkeypatch):
    module = load_module()
    _, docker_inspect = configure_fake_ollama(monkeypatch, module, architecture="arm64")
    with pytest.raises(ValueError, match="platform"):
        module.verify(EXPECTED_DIGEST, "http://ollama.test", docker_inspect)
