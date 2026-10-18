#!/usr/bin/env python3
"""Cheap local check of exact candidate image identity without archive transport."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

SHA = os.environ.get('CANDIDATE_SHA', '')
if not re.fullmatch(r'[a-f0-9]{40}', SHA):
    raise SystemExit('CANDIDATE_SHA must be an exact 40-character SHA')

labels = (
    f'org.opencontainers.image.revision={SHA}',
    'org.opencontainers.image.source=https://github.com/akhripunov/gpoint',
)
names = ('gpoint-runtime', 'gpoint-agata-runtime', 'gpoint-g-sales-runtime')
tags = [f'{name}:{SHA}' for name in names]
with tempfile.TemporaryDirectory(prefix='gpoint-image-identity-fixture-') as temporary:
    root = Path(temporary)
    (root / 'marker').write_text(SHA)
    (root / 'Dockerfile').write_text('FROM scratch\nCOPY marker /marker\n')
    try:
        identities = set()
        for tag in tags:
            subprocess.run(['docker', 'build', '--platform', 'linux/amd64',
                            *[arg for label in labels for arg in ('--label', label)],
                            '-t', tag, temporary], check=True, timeout=30)
            image = json.loads(subprocess.run(
                ['docker', 'image', 'inspect', tag], check=True, capture_output=True,
                text=True, timeout=10).stdout)[0]
            config = image.get('Id', '')
            actual_labels = image.get('Config', {}).get('Labels', {})
            if (not re.fullmatch(r'sha256:[a-f0-9]{64}', config)
                    or image.get('Os') != 'linux' or image.get('Architecture') != 'amd64'
                    or actual_labels.get('org.opencontainers.image.revision') != SHA
                    or actual_labels.get('org.opencontainers.image.source') != 'https://github.com/akhripunov/gpoint'):
                raise SystemExit('candidate image config/platform/source identity mismatch')
            identities.add(config)
        if len(identities) != len(tags):
            raise SystemExit('image identity fixture did not produce distinct config IDs')
        print('PREPARE_IMAGE_CONFIG_IDENTITY=PASS roles=3 platform=linux/amd64')
    finally:
        subprocess.run(['docker', 'image', 'rm', *tags], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
