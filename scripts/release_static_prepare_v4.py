#!/usr/bin/env python3
"""Exact-SHA fast static artifact for proven Mini App-only source changes.

No database/image mutation and no production apply. Controller, Mac preflight,
OWNER exact-SHA approval and Deploy remain canonical.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from release_component_impact_v4 import verified_plan
from release_ui_artifact_v4 import create_bundle

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[a-f0-9]{40}$")


def _exec(*args, env=None):
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def prepare_static(sha, output, impact):
    if not SHA.fullmatch(sha or "") or not isinstance(impact, dict):
        raise ValueError("invalid static candidate")
    baseline = impact.get("productionBaselineSha")
    if (impact.get("candidateSha") != sha or
            impact.get("version") != 1 or
            impact.get("classification") != "UI_ONLY" or
            impact.get("baselineSource") != "gpoint-release-ledger" or
            impact.get("reason") != "narrow-unchanged-backend" or
            not SHA.fullmatch(baseline or "") or baseline == sha or
            impact.get("checks") != {"agata_postgres_rehearsal": False,
                                     "gsales_postgres_regression": False}):
        raise ValueError("V3 immutable unchanged-source proof is missing")
    own = verified_plan(ROOT, baseline, sha, baseline_verified=True)
    if (own["classification"] != "COMPONENT_CANDIDATE" or own["component"] != "ui" or
        not own["changedPaths"] or
        sorted(own["changedPaths"]) != sorted(impact.get("changedPaths", [])) or
        own["mayDeploySelectively"] is not False or own["maySkipV3Gates"] is not False):
        raise ValueError("V4 static source contains unsafe paths")
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() != sha or \
       subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip():
        raise ValueError("source workspace not clean/exact")

    output = Path(output).resolve()
    if output.is_relative_to(ROOT) or output.is_symlink():
        raise ValueError("static output must be an external real directory")
    output.mkdir(parents=True, exist_ok=True)
    (output / "receipt.json").unlink(missing_ok=True)
    stages = {}
    def stage(name, cmd, *, env=None):
        before = time.monotonic()
        _exec(*cmd, env=env)
        stages[name] = {"seconds": round(time.monotonic() - before, 3), "status": "PASS"}

    stage("pnpm_install", ("pnpm", "install", "--frozen-lockfile"))
    build_env = dict(os.environ, GPOINT_BUILD_SHA=sha)
    shutil.rmtree(ROOT / "apps/miniapp/dist", ignore_errors=True)
    stage("miniapp_typecheck_build", ("pnpm", "--filter", "@gpoint/miniapp", "build"), env=build_env)
    stage("miniapp_tests", ("pnpm", "--filter", "@gpoint/miniapp", "test"), env=build_env)
    # Release tests are essential, even though backend binaries/migrations
    # have not changed. They cover auth, syntax and production release semantics.
    stage("release_tests", ("pnpm", "test:release"), env=build_env)
    static = output / "miniapp.zip"
    static.unlink(missing_ok=True)
    start = time.monotonic()
    create_bundle(ROOT / "apps/miniapp/dist", sha, static)
    stages["immutable_static_archive"] = {"seconds": round(time.monotonic() - start, 3),
                                          "status": "PASS"}
    digest = hashlib.sha256()
    with static.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    static_sha = digest.hexdigest()
    migration_dir = ROOT / "packages/db/prisma/migrations"
    prisma = sorted(p.name for p in migration_dir.iterdir()
                    if (p / "migration.sql").is_file())[-1]
    from release_static_migrations_v4 import checked_migration_head
    agata_head = checked_migration_head(ROOT, sha)
    gsales_file = ROOT / "services/gsales/migrations/001_gsales.sql"
    receipt = {
        "schema": 2, "route": "STATIC_UI_ONLY", "status": "READY",
        "candidateSha": sha, "productionBaselineSha": baseline,
        "runId": int(os.environ["GITHUB_RUN_ID"]),
        "runAttempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "impactV3": impact, "impactV4": own,
        "gates": {k: "PASS" for k in ("sourceDiff", "miniappBuild", "miniappTests",
                                    "releaseTests", "staticArchive", "migrationUnchanged")},
        "staticArtifactName": "miniapp.zip", "staticArtifactSha256": static_sha,
        "staticArtifactRoute": "V4_FAST_STATIC",
        "prismaHead": prisma, "agataHead": agata_head,
        "gsalesMigrationHead": gsales_file.stem,
        "gsalesMigrationSha256": hashlib.sha256(gsales_file.read_bytes()).hexdigest(),
        "prepareStages": stages, "productionUserSmoke": "NOT_RUN",
    }
    (output / "receipt.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    print("V4_PREPARE_ROUTE=STATIC_UI_ONLY")
    print("V4_STATIC_SHA256=" + static_sha)
    return receipt
