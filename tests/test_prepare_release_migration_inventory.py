"""Regression coverage for the disposable AGATA artifact migration rehearsal."""
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'prepare-release.py'
spec = importlib.util.spec_from_file_location('prepare_release', SCRIPT)
prepare_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_release)


def test_complete_disposable_tenant_inventory_is_forwarded(monkeypatch):
    tenants = '7508f36f-774a-48aa-8c6b-42f6f160415d\n'
    second = '11111111-2222-4333-8444-555555555555'
    monkeypatch.setattr(prepare_release, 'run', lambda *args, **kwargs: tenants + second)

    inventory = prepare_release.disposable_migration_tenant_inventory('ci-postgres', 'runtime_agata_test')

    assert inventory == ','.join(sorted([tenants.strip(), second]))


@pytest.mark.parametrize('output', ['', '\n  ', 'not-a-uuid'])
def test_missing_or_invalid_inventory_fails_closed(monkeypatch, output):
    monkeypatch.setattr(prepare_release, 'run', lambda *args, **kwargs: output)

    with pytest.raises(RuntimeError, match='complete disposable AGATA tenant inventory'):
        prepare_release.disposable_migration_tenant_inventory('ci-postgres', 'runtime_agata_test')
