#!/usr/bin/env python3
"""Read-only, fail-closed identity check for the installed six-role Kamal baseline."""

from __future__ import annotations

import json
import re
import subprocess
import sys

ROLES = {
    "gpoint-core-api": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-core-worker": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-core-bot": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-agata-api": "ghcr.io/akhripunov/gpoint-agata-runtime",
    "gpoint-agata-memory-worker": "ghcr.io/akhripunov/gpoint-agata-runtime",
    "gpoint-miniapp-web": "ghcr.io/akhripunov/gpoint-miniapp",
}
SHA40 = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9a-f]{40}-[0-9]+-[0-9]+$")
REPO_DIGEST = re.compile(r"^ghcr\.io/akhripunov/[a-z0-9-]+@sha256:[0-9a-f]{64}$")


def validate(records: list[dict]) -> dict:
    if len(records) != len(ROLES) or {r.get("role") for r in records} != set(ROLES):
        raise ValueError("running Kamal role set is incomplete or duplicated")
    versions, sources, result, images_by_repo = set(), set(), {}, {}
    for row in records:
        role = row["role"]
        name, version = row.get("name"), row.get("version")
        source, image_id = row.get("revision"), row.get("image_id")
        repo_digests = row.get("repo_digests")
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            raise ValueError(f"Kamal version is missing or malformed for {role}")
        if name != role + "-" + version:
            raise ValueError(f"invalid Kamal version/name for {role}")
        if not SHA40.fullmatch(source or "") or not IMAGE_ID.fullmatch(image_id or ""):
            raise ValueError(f"invalid OCI source SHA or Docker image ID for {role}")
        repo = ROLES[role]
        matching = [x for x in (repo_digests or []) if REPO_DIGEST.fullmatch(x or "") and x.startswith(repo + "@sha256:")]
        if len(matching) != 1:
            raise ValueError(f"expected one immutable GHCR RepoDigest for {role}")
        identity = (image_id, matching[0])
        if repo in images_by_repo and images_by_repo[repo] != identity:
            if images_by_repo[repo][0] != image_id:
                raise ValueError(f"roles for {repo} have different Docker image IDs")
            raise ValueError(f"roles for {repo} have different image digests")
        images_by_repo[repo] = identity
        versions.add(version)
        sources.add(source)
        result[role] = {"name": name, "version": version, "source_sha": source,
                        "image_id": image_id, "repo_digest": matching[0]}
    if len(versions) != 1:
        raise ValueError("running Kamal roles have mixed release versions")
    if len(sources) != 1:
        raise ValueError("running Kamal roles have mixed source revisions")
    return {"version": next(iter(versions)), "source_sha": next(iter(sources)), "roles": result}


def inspect_running_roles() -> list[dict]:
    rows = []
    for role in ROLES:
        found = subprocess.run(["docker", "ps", "--no-trunc", "--format", "{{.Names}}"],
                              check=True, text=True, capture_output=True).stdout.splitlines()
        matches = [name for name in found if name.startswith(role + "-")]
        if len(matches) != 1:
            raise ValueError(f"expected exactly one running Kamal role: {role}")
        name = matches[0]
        inspected = json.loads(subprocess.run(["docker", "inspect", name], check=True,
                                text=True, capture_output=True).stdout)[0]
        image_id = inspected["Image"]
        image = json.loads(subprocess.run(["docker", "image", "inspect", image_id], check=True,
                             text=True, capture_output=True).stdout)[0]
        version = name[len(role) + 1:]
        rows.append({"role": role, "name": name, "version": version,
                     "image_id": image_id,
                     "revision": image.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision"),
                     "repo_digests": image.get("RepoDigests") or []})
    return rows


if __name__ == "__main__":
    try:
        print(json.dumps(validate(inspect_running_roles()), sort_keys=True, separators=(",", ":")))
    except (ValueError, KeyError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"KAMAL_BASELINE=FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
