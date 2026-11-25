import importlib.util
import json
from pathlib import Path
import re

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'docker_dependency_manifests.py'
spec = importlib.util.spec_from_file_location('docker_dependency_manifests', SCRIPT)
manifests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifests)


def test_build_script_changes_do_not_invalidate_dependency_manifest():
    source = {'name': '@gpoint/bot', 'dependencies': {'grammy': '^1.35.0'},
              'scripts': {'build': 'tsc -p tsconfig.build.json'}}
    changed = {**source, 'scripts': {'build': 'tsc -p tsconfig.build.json && cp helper.js dist/helper.js'}}

    assert manifests.dependency_projection(source) == manifests.dependency_projection(changed)


def test_dependency_or_install_lifecycle_change_invalidates_projection():
    source = {'name': '@gpoint/bot', 'dependencies': {'grammy': '^1.35.0'},
              'scripts': {'postinstall': 'node install.js', 'build': 'tsc'}}

    dependency_change = {**source, 'dependencies': {'grammy': '^1.36.0'}}
    lifecycle_change = {**source, 'scripts': {'postinstall': 'node install-v2.js', 'build': 'tsc'}}

    assert manifests.dependency_projection(source) != manifests.dependency_projection(dependency_change)
    assert manifests.dependency_projection(source) != manifests.dependency_projection(lifecycle_change)


def test_context_contains_exact_candidate_workspace_manifests(tmp_path):
    root = tmp_path / 'repo'
    for relative in manifests.MANIFEST_PATHS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({'name': relative, 'scripts': {'build': 'build'}}))
    output = tmp_path / 'deps'

    result = manifests.write_context(root, output)

    assert result == len(manifests.MANIFEST_PATHS)
    assert {p.relative_to(output).as_posix() for p in output.rglob('package.json')} == set(manifests.MANIFEST_PATHS)
    for relative in manifests.MANIFEST_PATHS:
        data = json.loads((output / relative).read_text())
        assert 'build' not in data.get('scripts', {})


def test_generated_projection_matches_exact_candidate_source_manifests(tmp_path):
    repo = SCRIPT.parents[1]
    output = tmp_path / 'deps'

    manifests.write_context(repo, output)

    for relative in manifests.MANIFEST_PATHS:
        source = json.loads((repo / relative).read_text())
        projected = json.loads((output / relative).read_text())
        assert projected == manifests.dependency_projection(source)
    assert 'packages/agata-client/package.json' not in manifests.MANIFEST_PATHS


def test_dockerfile_named_context_inventory_matches_projection_inventory():
    dockerfile = SCRIPT.parents[1] / 'infra/production/Gpoint.Dockerfile'
    docker_source = dockerfile.read_text()
    copies_by_stage = {}
    current_stage = None
    for line in docker_source.splitlines():
        stage = re.match(r'^FROM\s+\S+\s+AS\s+(\S+)$', line, re.IGNORECASE)
        if stage:
            current_stage = stage.group(1)
            copies_by_stage.setdefault(current_stage, [])
        copy = re.match(r'^COPY --from=dependency-manifests /([^\s]+) ([^\s]+)$', line)
        if copy:
            copies_by_stage[current_stage].append(copy.groups())

    for stage in ('build', 'miniapp-build'):
        copies = copies_by_stage[stage]
        assert tuple(source for source, _ in copies) == manifests.MANIFEST_PATHS
        assert tuple(destination.removeprefix('./') for _, destination in copies) == manifests.MANIFEST_PATHS
        stage_start = docker_source.index(f' AS {stage}')
        stage_end = next((docker_source.index(f' AS {name}', stage_start + 1)
                          for name in ('build', 'miniapp-build', 'runtime', 'miniapp-runtime')
                          if name != stage and docker_source.find(f' AS {name}', stage_start + 1) >= 0),
                         len(docker_source))
        stage_source = docker_source[stage_start:stage_end]
        lockfile_input = stage_source.index('COPY pnpm-lock.yaml pnpm-workspace.yaml ./')
        install = stage_source.index('pnpm install --')
        assert lockfile_input < install
        assert all(stage_source.index(f'COPY --from=dependency-manifests /{relative} ') < install
                   for relative in manifests.MANIFEST_PATHS)


def test_miniapp_target_does_not_depend_on_backend_compilation():
    dockerfile = SCRIPT.parents[1] / 'infra/production/Gpoint.Dockerfile'
    lines = dockerfile.read_text().splitlines()
    stages = {}
    current = None
    for line in lines:
        match = re.match(r'FROM\s+\S+(?:\s+AS\s+(\S+))?$', line, re.IGNORECASE)
        if match:
            current = match.group(1) or f'stage-{len(stages)}'
            stages[current] = {'dependencies': set(), 'instructions': []}
            continue
        if current is None:
            continue
        stages[current]['instructions'].append(line)
        source = re.match(r'COPY\s+--from=(\S+)\s+', line, re.IGNORECASE)
        if source:
            stages[current]['dependencies'].add(source.group(1))

    def dependency_closure(stage):
        found, pending = set(), list(stages[stage]['dependencies'])
        while pending:
            dependency = pending.pop()
            if dependency in found or dependency not in stages:
                continue
            found.add(dependency)
            pending.extend(stages[dependency]['dependencies'])
        return found

    assert 'build' not in dependency_closure('miniapp-runtime')
    miniapp_instructions = '\n'.join(stages['miniapp-build']['instructions'])
    assert 'pnpm install --filter @gpoint/miniapp --frozen-lockfile' in miniapp_instructions
    assert 'COPY packages/contracts/src ./packages/contracts/src' in miniapp_instructions
    assert 'pnpm --filter @gpoint/miniapp build' in miniapp_instructions
    assert 'pnpm --filter @gpoint/api build' not in miniapp_instructions
    assert 'pnpm --filter @gpoint/worker build' not in miniapp_instructions
    assert 'pnpm --filter @gpoint/bot build' not in miniapp_instructions


def test_workspace_manifests_are_accounted_for_by_production_subset():
    repo = SCRIPT.parents[1]
    workspace_manifests = {
        path.relative_to(repo).as_posix()
        for group in ('apps', 'packages')
        for path in (repo / group).glob('*/package.json')
    }

    assert workspace_manifests == (
        set(manifests.MANIFEST_PATHS) - {'package.json'}
    ) | set(manifests.PRODUCTION_WORKSPACE_MANIFEST_EXCLUSIONS)
    assert manifests.PRODUCTION_WORKSPACE_MANIFEST_EXCLUSIONS == (
        'packages/gpoint-codex-mcp/package.json',
    )
    for path in workspace_manifests - set(manifests.PRODUCTION_WORKSPACE_MANIFEST_EXCLUSIONS):
        manifest = json.loads((repo / path).read_text())
        for section in ('dependencies', 'devDependencies', 'optionalDependencies'):
            assert '@gpoint/codex-mcp' not in manifest.get(section, {})


def test_context_refuses_in_repo_output_and_symlinked_manifest(tmp_path):
    root = tmp_path / 'repo'
    for relative in manifests.MANIFEST_PATHS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({'name': relative}))

    with pytest.raises(ValueError, match='outside the source repository'):
        manifests.write_context(root, root / 'generated')

    target = root / manifests.MANIFEST_PATHS[0]
    real = root / 'real-package.json'
    target.unlink()
    real.write_text('{"name":"safe"}')
    target.symlink_to(real)
    with pytest.raises(ValueError, match='regular file'):
        manifests.write_context(root, tmp_path / 'deps')


def test_api_build_output_test_is_not_excluded_from_docker_context():
    dockerignore = SCRIPT.parents[1] / '.dockerignore'
    lines = dockerignore.read_text().splitlines()

    assert '!apps/api/test/' in lines
    assert '!apps/api/test/build-output.test.mjs' in lines
