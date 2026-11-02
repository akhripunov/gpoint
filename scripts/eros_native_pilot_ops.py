#!/usr/bin/env python3
"""Fail-closed DB, backup and stock migration operations for the Native Eros pilot.

Run only on the GPoint VPS from the exact reviewed source checkout. The normal
Native deploy path never invokes this module.
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
import subprocess
import sys
import tempfile
import uuid


POSTGRES_CONTAINER = "shared_postgres_1"
EROS_NETWORK = "gpoint_eros_data"
EMBEDDINGS_NETWORK = "gpoint_eros_embeddings"
BACKUP_ROOT = Path("/opt/gpoint/backups/eros-pilot")
EROS_REPOSITORY = "ghcr.io/akhripunov/gpoint-eros-pilot"
UPSTREAM_SHA = "d1a140083321687212fb7b17c09cc0c3c9639f5e"
SYNTHETIC_PERSONA_SHA256 = "054d5bd0535eb231279cb0fa5b57dead1d51d3459d223c41962595fc7b3ac873"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
SHA40 = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE = re.compile(r"^ghcr\.io/akhripunov/gpoint-eros-pilot@sha256:[0-9a-f]{64}$")
RESTORE_DB = re.compile(r"^eros_restore_probe_[0-9a-f]{16}$")


class PilotGateError(RuntimeError):
    pass


def _valid_stock_image(image: str, candidate_sha: str) -> bool:
    if IMAGE.fullmatch(image or ""):
        return True
    return (os.environ.get("EROS_NATIVE_OPS_TEST") == "1"
            and re.fullmatch(r"eros-provision-test-[a-f0-9]{12}",
                             os.environ.get("EROS_NATIVE_OPS_TEST_POSTGRES_CONTAINER", ""))
            and SHA40.fullmatch(candidate_sha or "")
            and image == f"gpoint-eros-pilot:{candidate_sha}")


def validate_pilot_runtime(values: dict[str, str], user_id: str, creator_id: str,
                           candidate_sha: str) -> bool:
    if not UUID.fullmatch(user_id or "") or not UUID.fullmatch(creator_id or ""):
        raise PilotGateError("exact synthetic pilot UUID pair is required")
    if not SHA40.fullmatch(candidate_sha or "") or values.get("EROS_SOURCE_SHA") != candidate_sha:
        raise PilotGateError("pilot runtime source SHA differs from the exact candidate")
    if values.get("EROS_ENABLED") != "true" or values.get("EROS_PRODUCTION_TEST_MODE") != "true" or values.get("EROS_DATA_MODE") != "synthetic":
        raise PilotGateError("pilot runtime is not explicitly synthetic")
    if values.get("EROS_PRODUCTION_TEST_USER_IDS") != user_id or values.get("EROS_PRODUCTION_TEST_CREATOR_IDS") != creator_id:
        raise PilotGateError("pilot runtime UUID allowlist differs from the authorized pair")
    if values.get("EROS_TEST_USER_IDS") != user_id or values.get("EROS_TEST_CREATOR_IDS") != creator_id:
        raise PilotGateError("legacy synthetic allowlist aliases differ from the authorized pair")
    image = values.get("EROS_IMAGE", "")
    if not _valid_stock_image(image, candidate_sha):
        raise PilotGateError("stock Eros image must be pinned by immutable GHCR digest")
    for key in ("DATABASE_URL", "EROS_RUNTIME_DATABASE_URL", "EROS_MIGRATOR_DATABASE_URL"):
        if not values.get(key, "").startswith("postgresql://") or "@gpoint-eros-db:5432/eros" not in values[key]:
            raise PilotGateError("Eros database connection must target the isolated shared-Postgres database")
    if values["DATABASE_URL"] != values["EROS_RUNTIME_DATABASE_URL"]:
        raise PilotGateError("stock Eros runtime DATABASE_URL alias differs")
    return True


def write_migrator_env(path: Path, values: dict[str, str]) -> None:
    dsn = values.get("EROS_MIGRATOR_DATABASE_URL", "")
    if not dsn.startswith("postgresql://") or "@gpoint-eros-db:5432/eros" not in dsn or "\n" in dsn:
        raise PilotGateError("migrator DSN is invalid")
    path = Path(path)
    if path.parent != Path("/run") and os.environ.get("EROS_NATIVE_OPS_TEST") != "1":
        raise PilotGateError("migrator environment must be a transient /run file")
    if path.is_symlink() or path.exists():
        raise PilotGateError("migrator environment destination already exists")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(f"DATABASE_URL={dsn}\n")
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def stock_migrate_command(image: str, candidate_sha: str, env_file: str,
                          *, postgres_network: str = EROS_NETWORK) -> list[str]:
    if not SHA40.fullmatch(candidate_sha or "") or not _valid_stock_image(image, candidate_sha):
        raise PilotGateError("exact candidate SHA and immutable stock Eros digest are required")
    if postgres_network != EROS_NETWORK:
        raise PilotGateError("stock SQLx migration is restricted to the private Eros data network")
    test_run_dir = os.environ.get("EROS_NATIVE_OPS_TEST_RUN_DIR", "")
    expected_env = (re.escape(test_run_dir) + r"/eros-migrator-[0-9a-f]{40}\.env"
                    if os.environ.get("EROS_NATIVE_OPS_TEST") == "1" and test_run_dir
                    else r"/run/eros-migrator-[0-9a-f]{40}\.env")
    if not re.fullmatch(expected_env, env_file):
        raise PilotGateError("stock SQLx migration requires a candidate-scoped transient env file")
    return ["docker", "run", "--rm", "--network", EROS_NETWORK,
            "--env-file", env_file, image, "migrate"]


def _run(command: list[str], *, input_text: str | None = None, input_bytes: bytes | None = None,
         stdout=None, timeout: int = 90) -> subprocess.CompletedProcess:
    try:
        input_data = input_bytes if input_bytes is not None else input_text.encode("utf-8") if input_text is not None else None
        result = subprocess.run(command, input=input_data,
                                stdout=subprocess.PIPE if stdout is None else stdout,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PilotGateError("Native Eros pilot operation failed to start or timed out") from None
    if result.returncode:
        raise PilotGateError("Native Eros pilot operation failed; command output was withheld")
    return result


def verify_stock_image(image: str, candidate_sha: str) -> dict:
    if not _valid_stock_image(image, candidate_sha) or not SHA40.fullmatch(candidate_sha or ""):
        raise PilotGateError("exact Eros image digest and GPoint candidate SHA are required")
    result = _run(["docker", "image", "inspect", image], timeout=30)
    try:
        rows = json.loads(result.stdout)
        row = rows[0]
        labels = row["Config"]["Labels"] or {}
        digests = row.get("RepoDigests") or []
    except (TypeError, KeyError, IndexError, json.JSONDecodeError):
        raise PilotGateError("stock Eros image inspection is incomplete") from None
    test_image = _valid_stock_image(image, candidate_sha) and not IMAGE.fullmatch(image or "")
    if (row.get("Os") != "linux" or row.get("Architecture") != "amd64"
            or labels.get("org.opencontainers.image.revision") != UPSTREAM_SHA
            or labels.get("com.gpoint.eros.upstream-revision") != UPSTREAM_SHA
            or labels.get("com.gpoint.integration.revision") != candidate_sha
            or labels.get("com.gpoint.eros.patch-set.sha256") != "none"
            or (not test_image and image not in digests)):
        raise PilotGateError("stock Eros image identity or immutable digest read-back failed")
    return {"imageId": row.get("Id"),
            "digest": row.get("Id") if test_image else image.split("@", 1)[1],
            "platform": "linux/amd64"}


def run_stock_migration(image: str, candidate_sha: str, values: dict[str, str]) -> None:
    verify_stock_image(image, candidate_sha)
    if values.get("EROS_IMAGE") != image:
        raise PilotGateError("migration image differs from the provisioned pilot image")
    test_run_dir = os.environ.get("EROS_NATIVE_OPS_TEST_RUN_DIR", "")
    env_root = Path(test_run_dir) if os.environ.get("EROS_NATIVE_OPS_TEST") == "1" and test_run_dir else Path("/run")
    env_file = env_root / f"eros-migrator-{candidate_sha}.env"
    if env_root != Path("/run"):
        env_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(env_root, 0o700)
    write_migrator_env(env_file, values)
    try:
        _run(stock_migrate_command(image, candidate_sha, str(env_file)), timeout=600)
    finally:
        try:
            env_file.unlink()
        except FileNotFoundError:
            pass


def stock_persona_seed_command(image: str, candidate_sha: str, env_file: str) -> list[str]:
    if not SHA40.fullmatch(candidate_sha or "") or not _valid_stock_image(image, candidate_sha):
        raise PilotGateError("exact stock Eros image and source SHA are required for persona seed")
    test_root = os.environ.get("EROS_NATIVE_OPS_TEST_SOURCE_ROOT", "")
    source_root = Path(test_root) if os.environ.get("EROS_NATIVE_OPS_TEST") == "1" and test_root else Path("/opt/gpoint/releases") / candidate_sha / "source"
    persona_dir = source_root / "services/eros-engine/test-personas"
    persona_file = persona_dir / "synthetic-eros-persona.toml"
    if persona_file.is_symlink() or not persona_file.is_file() or sha256_file(persona_file) != SYNTHETIC_PERSONA_SHA256:
        raise PilotGateError("stock synthetic persona fixture differs from its reviewed identity")
    test_run_dir = os.environ.get("EROS_NATIVE_OPS_TEST_RUN_DIR", "")
    expected_env = (re.escape(test_run_dir) + r"/eros-migrator-[0-9a-f]{40}\.env"
                    if os.environ.get("EROS_NATIVE_OPS_TEST") == "1" and test_run_dir
                    else r"/run/eros-migrator-[0-9a-f]{40}\.env")
    if not re.fullmatch(expected_env, env_file):
        raise PilotGateError("stock persona seed requires a candidate-scoped migrator environment")
    return ["docker", "run", "--rm", "--network", EROS_NETWORK, "--env-file", env_file,
            "--volume", f"{persona_dir}:/etc/eros-engine/test-personas:ro", image,
            "seed-personas", "/etc/eros-engine/test-personas"]


def parse_seeded_genome_id(output: bytes | str) -> str:
    if isinstance(output, bytes):
        try:
            output = output.decode("utf-8")
        except UnicodeError:
            raise PilotGateError("stock persona seed output is not valid text") from None
    found: list[str] = []
    for line in output.splitlines():
        if "Synthetic Eros Persona" in line:
            found.extend(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", line, re.I))
    if len(found) != 1:
        raise PilotGateError("stock persona seed did not return exactly one synthetic genome UUID")
    try:
        parsed = uuid.UUID(found[0])
    except ValueError:
        raise PilotGateError("stock persona genome UUID is invalid") from None
    if parsed.int == 0:
        raise PilotGateError("stock persona genome UUID is invalid")
    return str(parsed)


def run_stock_persona_seed(image: str, candidate_sha: str, values: dict[str, str]) -> str:
    verify_stock_image(image, candidate_sha)
    test_run_dir = os.environ.get("EROS_NATIVE_OPS_TEST_RUN_DIR", "")
    env_root = Path(test_run_dir) if os.environ.get("EROS_NATIVE_OPS_TEST") == "1" and test_run_dir else Path("/run")
    env_file = env_root / f"eros-migrator-{candidate_sha}.env"
    if env_root != Path("/run"):
        env_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(env_root, 0o700)
    write_migrator_env(env_file, values)
    try:
        result = _run(stock_persona_seed_command(image, candidate_sha, str(env_file)), timeout=300)
        return parse_seeded_genome_id(result.stdout)
    finally:
        try:
            env_file.unlink()
        except FileNotFoundError:
            pass


def write_persona_seed_result(path: Path, source_sha: str, genome_id: str) -> None:
    if Path(path).parent != Path("/run") and os.environ.get("EROS_NATIVE_OPS_TEST") != "1":
        raise PilotGateError("persona seed result must be written under the transient /run directory")
    if not SHA40.fullmatch(source_sha or ""):
        raise PilotGateError("persona seed result requires an exact source SHA")
    try:
        parsed = uuid.UUID(genome_id)
    except (ValueError, TypeError):
        raise PilotGateError("persona seed result UUID is invalid") from None
    if parsed.int == 0 or Path(path).exists() or Path(path).is_symlink():
        raise PilotGateError("persona seed result destination is unsafe")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        json.dump({"schema": "gpoint-eros-persona-seed/v1", "sourceSha": source_sha,
                   "genomeId": str(parsed)}, target, sort_keys=True, separators=(",", ":"))
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_path(path: Path, root: Path) -> Path:
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    if path.parent != root or not path.name.startswith("eros-before-") or not path.name.endswith(".dump"):
        raise PilotGateError("Eros backup path must be a named dump inside the protected pilot backup directory")
    if path.is_symlink():
        raise PilotGateError("Eros backup path cannot be a symlink")
    return path


def write_backup_manifest(backup: Path, source_sha: str, database_identity: str,
                          *, backup_root: Path = BACKUP_ROOT) -> dict:
    if not SHA40.fullmatch(source_sha or "") or not database_identity:
        raise PilotGateError("exact candidate SHA and database identity are required for backup evidence")
    backup = _backup_path(backup, backup_root)
    if not backup.is_file() or backup.stat().st_mode & 0o077 or backup.stat().st_size == 0:
        raise PilotGateError("Eros backup must be a nonempty mode-0600 regular file")
    facts = {"schema": "gpoint-eros-pilot-backup/v1", "database": "eros",
             "sourceSha": source_sha, "sha256": sha256_file(backup),
             "databaseIdentitySha256": hashlib.sha256(database_identity.encode()).hexdigest(),
             "bytes": backup.stat().st_size}
    manifest = backup.with_suffix(".json")
    if manifest.exists() or manifest.is_symlink():
        raise PilotGateError("Eros backup evidence already exists")
    fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        json.dump(facts, target, sort_keys=True, separators=(",", ":"))
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    return facts


def verify_backup(backup: Path, backup_root: Path = BACKUP_ROOT) -> dict:
    backup = _backup_path(backup, backup_root)
    manifest = backup.with_suffix(".json")
    if (not backup.is_file() or backup.stat().st_mode & 0o077 or manifest.is_symlink()
            or not manifest.is_file() or manifest.stat().st_mode & 0o077):
        raise PilotGateError("Eros backup or its mode-0600 evidence is missing or unsafe")
    try:
        facts = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise PilotGateError("Eros backup evidence is invalid") from None
    if (facts.get("schema") != "gpoint-eros-pilot-backup/v1" or facts.get("database") != "eros"
            or facts.get("sha256") != sha256_file(backup) or facts.get("bytes") != backup.stat().st_size
            or not SHA40.fullmatch(facts.get("sourceSha", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", facts.get("databaseIdentitySha256", ""))):
        raise PilotGateError("Eros backup checksum or identity verification failed")
    return facts


def validate_restore_probe_name(name: str) -> bool:
    if not RESTORE_DB.fullmatch(name or ""):
        raise PilotGateError("isolated restore database name must use the fixed eros_restore_probe prefix")
    return True


def validate_cleanup_identity(*, database: str, owner: str, observed_identity: str,
                              expected_identity: str) -> bool:
    """Require the database's private deployment marker before any destructive cleanup."""
    if (database != "eros" or owner != "eros_ddl" or not expected_identity
            or observed_identity != expected_identity):
        raise PilotGateError("cleanup identity does not match the dedicated Eros database")
    return True


def validate_cleanup_service(service: str) -> bool:
    if service not in {"gpoint-eros-pilot", "gpoint-eros-relay"}:
        raise PilotGateError("cleanup is restricted to the two dedicated Eros Kamal services")
    return True


def _psql(sql: str, database: str = "postgres") -> str:
    if database not in {"postgres", "eros"}:
        raise PilotGateError("PostgreSQL operation database is outside the Eros cleanup scope")
    result = _run(["docker", "exec", "-i", _postgres_container(), "sh", "-ec",
                   f'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d {database}'],
                  input_text=sql, timeout=120)
    try:
        return result.stdout.decode().strip()
    except UnicodeError:
        raise PilotGateError("PostgreSQL read-back is not valid text") from None


def cleanup_database(expected_identity: str) -> None:
    if not expected_identity or "\n" in expected_identity or "\r" in expected_identity:
        raise PilotGateError("explicit Eros database identity is required for cleanup")
    owner = _psql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='eros'")
    if not owner:
        roles = _psql("SELECT count(*)::text FROM pg_roles WHERE rolname IN ('eros_ddl','eros_runtime','eros_migrator')")
        if roles == "0":
            return
        raise PilotGateError("Eros roles remain without the dedicated Eros database")
    identity = _psql("SELECT identity FROM public.eros_deployment_identity WHERE singleton=true", "eros")
    validate_cleanup_identity(database="eros", owner=owner, observed_identity=identity,
                              expected_identity=expected_identity)
    state = _psql("""SELECT json_build_object(
      'outsideOwners',(SELECT count(*) FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba
        WHERE d.datname<>'eros' AND r.rolname IN ('eros_ddl','eros_runtime','eros_migrator')),
      'memberships',(SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid IN (m.roleid,m.member)
        WHERE r.rolname IN ('eros_ddl','eros_runtime','eros_migrator')),
      'connections',(SELECT count(*) FROM pg_stat_activity WHERE datname='eros' AND pid<>pg_backend_pid())
    )::text""")
    try:
        inventory = json.loads(state)
    except (ValueError, TypeError):
        raise PilotGateError("Eros cleanup role and connection inventory is invalid") from None
    if inventory != {"outsideOwners": 0, "memberships": 0, "connections": 0}:
        raise PilotGateError("Eros roles or active connections extend beyond the isolated database")
    _psql("DROP DATABASE eros")
    _psql("DROP ROLE eros_runtime; DROP ROLE eros_migrator; DROP ROLE eros_ddl;")


def cleanup_managed_networks() -> list[str]:
    """Remove only the two labeled, internal, empty Eros networks."""
    removed: list[str] = []
    for name, expected_subnet in ((EROS_NETWORK, None), (EMBEDDINGS_NETWORK, "172.31.254.0/29")):
        result = subprocess.run(["docker", "network", "inspect", name], text=True,
                                capture_output=True, timeout=30, check=False)
        if result.returncode:
            detail = (result.stdout + result.stderr).lower()
            if "no such network" in detail or f"network {name} not found" in detail:
                continue
            raise PilotGateError("Eros network inspection failed")
        try:
            row = json.loads(result.stdout)[0]
            labels = row.get("Labels") or {}
            config = ((row.get("IPAM") or {}).get("Config") or [])
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            raise PilotGateError("Eros network inspection is incomplete") from None
        if (row.get("Name") != name or row.get("Driver") != "bridge" or row.get("Internal") is not True
                or labels.get("com.gpoint.eros.managed") != "true"):
            raise PilotGateError("network is not an internally managed Eros network")
        if expected_subnet and not any(item.get("Subnet") == expected_subnet for item in config):
            raise PilotGateError("Eros embeddings network subnet identity changed")
        containers = row.get("Containers") or {}
        if name == EROS_NETWORK and len(containers) == 1:
            attached = next(iter(containers.values()))
            if attached.get("Name") == "shared_postgres_1":
                _run(["docker", "network", "disconnect", name, "shared_postgres_1"], timeout=30)
                containers = {}
        if containers:
            raise PilotGateError("Eros network still has attached containers")
        _run(["docker", "network", "rm", name], timeout=30)
        removed.append(name)
    return removed


def remove_runtime_file(path: Path) -> None:
    path = Path(path)
    if os.environ.get("EROS_NATIVE_OPS_TEST") != "1" and path != Path("/opt/gpoint/env/eros.env"):
        raise PilotGateError("runtime cleanup path is outside the dedicated Eros environment file")
    if path.is_symlink():
        raise PilotGateError("Eros runtime environment cannot be a symlink")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
            raise PilotGateError("Eros runtime environment file has unsafe ownership mode")
        path.unlink()


def detach_pilot_api_network(version: str, source_sha: str, repo_digest: str) -> None:
    if (not re.fullmatch(r"[0-9a-f]{40}-[0-9]+-[0-9]+", version or "")
            or not SHA40.fullmatch(source_sha or "")
            or not re.fullmatch(r"ghcr\.io/akhripunov/gpoint-runtime@sha256:[0-9a-f]{64}", repo_digest or "")):
        raise PilotGateError("exact Kamal API version, source SHA and image digest are required for network detach")
    name = f"gpoint-core-api-{version}"
    result = subprocess.run(["docker", "inspect", name], text=True, capture_output=True,
                            timeout=30, check=False)
    if result.returncode:
        if "no such object" in (result.stdout + result.stderr).lower():
            return
        raise PilotGateError("pilot API container inspection failed")
    try:
        row = json.loads(result.stdout)[0]
        labels = row.get("Config", {}).get("Labels") or {}
        networks = row.get("NetworkSettings", {}).get("Networks") or {}
    except (ValueError, IndexError, TypeError):
        raise PilotGateError("pilot API container identity is incomplete") from None
    image_id = row.get("Image")
    image_row = json.loads(_run(["docker", "image", "inspect", image_id], timeout=30).stdout)[0]
    if (row.get("Name") != "/" + name or labels.get("org.opencontainers.image.revision") != source_sha
            or labels.get("service") not in (None, "gpoint-core")):
        raise PilotGateError("API container identity is outside the exact GPoint pilot scope")
    if repo_digest not in (image_row.get("RepoDigests") or []):
        raise PilotGateError("API container image does not match the verified repository digest")
    if EMBEDDINGS_NETWORK in networks:
        _run(["docker", "network", "disconnect", EMBEDDINGS_NETWORK, name], timeout=30)


def check_resources(*, minimum_available_kib: int = 1441792,
                    minimum_disk_bytes: int = 5 * 1024**3,
                    root: Path = Path("/opt/gpoint")) -> dict[str, int]:
    if minimum_available_kib < 1441792 or minimum_disk_bytes < 5 * 1024**3:
        raise PilotGateError("Native Eros pilot resource floors cannot be lowered")
    try:
        available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                             if line.startswith("MemAvailable:")))
        free_disk = shutil.disk_usage(root).free
    except (OSError, StopIteration, ValueError):
        raise PilotGateError("live memory and disk inventory is unavailable") from None
    if available < minimum_available_kib or free_disk < minimum_disk_bytes:
        raise PilotGateError("live VPS memory reserve or free disk is below the synthetic pilot floor")
    return {"memAvailableKiB": available, "freeDiskBytes": free_disk}


def _postgres_container() -> str:
    if os.environ.get("EROS_NATIVE_OPS_TEST") == "1":
        name = os.environ.get("EROS_NATIVE_OPS_TEST_POSTGRES_CONTAINER", "")
        if re.fullmatch(r"eros-provision-test-[a-f0-9]{12}", name):
            return name
    return POSTGRES_CONTAINER


def create_verified_backup(path: Path, source_sha: str, database_identity: str,
                           *, backup_root: Path = BACKUP_ROOT) -> dict:
    path = _backup_path(path, backup_root)
    if path.exists():
        raise PilotGateError("Eros backup destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as target:
            _run(["docker", "exec", "-i", _postgres_container(), "sh", "-ec",
                  'exec pg_dump -U "$POSTGRES_USER" -d eros -Fc'], stdout=target, timeout=600)
            target.flush()
            os.fsync(target.fileno())
        facts = write_backup_manifest(path, source_sha, database_identity, backup_root=backup_root)
        _run(["docker", "exec", "-i", _postgres_container(), "sh", "-ec",
              'exec pg_restore --list'], input_bytes=path.read_bytes(), timeout=120)
        return facts
    except Exception:
        for candidate in (path, path.with_suffix(".json")):
            try:
                candidate.unlink()
            except OSError:
                pass
        raise


def restore_probe(backup: Path, probe_name: str, *, backup_root: Path = BACKUP_ROOT) -> None:
    verify_backup(backup, backup_root)
    validate_restore_probe_name(probe_name)
    container = _postgres_container()
    exists = _run(["docker", "exec", "-i", container, "sh", "-ec",
                   'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres'],
                  input_text=f"SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname='{probe_name}');\n").stdout.decode().strip()
    if exists != "f":
        raise PilotGateError("isolated restore target already exists or catalog read-back is invalid")
    _run(["docker", "exec", "-i", container, "sh", "-ec",
          'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres'],
         input_text=f"CREATE DATABASE {probe_name};\n")
    try:
        _run(["docker", "exec", "-i", container, "sh", "-ec",
          f'exec pg_restore --exit-on-error -U "$POSTGRES_USER" -d {probe_name}'],
             input_bytes=Path(backup).read_bytes(), timeout=600)
    finally:
        _run(["docker", "exec", "-i", container, "sh", "-ec",
              f'exec dropdb --if-exists -U "$POSTGRES_USER" {probe_name}'], timeout=120)


def restore_eros(backup: Path, *, backup_root: Path = BACKUP_ROOT) -> dict:
    facts = verify_backup(backup, backup_root)
    _run(["docker", "exec", "-i", _postgres_container(), "sh", "-ec",
          'exec pg_restore --single-transaction --exit-on-error --clean --if-exists -U "$POSTGRES_USER" -d eros'],
         input_bytes=Path(backup).read_bytes(), timeout=900)
    return facts


def verify_sqlx_migrations() -> int:
    result = _run(["docker", "exec", "-i", _postgres_container(), "sh", "-ec",
                   'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d eros'],
                  input_text="SELECT count(*)::text || ':' || count(*) FILTER (WHERE NOT success)::text FROM public._sqlx_migrations;\n")
    try:
        total, failed = result.stdout.decode().strip().split(":", 1)
        total, failed = int(total), int(failed)
    except (ValueError, UnicodeError):
        raise PilotGateError("stock SQLx migration read-back is unavailable") from None
    if total <= 0 or failed != 0:
        raise PilotGateError("stock SQLx migration ledger is empty or contains a failed migration")
    return total


def _load_env(path: Path) -> dict[str, str]:
    if Path(path).is_symlink() or not Path(path).is_file() or Path(path).stat().st_mode & 0o077:
        raise PilotGateError("protected Eros environment file is missing or unsafe")
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise PilotGateError("protected Eros environment file cannot be read") from None
    for line in lines:
        if not line:
            continue
        key, separator, value = line.partition("=")
        if not separator or key in values or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise PilotGateError("protected Eros environment file is malformed")
        values[key] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="operation", required=True)
    for operation in ("validate", "migrate", "seed-persona", "backup", "restore-probe", "restore", "cleanup"):
        item = sub.add_parser(operation)
        item.add_argument("--source-sha", required=True)
        item.add_argument("--test-user-id", required=True)
        item.add_argument("--test-creator-id", required=True)
        item.add_argument("--environment", default="/opt/gpoint/env/eros.env")
        item.add_argument("--backup-root", default=str(BACKUP_ROOT))
        if operation == "migrate":
            item.add_argument("--image", required=True)
        if operation == "seed-persona":
            item.add_argument("--image", required=True)
        if operation in {"backup", "restore-probe", "restore"}:
            item.add_argument("--backup", required=True)
        if operation == "backup":
            item.add_argument("--database-identity")
        if operation == "restore-probe":
            item.add_argument("--probe-database", required=True)
    args = parser.parse_args()
    try:
        values = _load_env(Path(args.environment))
        validate_pilot_runtime(values, args.test_user_id, args.test_creator_id, args.source_sha)
        if args.operation == "validate":
            print("EROS_NATIVE_PILOT_RUNTIME_GATE=PASS")
        elif args.operation == "migrate":
            run_stock_migration(args.image, args.source_sha, values)
            print("EROS_STOCK_SQLX_MIGRATIONS=APPLIED")
        elif args.operation == "seed-persona":
            genome_id = run_stock_persona_seed(args.image, args.source_sha, values)
            result_path = Path("/run") / f"eros-persona-{args.source_sha}.json"
            write_persona_seed_result(result_path, args.source_sha, genome_id)
            print("EROS_STOCK_PERSONA_SEED=PASS")
        elif args.operation == "backup":
            database_identity = args.database_identity or values.get("EROS_DATABASE_IDENTITY", "")
            facts = create_verified_backup(Path(args.backup), args.source_sha,
                                           database_identity, backup_root=Path(args.backup_root))
            print(f"EROS_BACKUP=PASS sha256={facts['sha256']} bytes={facts['bytes']}")
        elif args.operation == "restore-probe":
            restore_probe(Path(args.backup), args.probe_database, backup_root=Path(args.backup_root))
            print("EROS_ISOLATED_RESTORE=PASS")
        elif args.operation == "restore":
            facts = restore_eros(Path(args.backup), backup_root=Path(args.backup_root))
            print(f"EROS_DATABASE_RESTORE=PASS sha256={facts['sha256']}")
        elif args.operation == "cleanup":
            cleanup_database(values.get("EROS_DATABASE_IDENTITY", ""))
            cleanup_managed_networks()
            remove_runtime_file(Path(args.environment))
            print("EROS_NATIVE_PILOT_DATABASE_CLEANUP=PASS")
    except (PilotGateError, OSError, ValueError) as error:
        print(f"EROS_NATIVE_PILOT_BLOCKED={error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
