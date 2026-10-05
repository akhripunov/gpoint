"""Regression coverage for the disposable AGATA artifact migration rehearsal."""
import importlib.util
import io
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


def test_background_release_stage_runs_while_foreground_work_continues(monkeypatch, tmp_path):
    output = io.StringIO()
    timings = {}
    monkeypatch.setattr(prepare_release.sys, 'stdout', output)
    marker = tmp_path / 'foreground-finished'
    child = (
        'import pathlib, time, sys\n'
        'p = pathlib.Path(sys.argv[1])\n'
        'deadline = time.monotonic() + 3\n'
        'while not p.exists() and time.monotonic() < deadline: time.sleep(0.01)\n'
        'if not p.exists(): raise SystemExit(9)\n'
        'print("BACKGROUND_DONE")\n'
    )
    stage = prepare_release.start_background_stage(
        'parallel_probe',
        (prepare_release.sys.executable, '-c', child, str(marker)),
        timings,
    )
    marker.write_text('done')
    prepare_release.finish_background_stage(stage)

    assert timings['parallel_probe']['status'] == 'PASS'
    assert 'PREPARE_STAGE_START=parallel_probe' in output.getvalue()
    assert 'PREPARE_STAGE=parallel_probe STATUS=PASS' in output.getvalue()
    assert 'BACKGROUND_DONE' in output.getvalue()


def test_background_release_stage_failure_blocks_preparation(monkeypatch):
    monkeypatch.setattr(prepare_release.sys, 'stdout', io.StringIO())
    timings = {}
    stage = prepare_release.start_background_stage(
        'failed_probe', (prepare_release.sys.executable, '-c', 'raise SystemExit(7)'), timings)

    with pytest.raises(prepare_release.subprocess.CalledProcessError) as error:
        prepare_release.finish_background_stage(stage)

    assert error.value.returncode == 7
    assert timings['failed_probe']['status'] == 'FAIL'


def test_background_release_stage_is_reaped_when_foreground_gate_fails(monkeypatch):
    monkeypatch.setattr(prepare_release.sys, 'stdout', io.StringIO())
    timings = {}
    stage = prepare_release.start_background_stage(
        'aborted_probe', (prepare_release.sys.executable, '-c',
                          'import time; time.sleep(30)'), timings)

    prepare_release.abort_background_stage(stage)

    assert stage['process'].poll() is not None
    assert timings['aborted_probe']['status'] == 'FAIL'
