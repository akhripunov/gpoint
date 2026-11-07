#!/usr/bin/env python3
"""Create only the empty, isolated Layer7 GPoint staging database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
import subprocess
from urllib.parse import unquote, urlsplit

ENV_PATH = Path("/opt/gpoint/env/staging.env")
DATABASE = "gpoint_staging"
ROLE = "gpoint_staging_runtime"


def read_database_url(path: Path = ENV_PATH) -> str:
    info = path.stat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("staging environment file permissions are unsafe")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key in values:
            raise ValueError("staging environment file is malformed")
        values[key] = value
    url = values.get("DATABASE_URL", "")
    parsed = urlsplit(url)
    if (parsed.scheme != "postgresql" or parsed.hostname != "shared_postgres_1"
            or parsed.path != "/gpoint_staging" or parsed.username != ROLE
            or not parsed.password or "\n" in url or "\r" in url):
        raise ValueError("staging database identity is invalid")
    return url


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise ValueError("database identifier is invalid")
    return '"' + value.replace('"', '""') + '"'


def _literal(value: str) -> str:
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("database password is invalid")
    return "'" + value.replace("'", "''") + "'"


def bootstrap_database(url: str, runner=subprocess.run) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "postgresql" or parsed.hostname != "shared_postgres_1"
            or parsed.path != "/gpoint_staging" or parsed.username != ROLE
            or not parsed.password):
        raise ValueError("staging database identity is invalid")
    role = _identifier(ROLE)
    database = _identifier(DATABASE)
    result = runner(
        ["docker", "exec", "-i", "shared_postgres_1", "sh", "-ec",
         'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres'],
        input=("SELECT json_build_object("
               "'database', (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='gpoint_staging'),"
               "'publicConnect', coalesce((SELECT EXISTS(SELECT 1 FROM aclexplode(coalesce(datacl,acldefault('d',datdba))) x "
               "WHERE x.grantee=0 AND x.privilege_type='CONNECT') FROM pg_database WHERE datname='gpoint_staging'),false),"
               "'otherConnect', coalesce((SELECT json_agg(rolname ORDER BY rolname) FROM pg_roles "
               "WHERE NOT rolsuper AND rolname <> 'gpoint_staging_runtime' AND has_database_privilege(oid, "
               "(SELECT oid FROM pg_database WHERE datname='gpoint_staging'),'CONNECT')), '[]'::json),"
               "'role', (SELECT json_build_object('superuser',rolsuper,'createdb',rolcreatedb,'createrole',rolcreaterole,"
               "'bypassrls',rolbypassrls,'inherit',rolinherit,'replication',rolreplication,"
               "'memberships',(SELECT count(*) FROM pg_auth_members WHERE member=pg_roles.oid)) "
               "FROM pg_roles WHERE rolname='gpoint_staging_runtime'))::text;\n").encode(),
        capture_output=True, check=True,
    )
    try:
        facts = json.loads(result.stdout.decode().strip())
    except (ValueError, AttributeError, json.JSONDecodeError):
        raise RuntimeError("staging database inventory is malformed") from None
    db_owner = facts.get("database")
    role_facts = facts.get("role")
    if db_owner not in (None, ROLE) or facts.get("publicConnect") is True or facts.get("otherConnect") != []:
        raise RuntimeError("unsafe existing staging identity: database owner or ACL differs")
    if db_owner is not None and role_facts is None:
        raise RuntimeError("unsafe existing staging identity: database owner role is missing")
    if role_facts is not None and (
            role_facts.get("superuser") is not False or role_facts.get("createdb") is not False
            or role_facts.get("createrole") is not False or role_facts.get("bypassrls") is not False
            or role_facts.get("inherit") is not False or role_facts.get("replication") is not False
            or role_facts.get("memberships") != 0):
        raise RuntimeError("unsafe existing staging identity: role is privileged")
    role_sql = (f"ALTER ROLE {role} LOGIN PASSWORD {_literal(unquote(parsed.password))} "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
                if role_facts is not None else
                f"CREATE ROLE {role} LOGIN PASSWORD {_literal(unquote(parsed.password))} "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n")
    database_sql = (f"CREATE DATABASE {database} OWNER {role} ENCODING 'UTF8';\n"
                    f"REVOKE ALL ON DATABASE {database} FROM PUBLIC;\n"
                    f"GRANT CONNECT, TEMPORARY ON DATABASE {database} TO {role};\n"
                    if db_owner is None else "")
    sql = (role_sql + database_sql).encode()
    runner(
        ["docker", "exec", "-i", "shared_postgres_1", "sh", "-ec",
         'exec psql -Xq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres'],
        input=sql, capture_output=True, check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ENV_PATH)
    args = parser.parse_args()
    try:
        bootstrap_database(read_database_url(args.env_file))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print("LAYER7_STAGING_DATABASE=BLOCKED reason=identity_or_acl_gate", file=__import__("sys").stderr)
        return 1
    print("LAYER7_STAGING_DATABASE=PASS database=gpoint_staging owner=gpoint_staging_runtime")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
