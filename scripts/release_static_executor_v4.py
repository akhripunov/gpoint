#!/usr/bin/env python3
"""GPoint static site transaction, invoked ONLY by canonical owner-authorized V3.

No direct network, SSH, Docker or database mutation. Every path is rooted in
/opt/gpoint on production. Durable journal+atomic rename-exchange on same fs.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile

from release_static_exchange_v4 import _entry_sha, exchange_site, restore_site
from release_ui_artifact_v4 import verify_bundle

SHA = re.compile(r"^[a-f0-9]{40}$")
PRODUCTION_ROOT = Path("/opt/gpoint")


def _journal_path(root: Path, candidate: str) -> Path:
    return root / "app" / (".static-v4-" + candidate + ".json")


def _journal_read(root: Path, candidate: str):
    file = _journal_path(root, candidate)
    if not file.exists():
        return None
    if file.is_symlink() or not file.is_file():
        raise RuntimeError("unsafe static transaction journal")
    data = json.loads(file.read_text())
    if data.get("candidateSha") != candidate:
        raise RuntimeError("static journal identity mismatch")
    return data


def _save_journal(root: Path, candidate: str, state: dict):
    path = _journal_path(root, candidate)
    if path.is_symlink():
        raise RuntimeError("journal symlink forbidden")
    content = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    fd, temp = tempfile.mkstemp(prefix=".v4-tx-", dir=root / "app")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        dfd = os.open(root / "app", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def verify_root(root: Path):
    root = root.resolve(strict=True)
    if root.is_symlink() or not (root / "app").is_dir() or (root / "app").is_symlink():
        raise RuntimeError("unsafe /opt/gpoint site root")
    return root


def _assert_no_pending(root: Path, candidate: str):
    for journal in (root / "app").glob(".static-v4-*.json"):
        if journal.is_symlink():
            raise RuntimeError("unexpected static symlink journal")
        record = json.loads(journal.read_text())
        if record.get("phase") in ("ARMED", "SWITCHED"):
            raise RuntimeError("unresolved static switch requires recovery: " + journal.name)


def preflight(root: Path, baseline: str, candidate: str):
    if not SHA.fullmatch(candidate or "") or not SHA.fullmatch(baseline or "") or candidate == baseline:
        raise ValueError("exact candidate and baseline required")
    root = verify_root(root)
    _assert_no_pending(root, candidate)
    live = root / "app" / "miniapp"
    if _entry_sha(live) != baseline:
        raise RuntimeError("live source SHA mismatch")
    staged = root / "app" / ("static-v4-" + candidate)
    if staged.exists() or staged.is_symlink():
        raise RuntimeError("candidate staging already exists")
    return {"status": "PASS", "candidateSha": candidate, "baselineSha": baseline}


def stage(root: Path, archive: Path, baseline: str, candidate: str):
    preflight(root, baseline, candidate)
    manifest = verify_bundle(archive, candidate)
    root = verify_root(root)
    app = root / "app"
    staged = app / ("static-v4-" + candidate)
    work = Path(tempfile.mkdtemp(prefix=".static-stage-", dir=app))
    try:
        with zipfile.ZipFile(archive) as z:
            for name in sorted(manifest["files"]):
                dest = work / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(name) as data, dest.open("xb") as output:
                    shutil.copyfileobj(data, output, length=65536)
                dest.chmod(0o644)
                if hashlib.sha256(dest.read_bytes()).hexdigest() != manifest["files"][name]["sha256"]:
                    raise RuntimeError("staged static checksum mismatch")
        if _entry_sha(work) != candidate:
            raise RuntimeError("staged entrypoint SHA wrong")
        os.replace(work, staged)
        return {"status": "STAGED", "candidateSha": candidate, "archiveFiles": len(manifest["files"])}
    finally:
        if work.exists():
            shutil.rmtree(work)


def switch(root: Path, baseline: str, candidate: str):
    root = verify_root(root)
    _assert_no_pending(root, candidate)
    journal = _journal_read(root, candidate)
    if journal and journal.get("phase") not in ("ROLLED_BACK",):
        raise RuntimeError("candidate static transaction already finalized/started")
    record = {"candidateSha": candidate, "baselineSha": baseline, "phase": "ARMED"}
    _save_journal(root, candidate, record)
    try:
        facts = exchange_site(root, baseline, candidate)
        record.update(facts, phase="SWITCHED")
        _save_journal(root, candidate, record)
        return record
    except BaseException:
        # No automatic rewrite if the first atomic exchange might have
        # succeeded before a subsequent IO/fault. Reconciliation is required.
        raise


def rollback(root: Path, baseline: str, candidate: str):
    root = verify_root(root)
    record = _journal_read(root, candidate)
    if not record or record.get("phase") not in ("ARMED", "SWITCHED", "COMPLETE") or record.get("baselineSha") != baseline:
        raise RuntimeError("rollback requires an unresolved trusted transaction")
    live = root / "app" / "miniapp"
    staged = root / "app" / ("static-v4-" + candidate)
    if _entry_sha(live) == candidate and _entry_sha(staged) == baseline:
        restore_site(root, candidate, baseline)
    elif _entry_sha(live) != baseline or _entry_sha(staged) != candidate:
        raise RuntimeError("ambiguous static failure state; recovery required")
    record["phase"] = "ROLLED_BACK"
    _save_journal(root, candidate, record)
    return record


def complete(root: Path, baseline: str, candidate: str):
    root = verify_root(root)
    record = _journal_read(root, candidate)
    if not record or record.get("phase") != "SWITCHED" or record.get("baselineSha") != baseline:
        raise RuntimeError("completion requires switched static transaction")
    if _entry_sha(root / "app" / "miniapp") != candidate:
        raise RuntimeError("switched site drift")
    record["phase"] = "COMPLETE"
    _save_journal(root, candidate, record)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("preflight", "stage", "switch", "rollback", "complete"))
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--archive")
    args = parser.parse_args()
    if args.command == "preflight":
        result = preflight(PRODUCTION_ROOT, args.baseline, args.candidate)
    elif args.command == "stage":
        if args.archive != "/tmp/gpoint-v4-static-" + args.candidate + ".zip":
            raise ValueError("archive not in canonical VPS temporary location")
        result = stage(PRODUCTION_ROOT, Path(args.archive), args.baseline, args.candidate)
    elif args.command == "switch":
        result = switch(PRODUCTION_ROOT, args.baseline, args.candidate)
    elif args.command == "rollback":
        result = rollback(PRODUCTION_ROOT, args.baseline, args.candidate)
    else:
        result = complete(PRODUCTION_ROOT, args.baseline, args.candidate)
    print("GPOINT_V4_STATIC_TRANSACTION=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
