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


def test_gsales_postgres_gate_starts_before_agata_postgres_rehearsal():
    source = SCRIPT.read_text()
    initialization = source.index('gsales_regression = None')
    start = source.index("'gsales_postgres_regression',\n")
    postgres = source.index("measured_stage('disposable_postgres_setup'")
    assert initialization < start < postgres


def test_candidate_image_builds_use_exact_platform_and_isolated_reusable_cache_scopes():
    workflow = SCRIPT.parent.parent / '.github/workflows/gpoint-prepare.yml'
    workflow_source = workflow.read_text()
    app = prepare_release.docker_build_command(
        'gpoint-runtime', 'infra/production/Gpoint.Dockerfile', '.', 'a' * 40,
        dependency_manifest_context='/tmp/dependency-manifests')
    agata = prepare_release.docker_build_command(
        'gpoint-agata-runtime', 'services/agata/Dockerfile', 'services/agata', 'a' * 40)

    assert app[:5] == ['docker', 'buildx', 'build', '--platform', 'linux/amd64']
    assert app[5] == '--load'
    assert app[6:8] == ['--build-context', 'dependency-manifests=/tmp/dependency-manifests']
    assert '--label' in app and 'org.opencontainers.image.revision=' + 'a' * 40 in app
    assert 'org.opencontainers.image.source=https://github.com/akhripunov/gpoint' in app
    assert '--cache-from' in app and 'type=gha,scope=gpoint-gpoint-runtime' in app
    assert '--cache-to' in app and 'type=gha,scope=gpoint-gpoint-runtime,mode=max,ignore-error=true' in app
    assert '--build-context' not in agata
    assert 'type=gha,scope=gpoint-gpoint-agata-runtime' in agata
    assert 'crazy-max/ghaction-github-runtime@a1014fef88934970651c7916e89aaf51bc58f38b' in workflow_source
    assert 'docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069' in workflow_source


def test_gpoint_docker_build_rejects_missing_or_misapplied_dependency_context():
    with pytest.raises(ValueError, match='requires dependency manifest context'):
        prepare_release.docker_build_command(
            'gpoint-runtime', 'infra/production/Gpoint.Dockerfile', '.', 'a' * 40)
    with pytest.raises(ValueError, match='only valid for GPoint runtime'):
        prepare_release.docker_build_command(
            'gpoint-agata-runtime', 'services/agata/Dockerfile', 'services/agata', 'a' * 40,
            dependency_manifest_context='/tmp/unused')
    with pytest.raises(ValueError, match='exact'):
        prepare_release.docker_build_command(
            'gpoint-runtime', 'infra/production/Gpoint.Dockerfile', '.', 'mutable',
            dependency_manifest_context='/tmp/dependency-manifests')


def test_prepare_builds_node_runtime_even_when_dockerfile_ends_with_miniapp():
    dockerfile = SCRIPT.parent.parent / 'infra/production/Gpoint.Dockerfile'
    assert dockerfile.read_text().rfind('AS miniapp-runtime') > dockerfile.read_text().rfind('AS runtime')
    command = prepare_release.docker_build_command(
        'gpoint-runtime', str(dockerfile), '.', 'a' * 40,
        dependency_manifest_context='/tmp/dependency-manifests')
    assert '--target' in command
    assert command[command.index('--target') + 1] == 'runtime'
    agata = prepare_release.docker_build_command(
        'gpoint-agata-runtime', 'services/agata/Dockerfile', 'services/agata', 'a' * 40)
    assert '--target' not in agata


def test_gpoint_build_sha_does_not_invalidate_unrelated_build_layers():
    dockerfile = (SCRIPT.parent.parent / 'infra/production/Gpoint.Dockerfile').read_text()
    sha_env = dockerfile.index('ENV GPOINT_BUILD_SHA=${GPOINT_BUILD_SHA}')
    miniapp_build = dockerfile.index('RUN pnpm --filter @gpoint/miniapp build')

    assert sha_env < miniapp_build
    for unrelated_build in (
            'RUN pnpm --filter @gpoint/db exec prisma generate',
            'RUN pnpm --filter @gpoint/agata-client build',
            'RUN pnpm --filter @gpoint/db build',
            'RUN pnpm --filter @gpoint/api build',
            'RUN pnpm --filter @gpoint/worker build',
            'RUN pnpm --filter @gpoint/bot build'):
        assert dockerfile.index(unrelated_build) < sha_env
