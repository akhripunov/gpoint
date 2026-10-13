#!/usr/bin/env python3
"""Canonical Alembic DAG head via existing, tested release-migrations.mjs."""
import re
import subprocess
from pathlib import Path


def checked_migration_head(root: Path, candidate: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{40}", candidate or ""):
        raise ValueError("invalid exact candidate SHA")
    script = """
import { migrationHead } from './scripts/release-migrations.mjs';
import { execFileSync } from 'node:child_process';
const rev = process.argv[1];
const list = execFileSync('git', ['ls-tree', '-r', '--name-only', rev, 'services/agata/migrations/versions'], {encoding:'utf8'}).trim();
const files = list.split('\\n').filter(p => p.endsWith('.py'));
const sources = files.map(p => execFileSync('git', ['show', rev+':'+p], {encoding:'utf8'}));
process.stdout.write(migrationHead(sources).head);
"""
    value = subprocess.check_output(
        ["node", "--input-type=module", "-e", script, candidate],
        cwd=root, text=True, timeout=30).strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", value):
        raise RuntimeError("unsupported AGATA head")
    return value
