#!/usr/bin/env python3
"""Read-only, fail-closed identity check for the installed Kamal baseline."""

from __future__ import annotations

import json
import re
import subprocess
import sys

ROLES = {
    "gpoint-core-api": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-core-worker": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-core-bot": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-miniapp-web": "ghcr.io/akhripunov/gpoint-miniapp",
}
EROS_ROLES = {
    "gpoint-eros-pilot-engine": "ghcr.io/akhripunov/gpoint-eros",
}
SHA40 = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9a-f]{40}-[0-9]+-[0-9]+$")
REPO_DIGEST = re.compile(r"^ghcr\.io/akhripunov/[a-z0-9-]+@sha256:[0-9a-f]{64}$")


def validate(records: list[dict]) -> dict:
    all_roles = {**ROLES, **EROS_ROLES}
    record_roles = {r.get("role") for r in records}
    if (len(records) != len(record_roles) or not record_roles.issubset(all_roles)
            or not {"gpoint-core-api", "gpoint-core-worker", "gpoint-miniapp-web"}.issubset(record_roles)):
        raise ValueError("running Kamal role set is incomplete or duplicated, or lacks a live Core baseline")
    missing_roles = set(all_roles) - record_roles
    role_repositories = all_roles
    versions, result, images_by_source, identities_by_group = set(), {}, {}, {}
    for row in records:
        role = row["role"]
        name, version = row.get("name"), row.get("version")
        source, image_id = row.get("revision"), row.get("image_id")
        repo_digests = row.get("repo_digests")
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            raise ValueError(f"Kamal version is missing or malformed for {role}")
        if name != role + "-" + version:
            raise ValueError(f"invalid Kamal version/name for {role}")
        if (not SHA40.fullmatch(source or "") or not version.startswith(source + "-")
                or not IMAGE_ID.fullmatch(image_id or "")):
            raise ValueError(f"invalid OCI source SHA or Docker image ID for {role}")
        repo = role_repositories[role]
        group = ("EROS_ENGINE" if role == "gpoint-eros-pilot-engine" else
                 "CORE" if role.startswith("gpoint-core-") else "MINIAPP")
        matching = [x for x in (repo_digests or []) if REPO_DIGEST.fullmatch(x or "") and x.startswith(repo + "@sha256:")]
        if len(matching) != 1:
            raise ValueError(f"expected one immutable GHCR RepoDigest for {role}")
        versions.add(version)
        source_identity = (image_id, matching[0])
        if (group, source) in images_by_source and images_by_source[(group, source)] != source_identity:
            raise ValueError(f"roles for {repo} with the same source SHA have different image identities")
        images_by_source[(group, source)] = source_identity
        identities_by_group.setdefault(group, set()).add((version, source, image_id, matching[0]))
        result[role] = {"name": name, "version": version, "source_sha": source,
                        "image_id": image_id, "repo_digest": matching[0],
                        "started_at": row.get("started_at")}
    core_source = result["gpoint-core-api"]["source_sha"]
    mixed_components = sorted(group for group, identities in identities_by_group.items() if len(identities) > 1)
    return {"version": next(iter(versions)) if len(versions) == 1 else None,
            "source_sha": core_source, "roles": result,
            "missing_roles": sorted(missing_roles), "mixed_components": mixed_components}


def inspect_running_roles() -> list[dict]:
    rows = []
    found = subprocess.run(["docker", "ps", "--no-trunc", "--format", "{{.Names}}"],
                          check=True, text=True, capture_output=True).stdout.splitlines()
    role_repositories = {**ROLES, **EROS_ROLES}
    for role in role_repositories:
        matches = [name for name in found if name.startswith(role + "-")]
        if not matches and role in {"gpoint-core-bot", "gpoint-eros-pilot-engine"}:
            continue
        if len(matches) != 1:
            raise ValueError(f"expected exactly one running Kamal role: {role}")
        name = matches[0]
        inspected = json.loads(subprocess.run(["docker", "inspect", name], check=True,
                                text=True, capture_output=True).stdout)[0]
        image_id = inspected["Image"]
        image = json.loads(subprocess.run(["docker", "image", "inspect", image_id], check=True,
                             text=True, capture_output=True).stdout)[0]
        version = name[len(role) + 1:]
        labels = image.get("Config", {}).get("Labels", {})
        revision = (labels.get("com.gpoint.source.revision") if role == "gpoint-eros-pilot-engine"
                    else labels.get("org.opencontainers.image.revision"))
        rows.append({"role": role, "name": name, "version": version,
                     "image_id": image_id,
                     "started_at": inspected.get("State", {}).get("StartedAt"),
                     "revision": revision,
                     "repo_digests": image.get("RepoDigests") or []})
    return rows


if __name__ == "__main__":
    try:
        rows = inspect_running_roles()
        baseline = validate(rows)
        print(json.dumps(baseline, sort_keys=True, separators=(",", ":")))
    except (ValueError, KeyError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"KAMAL_BASELINE=FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
