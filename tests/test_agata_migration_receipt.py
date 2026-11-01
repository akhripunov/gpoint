import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('parity_receipt', ROOT / 'scripts/verify-kamal-migration-parity.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def receipt(tmp_path):
    path = tmp_path / 'applied.json'
    value = {'schema': 'gpoint-agata-applied-migration/v1', 'status': 'PASS',
             'source_sha': 'a' * 40, 'head_before': 'agata0011', 'head_after': 'agata0012',
             'migration_sha256': 'b' * 64,
             'agata_image': 'ghcr.io/akhripunov/gpoint-agata-runtime@sha256:' + 'c' * 64}
    path.write_text(json.dumps(value)); path.chmod(0o600)
    return path, value


def test_receipt_is_optional_until_a_separate_migration_was_applied(tmp_path):
    assert MODULE.read_applied_agata_receipt(tmp_path / 'absent.json', 'a' * 40, 'agata0011') == ''


def test_root_private_exact_candidate_receipt_exposes_only_one_checksum(tmp_path):
    path, _ = receipt(tmp_path)
    assert MODULE.read_applied_agata_receipt(path, 'a' * 40, 'agata0012') == 'agata0012=' + 'b' * 64


@pytest.mark.parametrize('field,value', [('source_sha', 'd' * 40), ('head_before', 'agata0010'),
    ('head_after', 'agata0013'), ('status', 'NOT_RUN'), ('migration_sha256', 'bad'),
    ('agata_image', 'ghcr.io/akhripunov/gpoint-agata-runtime:latest')])
def test_receipt_cannot_claim_another_source_head_status_or_mutable_image(tmp_path, field, value):
    path, payload = receipt(tmp_path); payload[field] = value; path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        MODULE.read_applied_agata_receipt(path, 'a' * 40, 'agata0012')


def test_receipt_rejects_unsafe_mode_and_symlinks(tmp_path):
    path, _ = receipt(tmp_path); path.chmod(0o644)
    with pytest.raises(ValueError): MODULE.read_applied_agata_receipt(path, 'a' * 40, 'agata0012')
    path.chmod(0o600); link = tmp_path / 'link'; link.symlink_to(path)
    with pytest.raises(ValueError): MODULE.read_applied_agata_receipt(link, 'a' * 40, 'agata0012')
