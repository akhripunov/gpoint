#!/usr/bin/env python3
"""Disposable Docker/PostgreSQL rehearsal for Native Eros pilot operations."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16"
SAFE_DIAGNOSTICS = frozenset({
    "postgres_schema_permission", "postgres_object_permission", "postgres_object_owner",
    "postgres_dns_failure", "postgres_connection_failure", "postgres_database_missing",
    "postgres_vector_extension", "sqlx_migration_object_conflict",
})
stage = "startup"


def failure_summary(current_stage: str, error: Exception) -> str:
    line = f"EROS_NATIVE_POSTGRES_E2E=FAIL:stage={current_stage}:type={type(error).__name__}"
    diagnostic = getattr(error, "diagnostic_code", None)
    if diagnostic in SAFE_DIAGNOSTICS:
        line += f":diagnostic={diagnostic}"
    return line


def module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError("pilot operations module unavailable")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def run(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"isolated Docker operation failed: {args[1] if len(args) > 1 else args[0]}")
    return result


def main() -> int:
    global stage
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--test-user-id", required=True)
    parser.add_argument("--test-creator-id", required=True)
    args = parser.parse_args()
    ops = module("eros_native_pilot_ops", "eros_native_pilot_ops.py")
    secrets_module = module("eros_production_secrets", "eros-production-secrets.py")
    suffix = secrets.token_hex(6)
    container = f"eros-provision-test-{suffix}"
    owner_label = f"me36-eros-e2e-{suffix}"
    data_network = "gpoint_eros_data"
    embedding_network = "gpoint_eros_embeddings"
    created_networks: set[str] = set()
    started_container = False
    stage = "initialize"
    with tempfile.TemporaryDirectory(prefix="me36-native-eros-ops-") as temporary:
        root = Path(temporary)
        env_file = root / "eros.env"
        source_env = root / "production.env"
        run_dir = root / "run"
        backup_root = root / "backups"
        backup_root.mkdir(mode=0o700)
        try:
            for network in (data_network, embedding_network):
                existing = run("docker", "network", "inspect", network, check=False)
                if existing.returncode == 0:
                    raise RuntimeError("fixed Eros test network already exists; refusing to reuse it")
            run("docker", "network", "create", "--driver", "bridge", "--internal",
                "--label", "com.gpoint.eros.managed=true", data_network)
            created_networks.add(data_network)
            run("docker", "network", "create", "--driver", "bridge", "--internal",
                "--subnet", "172.31.254.0/29", "--gateway", "172.31.254.1",
                "--label", "com.gpoint.eros.managed=true", embedding_network)
            created_networks.add(embedding_network)
            run("docker", "run", "-d", "--name", container, "--label", f"com.gpoint.me36.run={owner_label}",
                "--network", data_network, "--network-alias", "gpoint-eros-db",
                "--env", "POSTGRES_USER=postgres", "--env", "POSTGRES_PASSWORD=synthetic-only",
                "--env", "POSTGRES_DB=postgres", POSTGRES_IMAGE)
            started_container = True
            for _ in range(60):
                if run("docker", "exec", container, "pg_isready", "-U", "postgres", check=False).returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("disposable PostgreSQL failed its readiness probe")
            run("docker", "exec", container, "createdb", "-U", "postgres", "gpoint")
            run("docker", "exec", container, "psql", "-Xq", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres",
                "-c", "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC")
            run("docker", "exec", container, "psql", "-Xq", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres",
                "-c", "REVOKE CONNECT ON DATABASE gpoint FROM PUBLIC")
            run("docker", "exec", container, "psql", "-Xq", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres",
                "-c", "REVOKE CONNECT ON DATABASE template1 FROM PUBLIC")
            source_env.write_text(f"AGATA_TENANT_ID={uuid.uuid4()}\n", encoding="utf-8")
            source_env.chmod(0o600)
            os.environ["EROS_PROVISION_TEST"] = "1"
            os.environ["EROS_PROVISION_TEST_POSTGRES_CONTAINER"] = container
            stage = "secret provision"
            identity = secrets_module.provision(
                env_file, source_sha=args.candidate_sha, image=args.image,
                source_env=source_env, pilot_test_user_id=args.test_user_id,
                pilot_test_creator_id=args.test_creator_id,
            )
            if identity != "EROS_SECRET_STORE=READY":
                raise RuntimeError("Eros secret provisioning did not report readiness")
            stage = "database and role provisioning"
            secrets_module.provision_database(env_file)
            values = ops._load_env(env_file)
            os.environ["EROS_NATIVE_OPS_TEST"] = "1"
            os.environ["EROS_NATIVE_OPS_TEST_POSTGRES_CONTAINER"] = container
            os.environ["EROS_NATIVE_OPS_TEST_RUN_DIR"] = str(run_dir)
            os.environ["EROS_NATIVE_OPS_TEST_SOURCE_ROOT"] = str(ROOT)
            stage = "runtime allowlist validation"
            ops.validate_pilot_runtime(values, args.test_user_id, args.test_creator_id, args.candidate_sha)
            stage = "stock image identity readback"
            ops.verify_stock_image(args.image, args.candidate_sha)
            stage = "ACL and private network readback"
            privilege_row = run("docker", "exec", container, "psql", "-XqAt", "-v", "ON_ERROR_STOP=1",
                "-U", "postgres", "-d", "postgres", "-c",
                "SELECT has_database_privilege('eros_runtime','eros','CONNECT')::text || '|' || has_database_privilege('eros_runtime','gpoint','CONNECT')::text || '|' || has_database_privilege('eros_migrator','gpoint','CONNECT')::text").stdout.strip()
            if privilege_row != "true|false|false":
                raise RuntimeError("Eros runtime database ACL isolation read-back failed")
            db_networks = json.loads(run("docker", "inspect", container, "--format", "{{json .NetworkSettings.Networks}}").stdout)
            if set(db_networks) != {data_network} or db_networks[data_network]["Aliases"].count("gpoint-eros-db") != 1:
                raise RuntimeError("PostgreSQL network attachment or alias isolation failed")
            port_bindings = json.loads(run("docker", "inspect", container, "--format", "{{json .HostConfig.PortBindings}}").stdout)
            if port_bindings:
                raise RuntimeError("isolated PostgreSQL test service published a host port")
            network_facts = json.loads(run("docker", "network", "inspect", embedding_network).stdout)[0]
            if network_facts.get("Internal") is not True or network_facts.get("Containers"):
                raise RuntimeError("embedding network is not isolated from the PostgreSQL test service")
            stage = "verified Eros backup"
            backup = backup_root / f"eros-before-{args.candidate_sha}.dump"
            facts = ops.create_verified_backup(backup, args.candidate_sha,
                                               values["EROS_DATABASE_IDENTITY"], backup_root=backup_root)
            stage = "isolated restore probe"
            ops.restore_probe(backup, "eros_restore_probe_" + secrets.token_hex(8), backup_root=backup_root)
            stage = "stock SQLx migration"
            ops.run_stock_migration(args.image, args.candidate_sha, values)
            stage = "stock synthetic persona seed"
            genome_id = ops.run_stock_persona_seed(args.image, args.candidate_sha, values)
            if not ops.UUID.fullmatch(genome_id):
                raise RuntimeError("stock synthetic persona seed did not return an exact genome UUID")
            migrations = ops.verify_sqlx_migrations()
            if migrations <= 0:
                raise RuntimeError("stock SQLx migration ledger is empty")
            stage = "backup rollback"
            ops.restore_eros(backup, backup_root=backup_root)
            ledger = run("docker", "exec", container, "psql", "-XqAt", "-v", "ON_ERROR_STOP=1",
                "-U", "postgres", "-d", "eros", "-c",
                "SELECT to_regclass('public._sqlx_migrations') IS NULL").stdout.strip()
            if ledger != "t":
                raise RuntimeError("rollback did not restore the pre-migration Eros database")
            ops.run_stock_migration(args.image, args.candidate_sha, values)
            ops.run_stock_persona_seed(args.image, args.candidate_sha, values)
            ops.verify_sqlx_migrations()
            bad_backup = root / f"eros-before-{args.candidate_sha}.bad.dump"
            bad_manifest = bad_backup.with_suffix(".json")
            bad_manifest.write_bytes(backup.with_suffix(".json").read_bytes())
            bad_manifest.chmod(0o600)
            bad_backup.write_bytes(backup.read_bytes() + b"tamper")
            bad_backup.chmod(0o600)
            try:
                ops.verify_backup(bad_backup, root)
            except ops.PilotGateError:
                pass
            else:
                raise RuntimeError("tampered Eros backup was accepted")
            try:
                ops.cleanup_database("wrong-pilot-identity")
            except ops.PilotGateError:
                pass
            else:
                raise RuntimeError("cleanup accepted a mismatched Eros database identity")
            stage = "identity-bound cleanup"
            ops.cleanup_database(values["EROS_DATABASE_IDENTITY"])
            run("docker", "network", "disconnect", data_network, container)
            ops.cleanup_managed_networks()
            ops.remove_runtime_file(env_file)
            gpoint_exists = run("docker", "exec", container, "psql", "-XqAt", "-U", "postgres", "-d", "postgres",
                "-c", "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname='gpoint')").stdout.strip()
            if gpoint_exists != "t":
                raise RuntimeError("cleanup modified the protected GPoint database")
            print(f"EROS_NATIVE_POSTGRES_E2E=PASS stock_migrations={migrations} backup_sha256={facts['sha256']}")
            print("EROS_NATIVE_POSTGRES_ACL=PASS Eros runtime can connect only to eros")
            print("EROS_NATIVE_POSTGRES_RESTORE_ROLLBACK=PASS isolated_probe=PASS tamper_rejected=PASS")
            print("EROS_NATIVE_STOCK_PERSONA_SEED=PASS exact_single_genome=true")
            print("EROS_NATIVE_PILOT_CLEANUP=PASS identity_mismatch_rejected=PASS gpoint_database_preserved=PASS")
            print("EROS_PRIVATE_NETWORKS=PASS embeddings_internal=true ollama_public_port=none")
            print("EROS_POSTGRES_INGRESS=PASS published_ports=none")
        finally:
            if started_container:
                run("docker", "rm", "-f", container, check=False)
            for network in reversed((data_network, embedding_network)):
                if network in created_networks:
                    run("docker", "network", "rm", network, check=False)
            for key in ("EROS_PROVISION_TEST", "EROS_PROVISION_TEST_POSTGRES_CONTAINER",
                        "EROS_NATIVE_OPS_TEST", "EROS_NATIVE_OPS_TEST_POSTGRES_CONTAINER",
                        "EROS_NATIVE_OPS_TEST_RUN_DIR", "EROS_NATIVE_OPS_TEST_SOURCE_ROOT"):
                os.environ.pop(key, None)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(failure_summary(stage, error), file=sys.stderr)
        raise SystemExit(1)
