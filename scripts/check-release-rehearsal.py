#!/usr/bin/env python3
"""Cheap branch-only check of the private daemon and canonical command chain."""
import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

spec = importlib.util.spec_from_file_location('prepare_release', Path(__file__).with_name('prepare-release.py'))
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)

sha = os.environ['CANDIDATE_SHA']
images = {}
with tempfile.TemporaryDirectory(prefix='gpoint-rehearsal-fixture-') as temporary:
    root = Path(temporary)
    (root / 'marker').write_text(sha)
    (root / 'Dockerfile').write_text('FROM scratch\nCOPY marker /marker\n')
    tags = [f'{name}:{sha}' for name in ('gpoint-runtime', 'gpoint-agata-runtime')]
    try:
        for role, tag in zip(('app', 'agata'), tags):
            prepare.run('docker', 'build', '--platform', 'linux/amd64', '--label',
                        f'org.opencontainers.image.revision={sha}', '-t', tag, temporary)
            image = json.loads(prepare.run('docker', 'image', 'inspect', tag, capture=True))[0]
            images[role] = {'id': image['Id'], 'platform': 'linux/amd64', 'revision': sha}
        archive = root / 'images.tar.gz'
        with archive.open('wb') as destination:
            saved = subprocess.run(['docker', 'save', *tags], stdout=subprocess.PIPE, check=True, timeout=30)
            destination.write(gzip.compress(saved.stdout, compresslevel=1))
        prepare.verify_saved_images(archive, images, sha, temporary)
    finally:
        subprocess.run(['docker', 'image', 'rm', *tags], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
