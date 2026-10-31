#!/usr/bin/env python3
"""Package trusted one-time release tooling; never package application secrets."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
from typing import Optional

REQUIRED_ENV = (
    'CANDIDATE_SHA', 'APP_SOURCE_SHA', 'IMAGE_VERSION', 'GITHUB_RUN_ID',
    'GITHUB_RUN_ATTEMPT', 'GITHUB_ACTOR', 'KAMAL_REGISTRY_PASSWORD',
    'APP_DIGEST', 'AGATA_DIGEST', 'MINIAPP_DIGEST', 'PROXY_DIGEST',
    'BASELINE_PATH', 'BASELINE_SHA', 'NO_EROS_STATE_PATH', 'NO_EROS_STATE_SHA',
    'CADDYFILE_SHA256', 'BACKUP_PATH', 'BACKUP_SHA', 'RESTORE_EVIDENCE_PATH',
    'RESTORE_EVIDENCE_SHA', 'MEDIA_BACKUP_PATH', 'MEDIA_BACKUP_SHA',
    'MEDIA_BACKUP_BYTES', 'MEDIA_FILE_COUNT', 'MEDIA_DIRECTORY_COUNT',
    'MEDIA_TOTAL_BYTES', 'MEDIA_SOURCE_MANIFEST_SHA', 'MEDIA_RESTORE_MANIFEST_SHA',
    'DEPLOYMENT_ID',
)
HELPERS = (
    'pause-legacy-gpoint.sh', 'retire-gpoint-caddy.sh', 'gpoint-media-tree.py',
    'collect_kamal_eros_state.py', 'kamal_eros_state.py',
    'sanitize-kamal-inventory.py', 'sanitize-kamal-resources.py',
    'write-kamal-production-manifest.py', 'recover-kamal-cutover.py',
    'kamal_proxy_routes.py',
)
PHASES = ('pause', 'retire', 'requests', 'routes', 'consumers', 'verify',
          'recover-pre', 'recover-post')


def _git_run(root: Path, *args: str, input: Optional[bytes] = None,
             check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ['git', '-C', str(root), *args], input=input, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=check,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError('candidate Git workspace is unavailable or invalid') from exc


def _git_workspace_entries(root: Path, candidate_sha: str) -> dict[str, tuple[bytes, int]]:
    """Build a credential-free, shallow Git context with the real candidate commit and trees."""
    git_dir = root / '.git'
    try:
        git_dir.lstat()
    except OSError as exc:
        raise ValueError('candidate Git workspace is missing .git') from exc
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise ValueError('candidate Git workspace has unsafe .git')

    top_level = Path(_git_run(root, 'rev-parse', '--show-toplevel').stdout.decode().strip()).resolve()
    actual_head = _git_run(root, 'rev-parse', '--verify', 'HEAD^{commit}').stdout.decode().strip()
    if top_level != root.resolve() or actual_head != candidate_sha:
        raise ValueError('candidate Git HEAD does not match requested SHA')

    with tempfile.TemporaryDirectory(prefix='gpoint-kamal-git-') as temporary:
        minimal_git = Path(temporary) / 'gitdir'
        subprocess.run(['git', 'init', '--quiet', '--bare', str(minimal_git)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pack = _git_run(root, 'pack-objects', '--stdout', '--filter=blob:none', '--revs',
                        input=(candidate_sha + '\n').encode())
        indexed = subprocess.run(
            ['git', '--git-dir', str(minimal_git), 'index-pack', '--stdin', '--promisor'],
            input=pack.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        pack_hash = indexed.stdout.decode().strip().split()[-1]
        pack_index = minimal_git / 'objects' / 'pack' / f'pack-{pack_hash}.idx'
        verified = subprocess.run(
            ['git', '--git-dir', str(minimal_git), 'verify-pack', '-v', str(pack_index)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        object_rows = [line.split() for line in verified.stdout.decode().splitlines()
                       if len(line.split()) >= 2 and line.split()[1] in {'commit', 'tree', 'blob', 'tag'}]
        if (not any(row[0] == candidate_sha and row[1] == 'commit' for row in object_rows)
                or any(row[1] not in {'commit', 'tree'} for row in object_rows)):
            raise ValueError('candidate Git pack is missing its commit or contains non-tree objects')

        entries: dict[str, tuple[bytes, int]] = {
            'repo/.git/HEAD': ((candidate_sha + '\n').encode(), 0o600),
            'repo/.git/refs/heads/candidate': ((candidate_sha + '\n').encode(), 0o600),
            'repo/.git/config': (
                b'[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n', 0o600,
            ),
        }
        parents = _git_run(root, 'rev-list', '--parents', '-n', '1', candidate_sha).stdout.decode().split()
        if len(parents) > 1:
            entries['repo/.git/shallow'] = ((candidate_sha + '\n').encode(), 0o600)
        pack_dir = minimal_git / 'objects' / 'pack'
        pack_stem = f'pack-{pack_hash}'
        for suffix in ('.pack', '.idx', '.promisor'):
            path = pack_dir / f'{pack_stem}{suffix}'
            if not path.is_file() or path.is_symlink():
                raise ValueError('candidate Git pack is incomplete')
            entries[f'repo/.git/objects/pack/{pack_stem}{suffix}'] = (path.read_bytes(), 0o600)
        return entries


def package(root: Path, archive: Path, source_env: dict[str, str]) -> str:
    if any(not isinstance(source_env.get(key), str) or not source_env[key] for key in REQUIRED_ENV):
        raise ValueError('required private cutover input missing')
    sha, run, attempt = (source_env[k] for k in ('CANDIDATE_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'))
    if (not re.fullmatch('[0-9a-f]{40}', sha)
            or not re.fullmatch('[0-9]{1,20}', run)
            or not re.fullmatch('[1-9][0-9]{0,5}', attempt)
            or not re.fullmatch('[0-9a-f]{40}', source_env['APP_SOURCE_SHA'])
            or source_env['IMAGE_VERSION'] != f'{sha}-{run}-{attempt}'):
        raise ValueError('invalid cutover identity')
    stage = f'/run/gpoint-kamal-owner/{sha}-{run}-{attempt}'
    values = {key: source_env[key] for key in REQUIRED_ENV}
    values.update({
        'HOME': stage+'/home', 'RUNNER_TEMP': stage+'/tmp',
        'GITHUB_WORKSPACE': stage+'/repo', 'GITHUB_STEP_SUMMARY': stage+'/github-summary',
        'GITHUB_OUTPUT': stage+'/github-output',
        'GPOINT_COMMIT_SHA': sha, 'GPOINT_APP_SOURCE_SHA': source_env['APP_SOURCE_SHA'],
        'GPOINT_PRODUCTION_HOST': '127.0.0.1',
        'GPOINT_SSH_KEY': stage+'/home/.ssh/id_ed25519',
        'GPOINT_KNOWN_HOSTS_FILE': stage+'/home/.ssh/known_hosts',
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    entries: dict[str, tuple[bytes, int]] = {}
    git_sources: list[Path] = []

    def source(path: Path, target: str, mode: int = 0o600):
        current = path
        while current != root:
            if current.is_symlink():
                raise ValueError('symlink in trusted cutover source')
            if root not in current.parents:
                raise ValueError('source escaped repository')
            current = current.parent
        if not path.is_file():
            raise ValueError('required release helper missing')
        git_sources.append(path)
        entries[target] = (path.read_bytes(), mode)

    for helper in HELPERS:
        source(root/'scripts'/helper, 'repo/scripts/'+helper)
    for helper in ('kamal_cutover_owner.py', 'kamal_cutover_credentials.py'):
        source(root/'scripts'/helper, helper)
    for phase in PHASES:
        source(root/'scripts/kamal-first-cutover'/f'{phase}.sh', f'phases/{phase}.sh')
    for name in ('deploy.yml', 'deploy.agata.yml', 'deploy.miniapp.yml'):
        source(root/'config'/name, 'repo/config/'+name)
    source(root/'.kamal/secrets', 'repo/.kamal/secrets')
    for path in sorted((root/'.kamal/hooks').iterdir()):
        source(path, 'repo/.kamal/hooks/'+path.name, 0o700)
    for path in git_sources:
        relative = path.relative_to(root).as_posix()
        tracked = _git_run(root, 'ls-files', '--error-unmatch', '--', relative)
        if tracked.stdout.decode().strip() != relative:
            raise ValueError('release helper is not tracked by the candidate commit')
        if _git_run(root, 'diff', '--quiet', 'HEAD', '--', relative, check=False).returncode != 0:
            raise ValueError('release helper content differs from the candidate commit')
    entries.update(_git_workspace_entries(root, sha))
    entries['environment.json'] = (json.dumps(values, sort_keys=True).encode(), 0o600)
    manifest = {'schema': 'gpoint-kamal-owner/v1', 'candidateSha': sha,
                'runId': run, 'attempt': attempt, 'environmentFile': 'environment.json',
                'files': {name: hashlib.sha256(raw).hexdigest() for name, (raw, _) in entries.items()}}
    manifest_raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    entries['manifest.json'] = (manifest_raw, 0o600)
    fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as output, tarfile.open(fileobj=output, mode='w') as tar:
        for name, (raw, mode) in sorted(entries.items()):
            item = tarfile.TarInfo(name)
            item.mode, item.size = mode, len(raw)
            item.uid = item.gid = 0
            tar.addfile(item, io.BytesIO(raw))
    return hashlib.sha256(manifest_raw).hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(package(Path(__file__).resolve().parents[1], args.output, dict(os.environ)))
