#!/usr/bin/env python3
"""Validate BuildKit SPDX and exact remote-Git source provenance."""

from __future__ import annotations

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


def _source_materials(slsa: dict[str, Any]) -> list[dict[str, Any]]:
    payload = slsa.get('predicate') if isinstance(slsa.get('predicate'), dict) else slsa
    if not isinstance(payload, dict):
        return []
    materials = payload.get('materials')
    build_definition = payload.get('buildDefinition')
    if isinstance(build_definition, dict):
        resolved = build_definition.get('resolvedDependencies')
        if isinstance(resolved, list):
            materials = (materials if isinstance(materials, list) else []) + resolved
    return [item for item in materials if isinstance(item, dict)] if isinstance(materials, list) else []


def _matches_git_source(item: dict[str, Any], sha: str, expected_repo: str) -> bool:
    source = urlsplit(str(item.get('uri', '')))
    digest = item.get('digest')
    expected = urlsplit(expected_repo)
    return (source.scheme in {'https', 'git+https'}
            and expected.scheme == 'https'
            and source.netloc.lower() == expected.netloc.lower()
            and source.hostname == expected.hostname
            and source.path == expected.path
            and not source.query
            and isinstance(digest, dict)
            and digest.get('sha1') == sha)


def verify_provenance_material(provenance: Any, sha: str, expected_repo: str) -> None:
    if not isinstance(provenance, dict) or not isinstance(provenance.get('SLSA'), dict):
        raise ValueError('published image provenance has no SLSA statement')
    if len(sha) != 40 or any(char not in '0123456789abcdef' for char in sha):
        raise ValueError('required source material SHA is not an exact lowercase Git SHA')
    materials = _source_materials(provenance['SLSA'])
    if not any(_matches_git_source(material, sha, expected_repo) for material in materials):
        raise ValueError('published provenance must bind the required remote Git material to its exact SHA')


def verify_provenance_identity(provenance: Any, sha: str, expected_repo: str,
                               required_material_sha: str | None = None) -> None:
    if not isinstance(provenance, dict) or not isinstance(provenance.get('SLSA'), dict):
        raise ValueError('published image provenance has no SLSA statement')
    if len(sha) != 40 or any(char not in '0123456789abcdef' for char in sha):
        raise ValueError('candidate SHA is not an exact lowercase Git SHA')
    configurations = _source_configurations(provenance['SLSA'])
    if len(configurations) != 1:
        raise ValueError('published provenance must contain exactly one Git source configuration')
    for config in configurations:
        if _matches_git_source(config, sha, expected_repo):
            if required_material_sha is not None:
                verify_provenance_material(provenance, required_material_sha, expected_repo)
            return
    raise ValueError('published provenance must bind the exact SHA to the expected remote Git repository')


def verify_build_args(provenance: Any, expected: dict[str, str]) -> None:
    """Require selected BuildKit args to be present in max-level SLSA v1 provenance."""
    if not isinstance(provenance, dict) or not isinstance(provenance.get('SLSA'), dict):
        raise ValueError('published image provenance has no SLSA statement')
    slsa = provenance['SLSA']
    payload = slsa.get('predicate') if isinstance(slsa.get('predicate'), dict) else slsa
    build_definition = payload.get('buildDefinition') if isinstance(payload, dict) else None
    external = build_definition.get('externalParameters') if isinstance(build_definition, dict) else None
    request = external.get('request') if isinstance(external, dict) else None
    args = request.get('args') if isinstance(request, dict) else None
    if not isinstance(args, dict):
        raise ValueError('published provenance is missing SLSA v1 build arguments')
    for name, value in expected.items():
        if args.get('build-arg:' + name) != value:
            raise ValueError(f'published provenance build argument {name} does not match the expected value')


def main(argv: list[str]) -> int:
    if len(argv) not in (5, 7, 9) or (len(argv) >= 7 and argv[5] != '--required-material-sha') or (len(argv) == 9 and argv[7] != '--required-build-arg'):
        raise SystemExit('usage: verify_native_image_attestation.py SBOM_JSON PROVENANCE_JSON SHA REPOSITORY_URL [--required-material-sha SHA [--required-build-arg NAME=VALUE]]')
    sbom_path, provenance_path, sha, repository = argv[1:5]
    required_material_sha = argv[6] if len(argv) in (7, 9) else None
    sbom = json.loads(Path(sbom_path).read_text(encoding='utf-8'))
    provenance = json.loads(Path(provenance_path).read_text(encoding='utf-8'))
    verify_sbom(sbom)
    verify_provenance_identity(provenance, sha, repository, required_material_sha)
    if len(argv) == 9:
        name, separator, value = argv[8].partition('=')
        if not separator or not name:
            raise SystemExit('--required-build-arg must be NAME=VALUE')
        verify_build_args(provenance, {name: value})
    print('IMAGE_ATTESTATIONS=PASS exact-source-revision=' + sha)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
