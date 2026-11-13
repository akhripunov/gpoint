#!/usr/bin/env python3
"""Fail closed before cutover unless the candidate requires no DB migrations."""
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util.exc import CommandError

SHA40 = re.compile(r"^[0-9a-f]{40}$")
APPROVED_ALLOWLIST_SCHEMA = "gpoint-prisma-production-apply/v1"


def load_approved_migrations(path):
    """Load the OWNER-approved Prisma apply allowlist (fail-closed)."""
    raw = Path(path)
    if not raw.is_file():
        return {}
    try:
        data = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("approved migration allowlist is unavailable or invalid") from None
    migrations = data.get("migrations") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("schema") != APPROVED_ALLOWLIST_SCHEMA
            or not isinstance(migrations, dict)):
        raise ValueError("approved migration allowlist schema is unsupported")
    approved = {}
    for name, digest in migrations.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", name)
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("approved migration allowlist entry is malformed")
        approved[name] = digest
    return approved


def migration_file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def approved_pending_migration(migration, path, approved):
    """True when this pending migration is approved with the exact checksum."""
    recorded = approved.get(migration)
    if recorded is None:
        return False
    if migration_file_digest(path) != recorded:
        raise ValueError(f"APPROVED_MIGRATION_CHECKSUM_MISMATCH:{migration}")
    return True


COMMON_SCHEMA_FIELDS = {
    "id", "userId", "creatorId", "tenant_id", "session_id", "instance_id",
    "created_at", "updated_at", "createdAt", "updatedAt", "status", "version",
    "effective_from", "effective_to", "rules", "jurisdiction", "market",
    # Generic column names that unrelated APIs use for their own payloads
    # (Eros `body.purpose`, `role`). A bare line match is not evidence of a
    # schema dependency; they still count when the migration table is named on
    # the same line, exactly like the other common fields above.
    "purpose", "role",
}



def read_applied_agata_receipt(path, candidate_sha, current_head):
    """Read a private proof for the separately authorized 0011 -> 0012 apply."""
    if not SHA40.fullmatch(candidate_sha or ""):
        raise ValueError("migration receipt candidate identity is invalid")
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ""
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
        raise ValueError("migration receipt must be a private regular file owned by the reader")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(receipt, dict)
            or receipt.get("schema") != "gpoint-agata-applied-migration/v1"
            or receipt.get("status") != "PASS" or receipt.get("source_sha") != candidate_sha
            or receipt.get("head_before") != "agata0011"
            or receipt.get("head_after") != current_head or current_head != "agata0012"
            or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("migration_sha256", "")))
            or not re.fullmatch(r"ghcr\.io/akhripunov/gpoint-agata-runtime@sha256:[0-9a-f]{64}",
                                str(receipt.get("agata_image", "")))):
        raise ValueError("migration receipt source, heads, result or image identity is invalid")
    return "agata0012=" + receipt["migration_sha256"]


def read_ancestor_agata_receipt(sources, current_head, *, root=Path('/opt/gpoint/release-logs')):
    """Use the original Apply proof only among caller-verified Git ancestors."""
    if (not 1 <= len(sources) <= 64 or len(set(sources)) != len(sources)
            or any(not SHA40.fullmatch(source) for source in sources)):
        raise ValueError("migration receipt ancestors must be a bounded unique SHA inventory")
    matches = []
    for source in sources:
        checksum = read_applied_agata_receipt(Path(root) / source / 'agata-applied-migration.json',
                                             source, current_head)
        if checksum:
            matches.append(source + ':' + checksum)
    if len(matches) > 1:
        raise ValueError("multiple migration Apply receipts require reconciliation")
    return matches[0] if matches else ''


def parenthesized_sql(source, opening):
    """Return a balanced SQL parenthesized body, respecting quoted text."""
    depth, quote, index = 0, None, opening
    while index < len(source):
        char = source[index]
        if quote:
            if char == quote:
                if quote == "'" and index + 1 < len(source) and source[index + 1] == "'":
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
        index += 1
    raise ValueError("pending migration contains an unbalanced SQL definition")


def sql_definition_items(body):
    items, start, depth, quote = [], 0, 0, None
    index = 0
    while index < len(body):
        char = body[index]
        if quote:
            if char == quote:
                if quote == "'" and index + 1 < len(body) and body[index + 1] == "'":
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(body[start:index])
            start = index + 1
        index += 1
    items.append(body[start:])
    return items


def prisma_names(directory):
    rows = []
    for path in sorted(Path(directory).iterdir()):
        if not path.is_dir():
            continue
        if not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", path.name):
            raise ValueError("candidate Prisma migration directory name is invalid")
        if not (path / "migration.sql").is_file():
            raise ValueError("candidate Prisma migration SQL is missing")
        rows.append(path.name)
    if not rows:
        raise ValueError("candidate Prisma migration inventory is empty")
    return rows


def alembic_revisions(directory):
    version_directory = Path(directory).resolve(strict=True)
    source_root = version_directory.parent.parent
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    script_directory = ScriptDirectory(
        dir=str(version_directory.parent),
        version_locations=[str(version_directory)],
    )
    revisions = {}
    revision_files = {}
    for script in script_directory.walk_revisions():
        revision = script.revision
        parents = script.down_revision
        if (not isinstance(revision, str)
                or not re.fullmatch(r"[A-Za-z0-9_]+", revision)
                or revision in revisions):
            raise ValueError("candidate Alembic revision identity is invalid or duplicated")
        if parents is None:
            parents = ()
        elif isinstance(parents, str):
            parents = (parents,)
        elif isinstance(parents, tuple) and all(isinstance(item, str) for item in parents):
            pass
        else:
            raise ValueError("candidate Alembic parent identity is unsupported")
        revisions[revision] = parents
        revision_files[revision] = Path(script.path).resolve(strict=True)
    if not revisions:
        raise ValueError("candidate Alembic migration inventory is empty")
    heads = sorted(script_directory.get_heads())
    if len(heads) != 1:
        raise ValueError("candidate Alembic migration graph must have exactly one head")
    return revisions, heads[0], revision_files


def alembic_head(directory):
    return alembic_revisions(directory)[1]


def migration_identifiers(path, *, prisma_directory, repo_root):
    """Identify tables and fields touched by a pending migration for code-diff checks."""
    source = Path(path).read_text(encoding="utf-8")
    identifiers, tables = set(), set()
    for match in re.finditer(
        r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w\".]+)\s*\(",
        source, re.IGNORECASE,
    ):
        table = match.group(1).split(".")[-1].strip('"')
        tables.add(table)
        identifiers.add(table)
        for definition in sql_definition_items(parenthesized_sql(source, match.end() - 1)):
            column = re.match(r"\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s+", definition)
            if column and column.group(1).upper() not in {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"}:
                identifiers.add(column.group(1))
    for match in re.finditer(
        r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w\".]+)[^;]*?\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?!CONSTRAINT\b|PRIMARY\b|UNIQUE\b|CHECK\b|FOREIGN\b|EXCLUDE\b)[\"']?([A-Za-z_][A-Za-z0-9_]*)",
        source, re.IGNORECASE,
    ):
        table = match.group(1).split(".")[-1].strip('"')
        tables.add(table)
        identifiers.update((table, match.group(2)))
    # Destructive/renaming operations also define the schema surface. A changed
    # runtime line that references the removed identifier is not compatible.
    for match in re.finditer(
        r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w\".]+)[^;]*?\bDROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?[\"']?([A-Za-z_][A-Za-z0-9_]*)",
        source, re.IGNORECASE,
    ):
        table = match.group(1).split(".")[-1].strip('"')
        identifiers.update((table, match.group(2)))
    for match in re.finditer(
        r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w\".]+)[^;]*?\bRENAME\s+COLUMN\s+[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s+TO\s+[\"']?([A-Za-z_][A-Za-z0-9_]*)",
        source, re.IGNORECASE,
    ):
        table = match.group(1).split(".")[-1].strip('"')
        identifiers.update((table, match.group(2), match.group(3)))
    for match in re.finditer(
        r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w\".]+)[^;]*?\bRENAME\s+TO\s+[\"']?([A-Za-z_][A-Za-z0-9_]*)",
        source, re.IGNORECASE,
    ):
        identifiers.update((match.group(1).split(".")[-1].strip('"'), match.group(2)))
    for match in re.finditer(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w\".]+)", source, re.IGNORECASE):
        table = match.group(1).split(".")[-1].strip('"')
        identifiers.add(table)
    # Data migrations can change semantics without adding a column, as AGATA 0012 does.
    for match in re.finditer(r"->>\s*[\"']([a-z][a-z0-9_]+)[\"']", source):
        identifiers.add(match.group(1))
    for match in re.finditer(r"op\.(?:create_table|add_column)\(\s*[\"']([^\"']+)", source):
        identifiers.add(match.group(1))
        tables.add(match.group(1))
    for match in re.finditer(r"sa\.Column\(\s*[\"']([^\"']+)", source):
        identifiers.add(match.group(1))

    if Path(path).suffix == ".py":
        for match in re.finditer(r"\b(?:FROM|UPDATE|INTO)\s+([\w\".]+)", source, re.IGNORECASE):
            table = match.group(1).split(".")[-1].strip('"')
            if table not in {"SET", "ONLY"}:
                tables.add(table)
                identifiers.add(table)
        # AGATA data migrations may change the meaning of existing fields. Map
        # touched tables back to their SQLAlchemy model and model fields so a
        # newly-added `RegionalPolicyProfile.status` use is treated as dependent.
        models_path = Path(repo_root) / "services/agata/app/db/models.py"
        if models_path.is_file():
            models = models_path.read_text(encoding="utf-8")
            for table in tables:
                for block in re.finditer(
                    r"class\s+([A-Za-z_][A-Za-z0-9_]*)\b[^:]*:\n(.*?)(?=\nclass\s|\Z)",
                    models, re.DOTALL,
                ):
                    model, body = block.group(1), block.group(2)
                    table_name = re.search(r"__tablename__\s*=\s*[\"']([^\"']+)", body)
                    if table_name and table_name.group(1) == table:
                        identifiers.add(model)
                        identifiers.update(re.findall(r"^\s{4}([A-Za-z_][A-Za-z0-9_]*)\s*:\s*Mapped\[", body, re.MULTILINE))

    # Prisma delegate names are not SQL table names; map new tables back to model names.
    schema = Path(prisma_directory).parent / "schema.prisma"
    if schema.is_file():
        content = schema.read_text(encoding="utf-8")
        for block in re.finditer(r"\bmodel\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{(.*?)\n\}", content, re.DOTALL):
            model, body = block.group(1), block.group(2)
            mapped = re.search(r"@@map\(\s*[\"']([^\"']+)", body)
            table = mapped.group(1) if mapped else model
            if table in tables:
                identifiers.update((model, model[:1].lower() + model[1:]))
    # Keep common names such as `status`, `key`, and `index` when a migration
    # actually defines them. Filtering valid identifiers creates false PASS.
    return {x for x in identifiers if x}


def is_explicit_noop_migration(path):
    """Recognize only a single SELECT 1 statement as having no schema dependencies."""
    source = Path(path).read_text(encoding="utf-8")
    without_line_comments = re.sub(r"(?m)^\s*--[^\n]*(?:\n|$)", "", source)
    return re.fullmatch(r"\s*SELECT\s+1\s*;\s*", without_line_comments, re.IGNORECASE) is not None


def changed_application_lines(repo_root, baseline_sha, candidate_sha):
    result = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--unified=0", baseline_sha, candidate_sha,
         "--", "apps", "services/agata", "packages"],
        check=True, capture_output=True, text=True,
    )
    path = ""
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        parts = set(Path(path).parts)
        if not path or parts.intersection({"test", "tests", "migrations", "migration"}):
            continue
        if Path(path).suffix in {".ts", ".tsx", ".js", ".jsx", ".py"}:
            yield path, line[1:]


def require_live_source_tree_in_candidate(repo_root, baseline_sha, candidate_sha):
    """Accept divergent history only when the candidate preserves every live-tree change."""
    common = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", baseline_sha, candidate_sha],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not SHA40.fullmatch(common):
        raise ValueError("live source and candidate have no verified common ancestor")
    paths = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--name-only", "--no-renames", "-z",
         common, baseline_sha, "--"],
        check=True, capture_output=True,
    ).stdout.split(b"\0")
    for raw_path in filter(None, paths):
        path = raw_path.decode("utf-8", errors="strict")
        baseline_entry = subprocess.run(
            ["git", "-C", str(repo_root), "ls-tree", "-z", baseline_sha, "--", path],
            check=True, capture_output=True,
        ).stdout
        candidate_entry = subprocess.run(
            ["git", "-C", str(repo_root), "ls-tree", "-z", candidate_sha, "--", path],
            check=True, capture_output=True,
        ).stdout
        if baseline_entry != candidate_entry:
            raise ValueError(f"LIVE_SOURCE_TREE_NOT_PRESERVED:{path}")


def verify_compatible(args, live_prisma, recorded):
    baseline, candidate = args.baseline_source_sha, args.candidate_source_sha
    if not SHA40.fullmatch(baseline or "") or not SHA40.fullmatch(candidate or ""):
        raise ValueError("compatible policy requires exact baseline and candidate source SHAs")
    repo_root = Path(args.repo_root).resolve(strict=True)
    for sha in (baseline, candidate):
        actual = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--verify", sha + "^{commit}"],
                                check=True, capture_output=True, text=True).stdout.strip()
        if actual != sha:
            raise ValueError("source commit identity is invalid")
    ancestor = subprocess.run(["git", "-C", str(repo_root), "merge-base", "--is-ancestor", baseline, candidate], check=False)
    if ancestor.returncode == 1:
        require_live_source_tree_in_candidate(repo_root, baseline, candidate)
    elif ancestor.returncode != 0:
        raise ValueError("cannot verify live source ancestry")

    pending_prisma = prisma_names(args.prisma_directory)[len(live_prisma):]
    if prisma_names(args.prisma_directory)[:len(live_prisma)] != live_prisma:
        raise ValueError("live Prisma migrations are not a prefix of candidate migration history")
    if not live_prisma or args.prisma_current_head != live_prisma[-1]:
        raise ValueError("live Prisma head does not match its applied migration inventory")
    revisions, candidate_agata_head, revision_files = alembic_revisions(args.agata_directory)
    if args.agata_current_head not in revisions:
        raise ValueError("live AGATA head is absent from candidate migration history")
    def ancestors(revision):
        seen, stack = set(), [revision]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(revisions[current])
        return seen
    live_ancestors = ancestors(args.agata_current_head)
    candidate_ancestors = ancestors(candidate_agata_head)
    if args.agata_current_head not in candidate_ancestors:
        raise ValueError("live AGATA head is not an ancestor of candidate history")
    pending_agata = candidate_ancestors - live_ancestors
    if getattr(args, "require_no_agata_pending", False) and pending_agata:
        raise ValueError("PENDING_AGATA_MIGRATION:" + ",".join(sorted(pending_agata)))
    applied_agata = {}
    for item in filter(None, getattr(args, "agata_current_checksums", "").split(",")):
        revision, separator, checksum = item.partition("=")
        if (not separator or revision in applied_agata or revision not in live_ancestors
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
            raise ValueError("applied AGATA checksum inventory is malformed")
        if hashlib.sha256(revision_files[revision].read_bytes()).hexdigest() != checksum:
            raise ValueError(f"ALEMBIC_MIGRATION_CHECKSUM_MISMATCH:{revision}")
        applied_agata[revision] = checksum
    for revision in live_ancestors:
        path = revision_files[revision]
        relative = path.resolve(strict=True).relative_to(repo_root)
        result = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--quiet", baseline, candidate, "--", str(relative)],
            check=False,
        )
        if result.returncode == 1:
            previous = subprocess.run(
                ["git", "-C", str(repo_root), "ls-tree", "-z", baseline, "--", str(relative)],
                check=True, capture_output=True,
            ).stdout
            # A separately applied new revision may precede the app switch.
            # Never permit rewriting a revision already present in live source.
            if previous or revision not in applied_agata:
                raise ValueError(f"APPLIED_ALEMBIC_MIGRATION_CHANGED:{revision}")
            directory = Path(args.agata_directory).resolve(strict=True).relative_to(repo_root)
            pattern = r"^[[:space:]]*revision[[:space:]]*=[[:space:]]*['\"]" + re.escape(revision) + r"['\"]"
            historical = subprocess.run(
                ["git", "-C", str(repo_root), "grep", "-E", pattern, baseline, "--", str(directory / "*.py")],
                capture_output=True, check=False,
            )
            if historical.returncode == 0:
                raise ValueError(f"APPLIED_ALEMBIC_MIGRATION_CHANGED:{revision}")
            if historical.returncode != 1:
                raise ValueError("cannot verify baseline Alembic revision inventory")
        elif result.returncode != 0:
            raise ValueError(f"cannot compare applied Alembic revision with baseline: {revision}")
    pending = [(name, Path(args.prisma_directory) / name / "migration.sql") for name in pending_prisma]
    pending.extend((name, revision_files[name]) for name in pending_agata)
    changed = list(changed_application_lines(repo_root, baseline, candidate))
    approved_migrations = load_approved_migrations(
        getattr(args, "approved_migrations", "") or
        (Path(args.prisma_directory) / ".." / "production-approved-migrations.json").resolve())
    for migration, path in pending:
        identifiers = migration_identifiers(path, prisma_directory=args.prisma_directory, repo_root=repo_root)
        approved_dependency = False
        if migration in approved_migrations:
            if migration_file_digest(path) != approved_migrations[migration]:
                raise ValueError(f"APPROVED_MIGRATION_CHECKSUM_MISMATCH:{migration}")
            approved_dependency = True
        if not identifiers:
            if is_explicit_noop_migration(path):
                continue
            raise ValueError(f"cannot identify dependencies in pending migration {migration}")
        for source_path, line in changed:
            if migration in pending_agata and not source_path.startswith("services/agata/"):
                continue
            for identifier in identifiers:
                if identifier in COMMON_SCHEMA_FIELDS:
                    anchors = identifiers - COMMON_SCHEMA_FIELDS
                    if not any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(anchor)}(?![A-Za-z0-9_])", line, re.IGNORECASE)
                               for anchor in anchors):
                        continue
                if re.search(rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])", line, re.IGNORECASE):
                    if approved_dependency:
                        print(f"APPROVED_PENDING_DEPENDENCY:{migration}:{identifier}:{source_path}",
                              file=sys.stderr)
                        continue
                    raise ValueError(
                        f"UNSAFE_SCHEMA_DEPENDENCY:{migration}:{identifier}:{source_path}")
    if set(recorded) != set(live_prisma):
        raise ValueError("live Prisma checksum inventory does not cover exactly applied migrations")
    return live_prisma[-1], args.agata_current_head, bool(pending)


def verify_static_pending(args):
    """Detect pending-migration/app-code dependencies without production access.

    Pending is derived from migrations *added* between the baseline and the
    candidate, so the same identifier scan can run in Tests before Build. The
    live-database gate stays mandatory in Deploy; this only fails earlier.
    """
    repo_root = Path(args.repo_root).resolve()
    baseline, candidate = args.baseline_source_sha, args.candidate_source_sha
    if not baseline or not candidate:
        raise ValueError("static pending scan requires --baseline-source-sha and --candidate-source-sha")
    added = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--name-only", "--diff-filter=A", baseline, candidate],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    raw_prisma = Path(args.prisma_directory)
    prisma_directory = (raw_prisma if raw_prisma.is_absolute() else repo_root / raw_prisma).resolve()
    pending = []
    for raw in added:
        path = (repo_root / raw).resolve()
        if path.name == "migration.sql" and path.parent.parent == prisma_directory:
            pending.append((path.parent.name, path))
        elif raw.startswith("services/agata/migrations/versions/") and path.suffix == ".py":
            pending.append((path.stem, path))
    changed = list(changed_application_lines(repo_root, baseline, candidate))
    approved_migrations = load_approved_migrations(
        getattr(args, "approved_migrations", "") or
        (Path(args.prisma_directory) / ".." / "production-approved-migrations.json").resolve())
    for migration, path in pending:
        identifiers = migration_identifiers(
            path, prisma_directory=args.prisma_directory, repo_root=repo_root)
        approved_dependency = False
        if migration in approved_migrations:
            if migration_file_digest(path) != approved_migrations[migration]:
                raise ValueError(f"APPROVED_MIGRATION_CHECKSUM_MISMATCH:{migration}")
            approved_dependency = True
        if not identifiers:
            if is_explicit_noop_migration(path):
                continue
            raise ValueError(f"cannot identify dependencies in pending migration {migration}")
        for source_path, line in changed:
            if migration in {name for name, _ in pending if str(path).endswith(".py")} \
                    and not source_path.startswith("services/agata/"):
                continue
            for identifier in identifiers:
                if identifier in COMMON_SCHEMA_FIELDS:
                    anchors = identifiers - COMMON_SCHEMA_FIELDS
                    if not any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(anchor)}(?![A-Za-z0-9_])", line, re.IGNORECASE)
                               for anchor in anchors):
                        continue
                if re.search(rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])", line, re.IGNORECASE):
                    if approved_dependency:
                        print(f"APPROVED_PENDING_DEPENDENCY:{migration}:{identifier}:{source_path}",
                              file=sys.stderr)
                        continue
                    raise ValueError(
                        f"UNSAFE_SCHEMA_DEPENDENCY:{migration}:{identifier}:{source_path}")
    return pending


def verify(args):
    missing = [name for name in ("prisma_current_migrations", "prisma_current_head",
                                 "agata_directory", "agata_current_head")
               if not getattr(args, name, "")]
    if missing:
        raise ValueError(f"live parity check requires {','.join(missing)}")
    candidate_prisma = prisma_names(args.prisma_directory)
    live_prisma = args.prisma_current_migrations.split(",") if args.prisma_current_migrations else []
    if any(not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", name) for name in live_prisma):
        raise ValueError("live Prisma migration inventory is malformed")
    if len(live_prisma) != len(set(live_prisma)):
        raise ValueError("live Prisma migration inventory contains duplicates")
    checksums = args.prisma_current_checksums.split(",") if args.prisma_current_checksums else []
    recorded = {}
    if checksums:
        for value in checksums:
            name, separator, checksum = value.partition("=")
            if (not separator or name in recorded or not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", name)
                    or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
                raise ValueError("live Prisma checksum inventory is malformed")
            recorded[name] = checksum
        if set(recorded) != set(live_prisma):
            raise ValueError("live Prisma checksum inventory does not cover applied migrations")
        for name, checksum in recorded.items():
            expected = hashlib.sha256((Path(args.prisma_directory) / name / "migration.sql").read_bytes()).hexdigest()
            if checksum != expected:
                raise ValueError(f"PRISMA_MIGRATION_CHECKSUM_MISMATCH:{name}")
    if args.policy == "compatible":
        return verify_compatible(args, live_prisma, recorded)
    if set(live_prisma) != set(candidate_prisma):
        raise ValueError("PENDING_OR_UNRECONCILED_PRISMA_MIGRATIONS")
    candidate_prisma_head = candidate_prisma[-1]
    if args.prisma_current_head != candidate_prisma_head:
        raise ValueError("PENDING_PRISMA_HEAD_MISMATCH")
    candidate_agata_head = alembic_head(args.agata_directory)
    if args.agata_current_head != candidate_agata_head:
        raise ValueError("PENDING_AGATA_HEAD_MISMATCH")
    return candidate_prisma_head, candidate_agata_head, False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", choices=("exact", "compatible"), default="exact")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--baseline-source-sha")
    parser.add_argument("--candidate-source-sha")
    parser.add_argument("--approved-migrations", default="")
    parser.add_argument("--prisma-directory", required=True)
    parser.add_argument("--prisma-current-migrations", default="")
    parser.add_argument("--prisma-current-checksums", default="")
    parser.add_argument("--prisma-current-head", default="")
    parser.add_argument("--agata-directory", default="")
    parser.add_argument("--agata-current-head", default="")
    parser.add_argument("--agata-current-checksums", default="")
    parser.add_argument("--require-no-agata-pending", action="store_true",
                        help="block Native Prisma Deploy if any AGATA revision still needs a separate guarded Apply")
    parser.add_argument("--static-pending", action="store_true",
                        help="scan only migrations added by this candidate; needs no live database")
    args = parser.parse_args()
    if args.static_pending:
        try:
            pending = verify_static_pending(args)
        except (CommandError, OSError, RevisionError, SyntaxError, ValueError,
                subprocess.CalledProcessError) as exc:
            print(f"GPOINT_KAMAL_SCHEMA_DEPENDENCY=BLOCKED reason={exc}", file=sys.stderr)
            return 1
        names = ",".join(name for name, _ in pending) or "none"
        print(f"GPOINT_KAMAL_SCHEMA_DEPENDENCY=PASS pending={names}")
        return 0
    try:
        prisma_head, agata_head, pending = verify(args)
    except (CommandError, ImportError, OSError, RevisionError, SyntaxError, ValueError,
            subprocess.CalledProcessError) as exc:
        print(f"GPOINT_KAMAL_MIGRATION_PARITY=BLOCKED reason={exc}", file=sys.stderr)
        return 1
    if args.policy == "compatible":
        print(f"GPOINT_KAMAL_SCHEMA_COMPATIBILITY=PASS LIVE_PRISMA_HEAD={prisma_head} LIVE_AGATA_HEAD={agata_head}")
        print(f"GPOINT_KAMAL_DB_MIGRATION_REQUIRED={'YES' if pending else 'NO'}")
    else:
        print(f"GPOINT_KAMAL_MIGRATION_PARITY=PASS PRISMA_HEAD={prisma_head} AGATA_HEAD={agata_head}")
        print("GPOINT_KAMAL_DB_MIGRATION_REQUIRED=NO")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--read-agata-ancestor-receipt":
        try:
            print(read_ancestor_agata_receipt(sys.argv[3:], sys.argv[2]))
        except (OSError, ValueError) as exc:
            print(f"AGATA_MIGRATION_RECEIPT=BLOCKED reason={exc}", file=sys.stderr)
            raise SystemExit(1)
    elif len(sys.argv) == 5 and sys.argv[1] == "--read-agata-receipt":
        try:
            print(read_applied_agata_receipt(sys.argv[2], sys.argv[3], sys.argv[4]))
        except (OSError, ValueError) as exc:
            print(f"AGATA_MIGRATION_RECEIPT=BLOCKED reason={exc}", file=sys.stderr)
            raise SystemExit(1)
    else:
        raise SystemExit(main())
