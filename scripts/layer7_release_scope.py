#!/usr/bin/env python3
"""Verify and scope an API-prompt-only release against the running Layer7 roles."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

EXACT_SHA = re.compile(r"[0-9a-f]{40}")
EXACT_VERSION = re.compile(r"[0-9a-f]{40}-[0-9]+-[0-9]+")
EXACT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

API = "gpoint-core-api"
BOT = "gpoint-core-bot"
ENGINE = "gpoint-eros-pilot-engine"
RELAY = "gpoint-eros-relay-relay"
REQUIRED_ROLES = (API, "gpoint-core-worker", "gpoint-miniapp-web", ENGINE, RELAY)
RUNTIME = "ghcr.io/akhripunov/gpoint-runtime"
ROLE_REPOSITORIES = {
    API: RUNTIME, "gpoint-core-worker": RUNTIME, BOT: RUNTIME, RELAY: RUNTIME,
    "gpoint-miniapp-web": "ghcr.io/akhripunov/gpoint-miniapp",
    ENGINE: "ghcr.io/akhripunov/gpoint-eros",
}
# These files operate the release. Kamal hooks, configs, app dependencies and
# migration files are intentionally outside this list.
RELEASE_PATHS = {
    ".github/workflows/gpoint-native-release.yml",
    ".github/workflows/gpoint-layer7-staging.yml",
    "scripts/layer7_release_scope.py",
    "scripts/native_release_intent.py",
    "tests/test_layer7_release_scope.py",
    "tests/test_layer7_staging_workflow.py",
    "tests/test_native_build_boundary.py",
    "tests/test_kamal_native_automatic_release.py",
    "tests/test_kamal_release_config.py",
    "tests/test_native_prisma_apply_boundary.py",
    "docs/release/automation.md",
}
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


def command(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True, timeout=30).strip()


def database_query(database, sql):
    return command("docker", "exec", "shared_postgres_1", "sh", "-ec",
                   'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -c "$2"',
                   "sh", database, "SET default_transaction_read_only=on; " + sql)


def collect():
    names = command("docker", "ps", "--format", "{{.Names}}").splitlines()
    roles = {}
    for role in (*REQUIRED_ROLES, BOT):
        matches = [name for name in names if name == role or name.startswith(role + "-")]
        if not matches and role == BOT:
            continue
        if len(matches) != 1:
            raise ValueError(f"Layer7 requires one running {role}")
        name = matches[0]
        fields = command("docker", "inspect", "--format",
                         '{{.Id}}|{{.Image}}|{{.State.Running}}|{{.RestartCount}}|'
                         '{{index .Config.Labels "org.opencontainers.image.revision"}}|'
                         '{{index .Config.Labels "com.gpoint.source.revision"}}|{{.Config.Image}}',
                         name).split("|")
        container, image, running, restarts, revision, eros_revision, configured = fields
        source = eros_revision if role == ENGINE else revision
        repository = ROLE_REPOSITORIES[role]
        refs = json.loads(command("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image))
        digests = [ref[len(repository) + 1:] for ref in refs if ref.startswith(repository + "@")]
        if not EXACT_SHA.fullmatch(source) or running != "true" or len(digests) != 1:
            raise ValueError(f"Layer7 cannot prove running image identity for {role}")
        version = "" if role == RELAY else name[len(role) + 1:]
        expected_ref = f"{repository}@{digests[0]}" if role == RELAY else f"{repository}:{version}"
        if configured != expected_ref:
            raise ValueError(f"Layer7 configured image differs for {role}")
        roles[role] = dict(source=source, container=container, image=image, digest=digests[0],
                           version=version, restarts=restarts)

    owner = database_query("postgres", "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='gpoint_staging'")
    if owner != "gpoint_staging_runtime":
        raise ValueError("Layer7 database owner differs")
    prisma = database_query("gpoint_staging", """
        SELECT CASE WHEN finished_at IS NULL THEN 'FAILED' ELSE migration_name || '=' || checksum END
        FROM public._prisma_migrations WHERE rolled_back_at IS NULL ORDER BY migration_name
    """).splitlines()
    sqlx = database_query("eros", """
        SELECT CASE WHEN success THEN version || '=' || encode(checksum,'hex') ELSE 'FAILED' END
        FROM public._sqlx_migrations ORDER BY version
    """).splitlines()
    database = {}
    for role, name in (("postgres", "shared_postgres_1"), ("redis", "shared_redis_1")):
        identity, running = command("docker", "inspect", "--format", "{{.Id}}|{{.State.Running}}", name).split("|")
        if running != "true":
            raise ValueError(f"Layer7 {role} is stopped")
        database[role] = identity
    result = dict(roles=roles, prisma=prisma, sqlx=sqlx, database=database)
    validate(result)
    return result


def validate(snapshot):
    roles = snapshot["roles"]
    if set(roles) not in (set(REQUIRED_ROLES), {*REQUIRED_ROLES, BOT}):
        raise ValueError("Layer7 role inventory differs")
    for role, row in roles.items():
        if not (EXACT_SHA.fullmatch(row["source"]) and CONTAINER_ID.fullmatch(row["container"])
                and EXACT_DIGEST.fullmatch(row["image"]) and EXACT_DIGEST.fullmatch(row["digest"])):
            raise ValueError("Layer7 role identity is malformed")
        version = row["version"]
        if role != RELAY and not (EXACT_VERSION.fullmatch(version) and version.startswith(row["source"] + "-")):
            raise ValueError("Layer7 role version does not match its source SHA")
    for key, pattern in (("prisma", r"[0-9]{14}_[a-z0-9_]+=[0-9a-f]{64}"),
                         ("sqlx", r"[0-9]+=[0-9a-f]{96}")):
        rows = snapshot[key]
        if (not rows or any(not re.fullmatch(pattern, row) for row in rows)
                or len({row.split("=")[0] for row in rows}) != len(rows)):
            raise ValueError("Layer7 database migration history is malformed")
    if set(snapshot["database"]) != {"postgres", "redis"} or any(
            not CONTAINER_ID.fullmatch(value) for value in snapshot["database"].values()):
        raise ValueError("Layer7 database identity is malformed")


def plan(repo, candidate, snapshot, *, previous=None, verify_schema=False):
    from native_release_intent import LAYER7_API_PATHS
    from prisma_migration_checksums import build_inventory

    validate(snapshot)
    if not EXACT_SHA.fullmatch(candidate):
        raise ValueError("Layer7 candidate is not an exact SHA")
    if previous is not None:
        from native_release_intent import changed_paths

        subprocess.run(["git", "merge-base", "--is-ancestor", previous, candidate], cwd=repo, check=True)
        if changed_paths(previous, candidate, repo=repo) - LAYER7_API_PATHS:
            raise ValueError("Layer7 push changed paths outside the API prompt release scope")
    for source in sorted({row["source"] for row in snapshot["roles"].values()}):
        subprocess.run(["git", "merge-base", "--is-ancestor", source, candidate], cwd=repo, check=True)
        paths = command("git", "diff", "--name-only", "-z", "--no-renames", source, candidate, cwd=repo)
        if set(paths.split("\0")) - LAYER7_API_PATHS - RELEASE_PATHS - {""}:
            raise ValueError("Layer7 candidate includes paths outside the API prompt release scope")
    if verify_schema:
        expected = [f"{name}={digest}" for name, digest in build_inventory(repo / "packages/db/prisma/migrations")]
        if snapshot["prisma"] != expected:
            raise ValueError("Layer7 Prisma ledger differs from candidate migration inventory")
    source = snapshot["roles"][API]["source"]
    changed = command("git", "diff", "--name-only", source, candidate, "--", "apps/api/src/eros-response-prompt.js", cwd=repo)
    return ["CORE"] if changed else []


def verify_runtime(before, after, candidate, version, digest):
    validate(before)
    validate(after)
    if any(before[key] != after[key] for key in ("database", "prisma", "sqlx")):
        raise ValueError("Layer7 database state changed during API-only release")
    before_roles = {role: row for role, row in before["roles"].items() if role != API}
    after_roles = {role: row for role, row in after["roles"].items() if role != API}
    if before_roles != after_roles:
        raise ValueError("Layer7 untouched roles changed during API-only release")
    api = after["roles"][API]
    if (api["source"], api["version"], api["digest"]) != (candidate, version, digest):
        raise ValueError("Layer7 installed API differs from the exact candidate image")
    if api["container"] == before["roles"][API]["container"]:
        raise ValueError("Layer7 API container was not replaced")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("collect")
    planning = sub.add_parser("plan")
    planning.add_argument("candidate")
    planning.add_argument("snapshot", type=Path)
    planning.add_argument("--previous", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("before", type=Path)
    verify.add_argument("after", type=Path)
    verify.add_argument("candidate")
    verify.add_argument("version")
    verify.add_argument("digest")
    args = parser.parse_args()
    if args.mode == "collect":
        print(json.dumps(collect(), sort_keys=True, separators=(",", ":")))
    elif args.mode == "plan":
        print(json.dumps(plan(Path.cwd(), args.candidate, json.loads(args.snapshot.read_text()),
                              previous=args.previous, verify_schema=True)))
    else:
        verify_runtime(json.loads(args.before.read_text()), json.loads(args.after.read_text()),
                       args.candidate, args.version, args.digest)


if __name__ == "__main__":
    main()
