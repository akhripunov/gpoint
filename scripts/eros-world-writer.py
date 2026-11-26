#!/usr/bin/env python3
"""Protected backup-bound provisioning of the GPoint Eros world writer."""
import argparse
import base64
import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DB = load_module("eros_native_database", "eros-native-database.py")
SEC = load_module("eros_production_secrets", "eros-production-secrets.py")


def scram_verifier(password):
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 4096)
    stored = hashlib.sha256(hmac.digest(salted, b"Client Key", "sha256")).digest()
    server = hmac.digest(salted, b"Server Key", "sha256")
    encode = lambda data: base64.b64encode(data).decode("ascii")
    return f"SCRAM-SHA-256$4096:{encode(salt)}${encode(stored)}:{encode(server)}"


def assert_acl(login):
    return ("DO $world_acl$ BEGIN IF (" + DB.world_writer_acl_query(login)
            + ") IS DISTINCT FROM true THEN RAISE EXCEPTION "
            "'world writer ACL contract failed'; END IF; END $world_acl$;\n")


def provision(candidate, image, run_id, acl_sha, app_image):
    if os.geteuid() != 0:
        raise RuntimeError("trusted production root context required")
    if not re.fullmatch(r"ghcr\.io/akhripunov/gpoint-runtime@sha256:[0-9a-f]{64}", app_image):
        raise ValueError("exact Core image digest required")
    if not re.fullmatch(r"[0-9a-f]{64}", acl_sha):
        raise ValueError("exact writer ACL checksum required")
    raw = (ROOT / "services/eros-engine/world-enrollment/writer-acl.sql").read_bytes()
    if hashlib.sha256(raw).hexdigest() != acl_sha:
        raise RuntimeError("writer ACL checksum mismatch")
    acl = raw.decode("utf8").rstrip()
    if not acl.endswith("COMMIT;"):
        raise RuntimeError("writer ACL must be transactional")
    DB.lifecycle("verify-applied", candidate, image, run_id)
    DB._validate_v281_history(DB._ledger("eros"))
    facts = DB._database_facts(DB._read_env()["EROS_DATABASE_IDENTITY"])
    role_exists = facts.get("worldWriterExists") is True
    path = SEC.WORLD_WRITER_ENV_PATH
    values = SEC.read_world_writer_env(path)
    if not values:
        if role_exists:
            raise RuntimeError("existing world writer has no protected credential; no rotation")
        if path.parent.is_symlink() or path.parent.stat().st_uid != 0 or path.parent.stat().st_mode & 0o077:
            raise RuntimeError("protected writer environment directory is unsafe")
        password = secrets.token_urlsafe(48)
        dsn = f"postgresql://eros_world_writer:{password}@gpoint-eros-db:5432/eros"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write("EROS_WORLD_ENROLLMENT_DATABASE_URL=" + dsn + "\n")
            out.flush()
            os.fsync(out.fileno())
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        values = SEC.read_world_writer_env(path)
    if not role_exists:
        password = urlsplit(values["EROS_WORLD_ENROLLMENT_DATABASE_URL"]).password
        verifier = scram_verifier(password)
        # New role, exact effective ACL, login, and readback commit atomically.
        sql = (acl.removesuffix("COMMIT;") + "\n" + assert_acl(False)
               + "ALTER ROLE eros_world_writer LOGIN PASSWORD '" + verifier + "';\n"
               + assert_acl(True) + "COMMIT;")
        DB._docker_psql("eros", sql)
    DB.verify_world_writer()
    identity = DB._run(["docker", "image", "inspect", "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}', app_image]).stdout.strip()
    if identity != candidate:
        raise RuntimeError("Core image revision does not match exact candidate")
    check = """const {PrismaClient}=require('@prisma/client');
const db=new PrismaClient({datasourceUrl:process.env.EROS_WORLD_ENROLLMENT_DATABASE_URL,log:[]});
(async()=>{try{const x=await db.$queryRawUnsafe('SELECT current_user AS role,current_database() AS db');if(x.length!==1||x[0].role!=='eros_world_writer'||x[0].db!=='eros')throw Error('identity');console.log('WORLD_WRITER_CONNECTION=PASS')}catch(e){process.exitCode=1}finally{await db.$disconnect()}})();"""
    result = DB._run(["docker","run","--rm","--network","gpoint_eros_data","--read-only",
        "--tmpfs","/tmp","--cap-drop","ALL","--security-opt","no-new-privileges",
        "--env-file",str(path),"--workdir","/app/apps/api","--entrypoint","node",
        app_image,"-e",check],timeout=30)
    if result.stdout.strip() != "WORLD_WRITER_CONNECTION=PASS":
        raise RuntimeError("candidate world writer connection was not verified")
    SEC.write_native_api_env()
    print("EROS_WORLD_WRITER_PROVISION=PASS")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candidate", required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--acl-sha", required=True)
    p.add_argument("--app-image", required=True)
    args = p.parse_args()
    try:
        provision(args.candidate, args.image, args.run_id, args.acl_sha, args.app_image)
    except Exception:
        print("EROS_WORLD_WRITER_PROVISION=BLOCKED; inspect protected state read-only", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
