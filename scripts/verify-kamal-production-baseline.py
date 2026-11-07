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
    "gpoint-agata-api": "ghcr.io/akhripunov/gpoint-agata-runtime",
    "gpoint-agata-memory-worker": "ghcr.io/akhripunov/gpoint-agata-runtime",
    "gpoint-miniapp-web": "ghcr.io/akhripunov/gpoint-miniapp",
}
EROS_ROLES = {
    "gpoint-eros-pilot-engine": "ghcr.io/akhripunov/gpoint-eros",
    "gpoint-eros-relay-relay": "ghcr.io/akhripunov/gpoint-runtime",
}
SHA40 = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9a-f]{40}-[0-9]+-[0-9]+$")
REPO_DIGEST = re.compile(r"^ghcr\.io/akhripunov/[a-z0-9-]+@sha256:[0-9a-f]{64}$")


def validate(records: list[dict]) -> dict:
    record_roles = {r.get("role") for r in records}
    if len(records) == len(ROLES) and record_roles == set(ROLES):
        role_repositories = ROLES
    elif len(records) == len(ROLES) + len(EROS_ROLES) and record_roles == set(ROLES) | set(EROS_ROLES):
        role_repositories = {**ROLES, **EROS_ROLES}
    else:
        raise ValueError("running Kamal role set is incomplete or duplicated")
    versions, result, images_by_group, component_identities = set(), {}, {}, {}
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
        repo = role_repositories[role]
        group = ("EROS_RELAY" if role == "gpoint-eros-relay-relay" else
                 "EROS_ENGINE" if role == "gpoint-eros-pilot-engine" else
                 "CORE" if role.startswith("gpoint-core-") else
                 "AGATA" if role.startswith("gpoint-agata-") else "MINIAPP")
        matching = [x for x in (repo_digests or []) if REPO_DIGEST.fullmatch(x or "") and x.startswith(repo + "@sha256:")]
        if len(matching) != 1:
            raise ValueError(f"expected one immutable GHCR RepoDigest for {role}")
        identity = (image_id, matching[0])
        if group in images_by_group and images_by_group[group] != identity:
            if images_by_group[group][0] != image_id:
                raise ValueError(f"roles for {repo} have different Docker image IDs")
            raise ValueError(f"roles for {repo} have different image digests")
        images_by_group[group] = identity
        versions.add(version)
        if group in component_identities and component_identities[group] != (version, source):
            raise ValueError(f"roles for {repo} have mixed release versions or mixed source revisions")
        component_identities[group] = (version, source)
        result[role] = {"name": name, "version": version, "source_sha": source,
                        "image_id": image_id, "repo_digest": matching[0]}
    core_source = result["gpoint-core-api"]["source_sha"]
    return {"version": next(iter(versions)) if len(versions) == 1 else None,
            "source_sha": core_source, "roles": result}


def inspect_running_roles() -> list[dict]:
    rows = []
    found = subprocess.run(["docker", "ps", "--no-trunc", "--format", "{{.Names}}"],
                          check=True, text=True, capture_output=True).stdout.splitlines()
    eros_running = {role: [name for name in found if name.startswith(role + "-")]
                    for role in EROS_ROLES}
    eros_present = {role for role, matches in eros_running.items() if matches}
    if eros_present and eros_present != set(EROS_ROLES):
        raise ValueError("running Kamal role set is incomplete or duplicated")
    role_repositories = {**ROLES, **(EROS_ROLES if eros_present else {})}
    for role in role_repositories:
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
        labels = image.get("Config", {}).get("Labels", {})
        revision = (labels.get("com.gpoint.source.revision") if role == "gpoint-eros-pilot-engine"
                    else labels.get("org.opencontainers.image.revision"))
        rows.append({"role": role, "name": name, "version": version,
                     "image_id": image_id,
                     "revision": revision,
                     "repo_digests": image.get("RepoDigests") or []})
    return rows


if __name__ == "__main__":
    try:
        print(json.dumps(validate(inspect_running_roles()), sort_keys=True, separators=(",", ":")))
    except (ValueError, KeyError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"KAMAL_BASELINE=FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
