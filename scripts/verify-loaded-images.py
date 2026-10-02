"""Verify immutable config identity across classic and containerd Docker stores."""
import hashlib
import json
import re
import subprocess
import sys


def digest(value):
    return 'sha256:' + hashlib.sha256(value).hexdigest()


def read(*args):
    return subprocess.check_output(args, timeout=30)


def verify(tag, expected, sha):
    image = json.loads(read('docker', 'image', 'inspect', tag))[0]
    native = image['Id']
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', native):
        raise RuntimeError('invalid native Docker image ID')
    if image.get('Os') != 'linux' or image.get('Architecture') != 'amd64':
        raise RuntimeError('loaded image platform mismatch')
    if image.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.revision') != sha:
        raise RuntimeError('loaded image source SHA mismatch')
    if native != expected:
        # containerd .Id identifies the manifest, whereas classic .Id and the
        # Prepare receipt identify config bytes. Prove the content-addressed
        # manifest -> config relationship; never accept labels alone.
        descriptor = image.get('Descriptor') or {}
        if descriptor.get('digest') != native or descriptor.get('mediaType') not in (
                'application/vnd.oci.image.manifest.v1+json',
                'application/vnd.docker.distribution.manifest.v2+json'):
            raise RuntimeError('loaded image identity mismatch without a single-platform manifest')
        manifest_bytes = read('ctr', '-n', 'moby', 'content', 'get', native)
        if digest(manifest_bytes) != native:
            raise RuntimeError('loaded image manifest checksum mismatch')
        manifest = json.loads(manifest_bytes)
        if manifest.get('schemaVersion') != 2 or manifest.get('config', {}).get('digest') != expected:
            raise RuntimeError(f'loaded image config identity mismatch: expected {expected}, native {native}')
        config_bytes = read('ctr', '-n', 'moby', 'content', 'get', expected)
        if digest(config_bytes) != expected:
            raise RuntimeError('loaded image config checksum mismatch')
        config = json.loads(config_bytes)
        if config.get('os') != 'linux' or config.get('architecture') != 'amd64':
            raise RuntimeError('verified config platform mismatch')
        if config.get('config', {}).get('Labels', {}).get('org.opencontainers.image.revision') != sha:
            raise RuntimeError('verified config source SHA mismatch')
        layers = config.get('rootfs', {}).get('diff_ids')
        if not layers or layers != image.get('RootFS', {}).get('Layers') or len(layers) != len(manifest.get('layers', [])):
            raise RuntimeError('loaded image rootfs differs from verified config')
    print(f'GPOINT_CI_IMAGE_IDENTITY tag={tag} config={expected} native={native}')


def main():
    sha, app, agata = sys.argv[1:]
    if not re.fullmatch(r'[a-f0-9]{40}', sha) or any(not re.fullmatch(r'sha256:[a-f0-9]{64}', value) for value in (app, agata)):
        raise RuntimeError('invalid expected image identity')
    verify(f'gpoint-runtime:{sha}', app, sha)
    verify(f'gpoint-agata-runtime:{sha}', agata, sha)


if __name__ == '__main__':
    main()
