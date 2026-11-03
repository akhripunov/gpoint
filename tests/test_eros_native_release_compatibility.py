import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "eros_native_release_compatibility", ROOT / "scripts/eros_native_release_compatibility.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_identical_pilot_and_candidate_contracts_allow_ordinary_release():
    source = {path: ("contract:" + path).encode() for path in MODULE.CONTRACT_PATHS}
    read = lambda _sha, path: source[path]
    assert MODULE.compare_contract("a" * 40, "b" * 40, read) == MODULE.contract_digest("a" * 40, read)


def test_changed_eros_adapter_contract_blocks_until_pilot_upgrade_is_approved():
    pilot = {path: ("pilot:" + path).encode() for path in MODULE.CONTRACT_PATHS}
    candidate = dict(pilot)
    candidate["apps/api/src/eros-adapter.ts"] += b"\nchanged"
    def read(sha, path):
        return (pilot if sha == "a" * 40 else candidate)[path]
    with pytest.raises(MODULE.CompatibilityError, match="contracts differ"):
        MODULE.compare_contract("a" * 40, "b" * 40, read)


def test_missing_contract_file_fails_closed():
    with pytest.raises(MODULE.CompatibilityError, match="unavailable"):
        MODULE.contract_digest("a" * 40, lambda _sha, _path: None)


def test_api_embedding_network_uses_dynamic_address_during_kamal_rollout():
    hook = (ROOT / ".kamal/hooks/post-app-boot").read_text()
    section = hook.split('if [[ "$service:$role" == "gpoint-core:api" ]]', 1)[1]
    assert "docker network connect gpoint_eros_embeddings \"$api_id\"" in section
    assert "docker network connect --ip 172.31.254.2" not in section


def test_ordinary_deploy_selects_eros_api_config_when_pilot_is_active():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1].split("  backup_maintenance:\n", 1)[0]
    assert "GPOINT_EROS_PILOT_ACTIVE" in deploy
    assert "config/deploy.eros-api.yml" in deploy
    assert "deploy.yml" in deploy
