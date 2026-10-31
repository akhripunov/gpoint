#!/usr/bin/env python3
"""Package trusted one-time release tooling; never package application secrets."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile

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
