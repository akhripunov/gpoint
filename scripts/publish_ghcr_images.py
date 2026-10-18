#!/usr/bin/env python3
"""Publish already-tested GPoint images to GHCR and attest exact manifest digests."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SHA40 = re.compile(r'^[a-f0-9]{40}$')
DIGEST = re.compile(r'^sha256:[a-f0-9]{64}$')
SOURCE_URL = 'https://github.com/akhripunov/gpoint'
IMAGES = {
    'app': ('gpoint-runtime', 'ghcr.io/akhripunov/gpoint-runtime'),
    'agata': ('gpoint-agata-runtime', 'ghcr.io/akhripunov/gpoint-agata-runtime'),
    'gsales': ('gpoint-g-sales-runtime', 'ghcr.io/akhripunov/gpoint-g-sales-runtime'),
}
RECEIPT_REPOSITORY = 'ghcr.io/akhripunov/gpoint-prepare-receipts'
RECEIPT_ARTIFACT_TYPE = 'application/vnd.gpoint.prepare.receipt.v1'
STATIC_REPOSITORY = 'ghcr.io/akhripunov/gpoint-miniapp-artifacts'
STATIC_ARTIFACT_TYPE = 'application/vnd.gpoint.miniapp.static.v1'


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


def run(*args: str, capture: bool = False, cwd: Path | None = None) -> str:
    result = subprocess.run(args, check=True, text=True,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
                            timeout=900, cwd=cwd)
    return (result.stdout or '') + (result.stderr or '')


def safe_oci_file(path: Path, expected_name: str, media_type: str) -> tuple[str, Path]:
    """Return an ORAS-safe relative file reference and its resolved working directory."""
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts or path.name != expected_name
            or path.is_symlink() or not path.is_file()):
        raise ValueError('unsafe OCI artifact path')
    working_dir = path.parent.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved.parent != working_dir or resolved.name != expected_name or not resolved.is_file():
        raise ValueError('unsafe OCI artifact path')
    return f'{expected_name}:{media_type}', working_dir


def publish_images(sha: str, run_id: str, attempt: str) -> dict:
    if not SHA40.fullmatch(sha) or not run_id.isdigit() or not attempt.isdigit():
        raise ValueError('invalid exact Prepare identity')
    tag = f'prepare-{sha}-{run_id}-{attempt}'
    records = {}
    for role, (local_name, repository) in IMAGES.items():
        local = f'{local_name}:{sha}'
        remote = f'{repository}:{tag}'
        local_metadata = json.loads(run('docker', 'image', 'inspect', local, capture=True))[0]
        local_identity = validate_image_metadata(
            role, repository, local_metadata.get('Id', ''), local_metadata, sha)
        run('docker', 'tag', local, remote)
        output = run('docker', 'push', remote, capture=True)
        digest = parse_push_digest(output)
        reference = f'{repository}@{digest}'
        run('docker', 'pull', reference, capture=True)
        metadata = json.loads(run('docker', 'image', 'inspect', reference, capture=True))[0]
        record = validate_image_metadata(role, repository, digest, metadata, sha)
        if record['configDigest'] != local_identity['configDigest']:
            raise ValueError(f'GHCR readback image config differs from built {role} image')
        records[role] = record
    return records


def validate_prepare_identity(receipt: dict, sha: str, run_id: str, attempt: str) -> None:
    if (receipt.get('schema') not in (1, 2) or receipt.get('status') != 'READY'
            or receipt.get('candidateSha') != sha or str(receipt.get('runId')) != run_id
            or str(receipt.get('runAttempt')) != attempt):
        raise ValueError('Prepare receipt identity changed before GHCR publication')


def augment_receipt(receipt: dict, records: dict, sha: str, run_id: str, attempt: str,
                    static_artifact: dict | None = None) -> dict:
    validate_prepare_identity(receipt, sha, run_id, attempt)
    static_only = receipt.get('route') == 'STATIC_UI_ONLY'
    if static_only:
        if records or not static_artifact:
            raise ValueError('static-only Prepare must publish only its verified Mini App artifact')
    elif set(records) != set(IMAGES):
        raise ValueError('GHCR image records are incomplete')
    for role, record in records.items():
        if record.get('sourceSha') != sha or record.get('platform') != 'linux/amd64':
            raise ValueError(f'GHCR image proof is invalid for {role}')
    # The manifest digest and receipt blob checksum are transport metadata.
    # Embedding either in this blob would be self-referential.
    enriched = {**receipt, 'schema': 3, 'transport': 'ghcr-oci', 'images': records}
    enriched.pop('archiveSha256', None)
    enriched.pop('receiptDigest', None)
    enriched.pop('receiptSha256', None)
    if static_only:
        enriched['gates'] = {**enriched['gates'], 'ghcrStaticReadback': 'PASS'}
    else:
        enriched['gates'] = {**enriched['gates'], 'ghcrImageReadback': 'PASS'}
    if static_artifact:
        enriched['staticArtifact'] = static_artifact
    return enriched


def publish_receipt(receipt_path: Path, sha: str, run_id: str, attempt: str) -> dict:
    """Publish one canonical receipt OCI artifact, then verify it by digest."""
    if not SHA40.fullmatch(sha) or not run_id.isdigit() or not attempt.isdigit():
        raise ValueError('invalid exact Prepare identity')
    file_reference, working_dir = safe_oci_file(
        receipt_path, 'receipt.json', 'application/vnd.gpoint.prepare.receipt.v1+json')
    raw = receipt_path.read_bytes()
    receipt_sha256 = hashlib.sha256(raw).hexdigest()
    receipt = json.loads(raw)
    if (receipt.get('candidateSha') != sha or str(receipt.get('runId')) != run_id
            or str(receipt.get('runAttempt')) != attempt or receipt.get('status') != 'READY'):
        raise ValueError('OCI receipt does not bind exact Prepare identity')
    tag = f'{RECEIPT_REPOSITORY}:prepare-{sha}-{run_id}-{attempt}'
    output = run('oras', 'push', '--artifact-type', RECEIPT_ARTIFACT_TYPE,
                 '--format', 'json', tag, file_reference, capture=True, cwd=working_dir)
    try:
        descriptor = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError('ORAS did not return a valid artifact descriptor') from error
    digest = descriptor.get('digest')
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError('ORAS did not return one exact receipt manifest digest')
    if descriptor.get('reference') not in (None, f'{RECEIPT_REPOSITORY}@{digest}'):
        raise ValueError('ORAS returned an unexpected receipt repository')
    with tempfile.TemporaryDirectory(prefix='gpoint-receipt-readback-') as directory:
        run('oras', 'pull', f'{RECEIPT_REPOSITORY}@{digest}', '--output', directory)
        readback = Path(directory) / receipt_path.name
        if not readback.is_file():
            raise ValueError('OCI receipt readback did not contain receipt.json')
        content = readback.read_bytes()
        if hashlib.sha256(content).hexdigest() != receipt_sha256 or content != raw:
            raise ValueError('OCI receipt readback content mismatch')
    return {'repository': RECEIPT_REPOSITORY, 'digest': digest,
            'contentSha256': receipt_sha256, 'sourceSha': sha,
            'runId': int(run_id), 'runAttempt': int(attempt)}


def publish_static_artifact(bundle_path: Path, receipt: dict, sha: str,
                            run_id: str, attempt: str) -> dict:
    if (receipt.get('staticArtifactName') != 'miniapp.zip'
            or receipt.get('staticArtifactRoute') not in ('V4_FAST_STATIC', 'V4_ADVISORY_ONLY')
            or not Path(bundle_path).is_file()):
        raise ValueError('static Mini App artifact is missing or not approved by V4 scope')
    file_reference, working_dir = safe_oci_file(
        bundle_path, 'miniapp.zip', 'application/vnd.gpoint.miniapp.static.v1+zip')
    content = bundle_path.read_bytes()
    content_sha256 = hashlib.sha256(content).hexdigest()
    if receipt.get('staticArtifactSha256') != content_sha256:
        raise ValueError('static Mini App artifact checksum differs from Prepare receipt')
    tag = f'{STATIC_REPOSITORY}:prepare-{sha}-{run_id}-{attempt}'
    output = run('oras', 'push', '--artifact-type', STATIC_ARTIFACT_TYPE,
                 '--format', 'json', tag,
                 file_reference, capture=True, cwd=working_dir)
    try:
        descriptor = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError('ORAS did not return a valid static artifact descriptor') from error
    digest = descriptor.get('digest')
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError('ORAS did not return one exact static artifact manifest digest')
    with tempfile.TemporaryDirectory(prefix='gpoint-static-readback-') as directory:
        run('oras', 'pull', f'{STATIC_REPOSITORY}@{digest}', '--output', directory)
        readback = Path(directory) / bundle_path.name
        if not readback.is_file():
            raise ValueError('OCI static artifact readback content mismatch')
        readback_content = readback.read_bytes()
        if hashlib.sha256(readback_content).hexdigest() != content_sha256 or readback_content != content:
            raise ValueError('OCI static artifact readback content mismatch')
    return {'repository': STATIC_REPOSITORY, 'digest': digest,
            'contentSha256': content_sha256, 'sourceSha': sha,
            'runId': int(run_id), 'runAttempt': int(attempt)}


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
    validate_prepare_identity(receipt, sha, run_id, attempt)
    if receipt.get('route') == 'STATIC_UI_ONLY':
        records = {}
    else:
        records = publish_images(sha, run_id, attempt)
    static_path = root / 'miniapp.zip'
    if receipt.get('staticArtifactSha256') and not static_path.is_file():
        raise ValueError('Prepare receipt requires a missing Mini App OCI artifact')
    static_artifact = (publish_static_artifact(static_path, receipt, sha, run_id, attempt)
                       if static_path.exists() else None)
    updated = augment_receipt(receipt, records, sha, run_id, attempt, static_artifact)
    temporary = receipt_path.with_suffix('.json.pending')
    temporary.write_text(json.dumps(updated, sort_keys=True, separators=(',', ':')) + '\n')
    temporary.replace(receipt_path)
    receipt_transport = publish_receipt(receipt_path, sha, run_id, attempt)
    output_path = os.environ.get('GITHUB_OUTPUT')
    if output_path:
        with Path(output_path).open('a', encoding='utf-8') as output:
            output.write(f"receipt_digest={receipt_transport['digest']}\n")
            output.write(f"receipt_content_sha256={receipt_transport['contentSha256']}\n")
    print('GHCR_IMAGE_PROVENANCE=PASS roles=' + str(len(records)) + ' platform=linux/amd64')
    print('GHCR_RECEIPT_DIGEST=' + receipt_transport['digest'])
    if static_artifact:
        print('GHCR_STATIC_ARTIFACT_DIGEST=' + static_artifact['digest'])


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not print Docker transport errors, which can include registry metadata.
        print(f'GHCR_IMAGE_PROVENANCE=BLOCKED error={type(error).__name__}', file=sys.stderr)
        raise SystemExit(1)
