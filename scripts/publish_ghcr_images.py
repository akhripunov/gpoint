#!/usr/bin/env python3
"""Publish already-tested GPoint images to GHCR and attest exact manifest digests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

SHA40 = re.compile(r'^[a-f0-9]{40}$')
DIGEST = re.compile(r'^sha256:[a-f0-9]{64}$')
SOURCE_URL = 'https://github.com/akhripunov/gpoint'
IMAGES = {
    'app': ('gpoint-runtime', 'ghcr.io/akhripunov/gpoint-runtime'),
    'agata': ('gpoint-agata-runtime', 'ghcr.io/akhripunov/gpoint-agata-runtime'),
    'gsales': ('gpoint-g-sales-runtime', 'ghcr.io/akhripunov/gpoint-g-sales-runtime'),
}


def parse_push_digest(output: str) -> str:
    matches = re.findall(r'\bdigest:\s*(sha256:[a-f0-9]{64})\b', output)
    if len(set(matches)) != 1:
        raise ValueError('GHCR push must return one exact manifest digest')
    return matches[0]


def validate_image_metadata(role: str, repository: str, digest: str, metadata: dict, sha: str) -> dict:
    expected = IMAGES.get(role)
    if expected is None:
        raise ValueError('unexpected GPoint image role')
    if repository != expected[1]:
        raise ValueError(f'GHCR repository mismatch for {role}')
    image_id = metadata.get('Id', '')
    if (not DIGEST.fullmatch(digest) or not DIGEST.fullmatch(image_id)
            or metadata.get('Os') != 'linux' or metadata.get('Architecture') != 'amd64'):
        raise ValueError(f'GHCR image digest/config/platform mismatch for {role}')
    labels = (metadata.get('Config') or {}).get('Labels') or {}
    if (labels.get('org.opencontainers.image.revision') != sha
            or labels.get('org.opencontainers.image.source') != SOURCE_URL):
        raise ValueError(f'GHCR image source labels mismatch for {role}')
    return {'repository': repository, 'digest': digest, 'configDigest': image_id,
            'sourceSha': sha, 'platform': 'linux/amd64'}


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, check=True, text=True,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
                            timeout=900)
    return (result.stdout or '') + (result.stderr or '')


def publish_images(sha: str, run_id: str, attempt: str) -> dict:
    if not SHA40.fullmatch(sha) or not run_id.isdigit() or not attempt.isdigit():
        raise ValueError('invalid exact Prepare identity')
    tag = f'prepare-{sha}-{run_id}-{attempt}'
    records = {}
    for role, (local_name, repository) in IMAGES.items():
        local = f'{local_name}:{sha}'
        remote = f'{repository}:{tag}'
        run('docker', 'tag', local, remote)
        output = run('docker', 'push', remote, capture=True)
        digest = parse_push_digest(output)
        reference = f'{repository}@{digest}'
        run('docker', 'pull', reference, capture=True)
        metadata = json.loads(run('docker', 'image', 'inspect', reference, capture=True))[0]
        records[role] = validate_image_metadata(role, repository, digest, metadata, sha)
    return records


def augment_receipt(receipt: dict, records: dict, sha: str, run_id: str, attempt: str) -> dict:
    if (receipt.get('schema') != 1 or receipt.get('status') != 'READY'
            or receipt.get('candidateSha') != sha or str(receipt.get('runId')) != run_id
            or str(receipt.get('runAttempt')) != attempt):
        raise ValueError('Prepare receipt identity changed before GHCR publication')
    if set(records) != set(IMAGES):
        raise ValueError('GHCR image records are incomplete')
    for role, record in records.items():
        if record.get('sourceSha') != sha or record.get('platform') != 'linux/amd64':
            raise ValueError(f'GHCR image proof is invalid for {role}')
    return {**receipt, 'schema': 3, 'transport': 'ghcr-oci', 'images': records}


def main() -> None:
    sha = os.environ.get('CANDIDATE_SHA', '')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if (os.environ.get('GITHUB_REPOSITORY') != 'akhripunov/gpoint'
            or os.environ.get('GITHUB_SHA') != sha
            or not SHA40.fullmatch(sha)):
        raise RuntimeError('GHCR publication requires exact trusted GPoint workflow source')
    root = Path(os.environ.get('RUNNER_TEMP', '')) / 'gpoint-bundle'
    receipt_path = root / 'receipt.json'
    receipt = json.loads(receipt_path.read_text())
    records = publish_images(sha, run_id, attempt)
    updated = augment_receipt(receipt, records, sha, run_id, attempt)
    temporary = receipt_path.with_suffix('.json.pending')
    temporary.write_text(json.dumps(updated, sort_keys=True) + '\n')
    temporary.replace(receipt_path)
    print('GHCR_IMAGE_PROVENANCE=PASS roles=3 platform=linux/amd64')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not print Docker transport errors, which can include registry metadata.
        print(f'GHCR_IMAGE_PROVENANCE=BLOCKED error={type(error).__name__}', file=sys.stderr)
        raise SystemExit(1)
