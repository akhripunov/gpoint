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
    def __init__(self, value, *, returncode=0, stderr=""):
        self.stdout = value
        self.returncode = returncode
        self.stderr = stderr


def docker_result(args, value=None, *, returncode=0, stderr=""):
    return Result("" if value is None else json.dumps(value), returncode=returncode, stderr=stderr)


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


def test_retries_transient_private_embedding_probe_before_failing_closed(capsys):
    module = load_module()
    inspect, _ = fixture(module)
    attempts = 0

    def transient_probe(args, **kwargs):
        nonlocal attempts
        if args[:2] == ["docker", "exec"]:
            attempts += 1
            if attempts == 1:
                return Result(
                    "OLLAMA_PRIVATE_ROUTE_VERIFY=FAIL reason=embed_http_503\n",
                    returncode=1,
                    stderr="temporary upstream unavailable",
                )
        return inspect(args, **kwargs)

    module.verify(EXPECTED_DIGEST, docker_inspect=transient_probe)

    assert attempts == 2
    assert "OLLAMA_PRIVATE_ROUTE_VERIFY=RETRY attempt=1 reason=embed_http_503" in capsys.readouterr().err


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


def test_prepare_creates_only_the_internal_network_and_reports_absent_accessory():
    module = load_module()
    network = {"Internal": True, "Containers": {}}
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["docker", "network", "inspect"]:
            return docker_result(args, [network])
        if args[:2] == ["docker", "inspect"]:
            name = args[2]
            return Result("", returncode=1, stderr=f"error: no such object: {name}")
        raise AssertionError(args)

    assert module.prepare(docker_run=run) == "absent"
    assert [call for call in calls if call[:3] == ["docker", "network", "create"]] == []
    assert not any("--format" in call for call in calls)


def test_prepare_retries_transient_docker_container_inventory_error(capsys):
    module = load_module()
    network = {"Internal": True, "Containers": {"1": {"Name": module.CONTAINER}}}
    container = {
        "Config": {"Image": module.IMAGE},
        "State": {"Status": "running"},
        "HostConfig": {
            "PortBindings": {"11434/tcp": [{"HostIp": "127.0.0.1", "HostPort": "11435"}]},
        },
        "Mounts": [{"Name": "ollama-layer7-models", "Destination": "/root/.ollama", "RW": True}],
        "NetworkSettings": {"Networks": {module.NETWORK: {"Aliases": ["ollama"]}}},
    }
    attempts = 0

    def run(args, **_kwargs):
        nonlocal attempts
        if args[:3] == ["docker", "network", "inspect"]:
            return docker_result(args, [network])
        if args[:2] == ["docker", "inspect"]:
            if args[2] == module.CONTAINER:
                attempts += 1
                if attempts == 1:
                    return Result("", returncode=1, stderr="Error response from daemon: context deadline exceeded")
                return docker_result(args, [container])
            return Result("", returncode=1, stderr=f"Error: No such object: {args[2]}")
        raise AssertionError(args)

    assert module.prepare(docker_run=run) == "running"
    assert attempts == 2
    assert "OLLAMA_DOCKER_INVENTORY=RETRY attempt=1 kind=container" in capsys.readouterr().err


def test_prepare_migrates_only_the_known_legacy_accessory_without_restarting_it():
    module = load_module()
    network = {"Internal": True, "Containers": {}}
    legacy_name = "gpoint-eros-relay-ollama"
    container = {
        "Config": {"Image": module.IMAGE},
        "State": {"Status": "running"},
        "HostConfig": {
            "PortBindings": {"11434/tcp": [{"HostIp": "127.0.0.1", "HostPort": "11435"}]},
        },
        "Mounts": [{"Name": "ollama-layer7-models", "Destination": "/root/.ollama", "RW": True}],
        "NetworkSettings": {"Networks": {"kamal": {"Aliases": [legacy_name]}}},
    }
    names = {legacy_name: container}
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["docker", "network", "inspect"]:
            return docker_result(args, [network])
        if args[:2] == ["docker", "inspect"]:
            name = args[2]
            if name not in names:
                return Result("", returncode=1, stderr=f"Error: No such object: {name}")
            return docker_result(args, [names[name]])
        if args[:3] == ["docker", "network", "connect"]:
            assert args[3:6] == ["--alias", "ollama", module.NETWORK]
            names[args[6]]["NetworkSettings"]["Networks"][module.NETWORK] = {"Aliases": ["ollama"]}
            network["Containers"]["1"] = {"Name": legacy_name}
            return Result("")
        if args[:2] == ["docker", "rename"]:
            old, new = args[2:]
            names[new] = names.pop(old)
            network["Containers"]["1"]["Name"] = new
            return Result("")
        raise AssertionError(args)

    assert module.prepare(docker_run=run) == "running"
    assert module.CONTAINER in names and legacy_name not in names
    assert ["docker", "network", "connect", "--alias", "ollama", module.NETWORK, legacy_name] in calls
    assert ["docker", "rename", legacy_name, module.CONTAINER] in calls
    assert not any(call[1] in {"restart", "start"} for call in calls if len(call) > 1)
    assert not any("--format" in call for call in calls)


def test_prepare_fails_closed_when_both_accessory_names_exist():
    module = load_module()
    network = {"Internal": True, "Containers": {}}
    container = {"Config": {"Image": module.IMAGE}, "State": {"Status": "running"}}

    def run(args, **_kwargs):
        if args[:3] == ["docker", "network", "inspect"]:
            return docker_result(args, [network])
        if args[:2] == ["docker", "inspect"]:
            return docker_result(args, [container])
        raise AssertionError(args)

    with pytest.raises(ValueError, match="Both current and legacy"):
        module.prepare(docker_run=run)


def test_verify_and_detach_removes_only_the_kamal_network_after_full_verification():
    module = load_module()
    inspect, calls = fixture(module)
    attached = {"value": True}

    def inspect_with_kamal(args, **kwargs):
        result = inspect(args, **kwargs)
        if args[:2] == ["docker", "inspect"]:
            rows = json.loads(result.stdout)
            if attached["value"]:
                rows[0]["NetworkSettings"]["Networks"]["kamal"] = {"Aliases": [module.CONTAINER]}
            return Result(json.dumps(rows))
        return result

    def mutate(args, **_kwargs):
        calls.append(args)
        assert args == ["docker", "network", "disconnect", "kamal", module.CONTAINER]
        attached["value"] = False
        return Result("")

    assert module.verify_and_detach(EXPECTED_DIGEST, docker_inspect=inspect_with_kamal, docker_mutate=mutate) is True
    assert ["docker", "network", "disconnect", "kamal", module.CONTAINER] in calls
