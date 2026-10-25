#!/usr/bin/env python3
"""Fail-closed checks for a pulled Native Release image's identity."""

import json
import sys
from pathlib import Path
from typing import Any


def normalize_image_inspect(value: Any) -> dict[str, Any]:
    """Accept Docker's object output and a single-item JSON array only."""
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError('docker image inspect must return exactly one image')
        value = value[0]
    if not isinstance(value, dict):
        raise ValueError('docker image inspect did not return an image object')
    return value


def verify_image_identity(item: dict[str, Any], image: str, digest: str, sha: str) -> None:
    expected = image + '@' + digest
    labels = item.get('Config', {}).get('Labels') or {}
    if expected not in item.get('RepoDigests', []):
        raise ValueError('pulled image does not resolve to the published digest')
    if (item.get('Os') != 'linux' or item.get('Architecture') != 'amd64'
            or labels.get('org.opencontainers.image.revision') != sha
            or labels.get('org.opencontainers.image.source') != 'https://github.com/akhripunov/gpoint'):
        raise ValueError('published image platform/source identity mismatch')


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        raise SystemExit('usage: verify_native_image_readback.py IMAGE_JSON IMAGE DIGEST SHA')
    path, image, digest, sha = argv[1:]
    item = normalize_image_inspect(json.loads(Path(path).read_text(encoding='utf-8')))
    verify_image_identity(item, image, digest, sha)
    print('IMAGE_READBACK=PASS ' + image + '@' + digest)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
