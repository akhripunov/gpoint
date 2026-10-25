#!/usr/bin/env python3
"""Validate BuildKit SPDX and exact remote-Git source provenance."""

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def verify_sbom(sbom: Any) -> None:
    packages = sbom.get('packages') if isinstance(sbom, dict) else None
    if not isinstance(packages, list) or not packages:
        raise ValueError('published image SBOM is missing or empty')
    if any(not isinstance(package, dict)
           or not isinstance(package.get('name'), str) or not package['name'].strip()
           or not isinstance(package.get('SPDXID'), str) or not package['SPDXID'].strip()
           for package in packages):
        raise ValueError('published image SBOM contains a malformed SPDX package')


def _source_configurations(slsa: dict[str, Any]) -> list[dict[str, Any]]:
    payload = slsa.get('predicate') if isinstance(slsa.get('predicate'), dict) else slsa
    candidates: list[dict[str, Any]] = []
    build_definition = payload.get('buildDefinition') if isinstance(payload, dict) else None
    if isinstance(build_definition, dict):
        external = build_definition.get('externalParameters')
        if isinstance(external, dict) and isinstance(external.get('configSource'), dict):
            candidates.append(external['configSource'])
    invocation = payload.get('invocation') if isinstance(payload, dict) else None
    if isinstance(invocation, dict) and isinstance(invocation.get('configSource'), dict):
        candidates.append(invocation['configSource'])
    return candidates


def verify_provenance_identity(provenance: Any, sha: str, expected_repo: str) -> None:
    if not isinstance(provenance, dict) or not isinstance(provenance.get('SLSA'), dict):
        raise ValueError('published image provenance has no SLSA statement')
    if len(sha) != 40 or any(char not in '0123456789abcdef' for char in sha):
        raise ValueError('candidate SHA is not an exact lowercase Git SHA')
    expected = urlsplit(expected_repo)
    configurations = _source_configurations(provenance['SLSA'])
    if len(configurations) != 1:
        raise ValueError('published provenance must contain exactly one Git source configuration')
    for config in configurations:
        source = urlsplit(str(config.get('uri', '')))
        digest = config.get('digest')
        if (source.scheme in {'https', 'git+https'}
                and expected.scheme == 'https'
                and source.netloc.lower() == expected.netloc.lower()
                and source.hostname == expected.hostname
                and source.path == expected.path
                and not source.query
                and isinstance(digest, dict)
                and digest.get('sha1') == sha):
            return
    raise ValueError('published provenance must bind the exact SHA to the expected remote Git repository')


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        raise SystemExit('usage: verify_native_image_attestation.py SBOM_JSON PROVENANCE_JSON SHA REPOSITORY_URL')
    sbom_path, provenance_path, sha, repository = argv[1:]
    sbom = json.loads(Path(sbom_path).read_text(encoding='utf-8'))
    provenance = json.loads(Path(provenance_path).read_text(encoding='utf-8'))
    verify_sbom(sbom)
    verify_provenance_identity(provenance, sha, repository)
    print('IMAGE_ATTESTATIONS=PASS exact-source-revision=' + sha)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
