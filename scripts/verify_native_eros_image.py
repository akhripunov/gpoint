#!/usr/bin/env python3
"""Verify the exact stock Eros OCI image and its upstream provenance."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_native_image_attestation import verify_provenance_identity, verify_sbom

UPSTREAM_REPOSITORY = "https://github.com/etherfunlab/eros-engine.git"
UPSTREAM_SHA = "d1a140083321687212fb7b17c09cc0c3c9639f5e"
EROS_IMAGE = "ghcr.io/akhripunov/gpoint-eros"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def verify_image(item: Any, *, digest: str, candidate_sha: str,
                 upstream_sha: str = UPSTREAM_SHA) -> None:
    if isinstance(item, list):
        if len(item) != 1:
            raise ValueError("Eros image inspect must return exactly one image")
        item = item[0]
    if not isinstance(item, dict):
        raise ValueError("Eros image inspect did not return an image object")
    if not OCI_DIGEST.fullmatch(digest or ""):
        raise ValueError("Eros OCI digest is missing or malformed")
    if not SHA40.fullmatch(candidate_sha or "") or not SHA40.fullmatch(upstream_sha or ""):
        raise ValueError("Eros source SHA is missing or malformed")
    labels = item.get("Config", {}).get("Labels") or {}
    expected_labels = {
        "org.opencontainers.image.source": "https://github.com/etherfunlab/eros-engine",
        "org.opencontainers.image.revision": upstream_sha,
        "com.gpoint.source.revision": candidate_sha,
        "com.gpoint.eros.upstream-revision": upstream_sha,
        "com.gpoint.eros.patch-set.sha256": "none",
    }
    if (item.get("Os") != "linux" or item.get("Architecture") != "amd64"
            or EROS_IMAGE + "@" + digest not in item.get("RepoDigests", [])):
        raise ValueError("Eros image digest or linux/amd64 platform mismatch")
    for name, value in expected_labels.items():
        if labels.get(name) != value:
            raise ValueError(f"Eros image source identity mismatch: {name}")


def verify_attestations(sbom: Any, provenance: Any, *, upstream_sha: str = UPSTREAM_SHA) -> None:
    verify_sbom(sbom)
    verify_provenance_identity(provenance, upstream_sha, UPSTREAM_REPOSITORY,
                               required_material_sha=upstream_sha)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_json")
    parser.add_argument("sbom_json")
    parser.add_argument("provenance_json")
    parser.add_argument("digest")
    parser.add_argument("candidate_sha")
    parser.add_argument("--upstream-sha", default=UPSTREAM_SHA)
    args = parser.parse_args()
    verify_image(json.loads(Path(args.image_json).read_text(encoding="utf-8")),
                 digest=args.digest, candidate_sha=args.candidate_sha,
                 upstream_sha=args.upstream_sha)
    verify_attestations(json.loads(Path(args.sbom_json).read_text(encoding="utf-8")),
                        json.loads(Path(args.provenance_json).read_text(encoding="utf-8")),
                        upstream_sha=args.upstream_sha)
    print(f"EROS_STOCK_IMAGE=PASS digest={args.digest} upstream_sha={args.upstream_sha} candidate_sha={args.candidate_sha} platform=linux/amd64 patch_set=none")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"EROS_STOCK_IMAGE=FAIL reason={exc}") from None
