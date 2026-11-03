#!/usr/bin/env python3
"""Read-only fail-closed verifier for an installed synthetic Eros pilot."""
from __future__ import annotations
import argparse, hashlib, ipaddress, json, os, re, subprocess, sys
from pathlib import Path

MARKER_SCHEMA = "gpoint-eros-native-pilot-state/v1"
EROS_UPSTREAM_SHA = "d1a140083321687212fb7b17c09cc0c3c9639f5e"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
SHA40 = re.compile(r"^[0-9a-f]{40}$")
PUBLIC_SCHEMA_CREATE_SQL = (
    "EXISTS(SELECT 1 FROM aclexplode(COALESCE(nspacl,acldefault('n',nspowner))) a "
    "WHERE a.grantee=0 AND a.privilege_type='CREATE')"
)
PUBLIC_CONNECT_SQL = (
    "EXISTS(SELECT 1 FROM aclexplode(COALESCE(datacl,acldefault('d',datdba))) a "
    "WHERE a.grantee=0 AND a.privilege_type='CONNECT')"
)

class RuntimeStateError(ValueError):
    pass

def require(ok, message):
    if not ok:
        raise RuntimeStateError(message)

def validate_database_acl(database):
    require(database == {"name":"eros","owner":"eros_ddl","allowConnections":True,
                         "publicConnect":False,"publicSchemaCreate":False,"runtimeSchemaCreate":False},
            "Eros database owner or public ACL is invalid")

def validate(marker, api, engine, relay, postgres, networks):
    require(marker.get("schema") == MARKER_SCHEMA and marker.get("status") == "synthetic-pilot-ready", "pilot marker is absent or not ready")
    source, version = marker.get("sourceSha"), marker.get("imageVersion")
    require(isinstance(source, str) and SHA40.fullmatch(source), "pilot source SHA is invalid")
    require(isinstance(version, str) and version.startswith(source + "-"), "pilot version is invalid")
    user, creator = marker.get("testUserId"), marker.get("testCreatorId")
    require(UUID.fullmatch(user or "") and UUID.fullmatch(creator or "") and user != creator, "pilot UUID pair is invalid")
    for key in ("pilotAppDigest", "backupSha256"):
        require(bool(re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", marker.get(key, ""))), f"pilot {key} is invalid")
    image = marker.get("erosImage")
    require(isinstance(image, str) and re.fullmatch(r"ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", image), "Eros image is not digest-pinned")
    env = api.get("env", {})
    exact = {"EROS_ENABLED":"true", "EROS_PRODUCTION_TEST_MODE":"true", "EROS_DATA_MODE":"synthetic",
             "EROS_PRODUCTION_TEST_USER_IDS":user, "EROS_PRODUCTION_TEST_CREATOR_IDS":creator}
    require(api.get("running") is True and api.get("health") == "healthy", "GPoint API is unhealthy")
    require(all(env.get(k) == v for k,v in exact.items()), "API synthetic mode or exact UUID allowlist mismatch")
    require(re.search(r"@[^/:]+/gpoint(?:\?|$)", env.get("DATABASE_URL","")) is not None, "API is not using the GPoint database")
    require(env.get("EROS_OLLAMA_EMBEDDING_URL") == "http://172.31.254.1:11434/api/embed",
            "API embedding URL is not pinned to the private Mac relay")
    api_nets = api.get("networks", {})
    require({"gpoint_app","shared_internal","gpoint_eros_embeddings"}.issubset(api_nets) and "gpoint_eros_data" not in api_nets,
            "API network boundary mismatch")
    ip = ipaddress.ip_address(api_nets["gpoint_eros_embeddings"].get("ip",""))
    require(ip in ipaddress.ip_network("172.31.254.0/29") and str(ip) != "172.31.254.1", "API embedding address is outside relay subnet")
    require(engine.get("running") is True and engine.get("health") == "healthy", "Eros engine is unhealthy")
    require(engine.get("revision") == EROS_UPSTREAM_SHA and engine.get("integrationRevision") == source and engine.get("patchSetSha256") == "none",
            "Eros engine is patched or source identity differs")
    require(image in engine.get("repoDigests", []) and set(engine.get("networks",{})) == {"gpoint_app","gpoint_eros_data"},
            "Eros engine digest or network boundary mismatch")
    require(bool(re.fullmatch(r"[0-9a-f]{64}", engine.get("id",""))), "Eros engine container identity is incomplete")
    require(relay.get("running") is True and relay.get("networkMode") == "host" and relay.get("revision") == source
            and len(relay.get("repoDigests",[])) == 1 and bool(re.fullmatch(r"[0-9a-f]{64}",relay.get("id",""))),
            "protected Ollama relay is unavailable or changed")
    require(postgres.get("running") is True and "gpoint_eros_data" in postgres.get("networks",{})
            and "gpoint-eros-db" in postgres["networks"]["gpoint_eros_data"].get("aliases",[]),
            "existing PostgreSQL is not on private Eros data network")
    validate_database_acl(postgres.get("database"))
    expected_roles = {
      "eros_ddl":{"login":False,"superuser":False,"createDatabase":False,"createRole":False,"bypassRls":False},
      "eros_runtime":{"login":True,"superuser":False,"createDatabase":False,"createRole":False,"bypassRls":True},
      "eros_migrator":{"login":True,"superuser":False,"createDatabase":False,"createRole":False,"bypassRls":False}}
    require(postgres.get("roles") == expected_roles, "Eros role privileges mismatch")
    require(postgres.get("runtimeConnect") is True and postgres.get("runtimeCreate") is False and
            postgres.get("runtimeSchemaCreate") is False and
            postgres.get("migratorConnect") is True and postgres.get("migratorCreate") is True and
            postgres.get("foreignConnect") is False and postgres.get("memberships") == [], "Eros database grants escape isolation")
    require(isinstance(postgres.get("migrationCount"), int) and postgres["migrationCount"] > 0 and
            postgres.get("migrationFailures") == 0 and isinstance(postgres.get("migrationHead"), str)
            and postgres["migrationHead"], "SQLx migration ledger is missing or failed")
    require(bool(re.fullmatch(r"[0-9a-f]{64}", postgres.get("catalogSha256",""))), "Eros catalog identity is incomplete")
    for name in ("gpoint_eros_data","gpoint_eros_embeddings"):
        net = networks.get(name,{})
        require(net.get("internal") is True and net.get("driver") == "bridge" and
                net.get("labels",{}).get("com.gpoint.eros.managed") == "true", f"{name} is not internally managed")
    require(networks["gpoint_eros_embeddings"].get("subnet") == "172.31.254.0/29" and
            networks["gpoint_eros_embeddings"].get("gateway") == "172.31.254.1", "relay network identity mismatch")
    # Exclude API container identity and dynamic IP: both legitimately change during Kamal rolling deploy.
    stable = {"mode":"synthetic-eros-pilot",
      "pilot":{k:marker[k] for k in ("sourceSha","imageVersion","testUserId","testCreatorId","erosImage","pilotAppDigest","backupSha256")},
      "engine":{k:engine[k] for k in ("id","name","revision","integrationRevision","patchSetSha256","repoDigests","networks")},
      "relay":{k:relay[k] for k in ("id","name","revision","repoDigests","networkMode")},
      "database":{k:postgres[k] for k in ("id","database","roles","runtimeConnect","runtimeCreate","runtimeSchemaCreate","migratorConnect","migratorCreate","foreignConnect","memberships","migrationCount","migrationFailures","migrationHead","catalogSha256")},
      "networks":{k:networks[k] for k in ("gpoint_eros_data","gpoint_eros_embeddings")}, "apiContract":exact}
    stable["sha256"] = hashlib.sha256(json.dumps(stable,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    return stable

def command(args):
    result = subprocess.run(args,text=True,capture_output=True,check=False)
    if result.returncode: raise RuntimeStateError("read-only runtime inspection failed")
    return result.stdout.strip()

def collect(marker_path=Path("/opt/gpoint/env/eros-pilot-state.json")):
    path=Path(marker_path)
    require(not path.is_symlink() and path.is_file(),"pilot marker missing or unsafe")
    st=path.stat(); require(st.st_uid == 0 and st.st_mode & 0o077 == 0 and st.st_nlink == 1,"pilot marker permissions unsafe")
    marker=json.loads(path.read_text()); version=marker.get("imageVersion","")
    def inspect(name):
        row=json.loads(command(["docker","inspect",name]))[0]
        image=json.loads(command(["docker","image","inspect",row["Image"]]))[0]
        return row,image,row.get("NetworkSettings",{}).get("Networks",{})
    apis=[x for x in command(["docker","ps","--format","{{.Names}}"]).splitlines() if x.startswith("gpoint-core-api-")]
    require(len(apis)==1,"exactly one current GPoint API must run")
    ar,_,an=inspect(apis[0]); env=dict(x.split("=",1) for x in ar.get("Config",{}).get("Env",[]) if "=" in x)
    api={"name":apis[0],"running":ar.get("State",{}).get("Running"),"health":ar.get("State",{}).get("Health",{}).get("Status"),
         "env":env,"networks":{k:{"ip":v.get("IPAddress")} for k,v in an.items()}}
    relay_probe="fetch('http://172.31.254.1:11434/healthz').then(async r=>{if(!r.ok)throw Error();const t=await fetch('http://172.31.254.1:11434/api/tags');if(!t.ok)throw Error();const j=await t.json();if(!j.models?.some(m=>m.name==='qwen3-embedding:0.6b'&&m.digest==='ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d'))throw Error()}).catch(()=>process.exit(1))"
    command(["docker","exec",apis[0],"node","-e",relay_probe])
    en,ei,enn=inspect("gpoint-eros-pilot-engine-"+version); rn,ri,_=inspect("gpoint-eros-relay-relay-"+version)
    labels=ei.get("Config",{}).get("Labels") or {}
    engine={"id":en.get("Id"),"name":"gpoint-eros-pilot-engine-"+version,"running":en.get("State",{}).get("Running"),
      "health":en.get("State",{}).get("Health",{}).get("Status"),"revision":labels.get("org.opencontainers.image.revision"),
      "integrationRevision":labels.get("com.gpoint.integration.revision"),"patchSetSha256":labels.get("com.gpoint.eros.patch-set.sha256"),
      "repoDigests":ei.get("RepoDigests") or [],"networks":{k:{} for k in enn}}
    relay={"id":rn.get("Id"),"name":"gpoint-eros-relay-relay-"+version,"running":rn.get("State",{}).get("Running"),
      "revision":(ri.get("Config",{}).get("Labels") or {}).get("org.opencontainers.image.revision"),
      "repoDigests":ri.get("RepoDigests") or [],"networkMode":rn.get("HostConfig",{}).get("NetworkMode")}
    pg,_,pgn=inspect("shared_postgres_1")
    # This query is READ ONLY and returns only ACL, migration and catalog facts.
    sql="""BEGIN READ ONLY; SELECT json_build_object(
      'database',(SELECT json_build_object('name',datname,'owner',pg_get_userbyid(datdba),'allowConnections',datallowconn,
       'publicConnect',PUBLIC_CONNECT_EXPR,'publicSchemaCreate',PUBLIC_SCHEMA_CREATE_EXPR,
       'runtimeSchemaCreate',has_schema_privilege('eros_runtime','public','CREATE'))
       FROM pg_database CROSS JOIN pg_namespace WHERE datname='eros' AND nspname='public'),
      'roles',(SELECT json_object_agg(rolname,json_build_object('login',rolcanlogin,'superuser',rolsuper,'createDatabase',rolcreatedb,'createRole',rolcreaterole,'bypassRls',rolbypassrls)) FROM pg_roles WHERE rolname IN ('eros_ddl','eros_runtime','eros_migrator')),
      'runtimeConnect',has_database_privilege('eros_runtime','eros','CONNECT'),'runtimeCreate',has_database_privilege('eros_runtime','eros','CREATE'),
      'runtimeSchemaCreate',has_schema_privilege('eros_runtime','public','CREATE'),
      'migratorConnect',has_database_privilege('eros_migrator','eros','CONNECT'),'migratorCreate',has_database_privilege('eros_migrator','eros','CREATE'),
      'foreignConnect',EXISTS(SELECT 1 FROM pg_database d WHERE d.datallowconn AND d.datname<>'eros' AND
        (has_database_privilege('eros_runtime',d.datname,'CONNECT') OR has_database_privilege('eros_migrator',d.datname,'CONNECT'))),
      'memberships',COALESCE((SELECT json_agg(1) FROM pg_auth_members m JOIN pg_roles r ON r.oid IN (m.roleid,m.member) WHERE r.rolname IN ('eros_ddl','eros_runtime','eros_migrator')),'[]'::json),
      'migrationCount',(SELECT count(*) FROM public._sqlx_migrations WHERE success),
      'migrationFailures',(SELECT count(*) FROM public._sqlx_migrations WHERE NOT success),
      'migrationHead',(SELECT version::text FROM public._sqlx_migrations WHERE success ORDER BY version DESC LIMIT 1),
      'catalogFacts',(SELECT string_agg(concat_ws('|',n.nspname,c.relname,a.attname,a.atttypid::text),E'\\n' ORDER BY n.nspname,c.relname,a.attnum)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        WHERE n.nspname IN ('public','engine'))::text FROM pg_catalog.pg_database LIMIT 1; COMMIT;""".replace(
            "PUBLIC_CONNECT_EXPR", PUBLIC_CONNECT_SQL
        ).replace("PUBLIC_SCHEMA_CREATE_EXPR", PUBLIC_SCHEMA_CREATE_SQL)
    proc=subprocess.run(["docker","exec","-i","shared_postgres_1","sh","-ec",'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d eros'],
      input=sql,text=True,capture_output=True,check=False)
    if proc.returncode: raise RuntimeStateError("read-only Eros PostgreSQL verification failed")
    facts=json.loads(proc.stdout.strip())
    facts["catalogSha256"]=hashlib.sha256((facts.pop("catalogFacts") or "").encode()).hexdigest()
    backup=Path(marker.get("backup",""))
    require(backup.is_absolute() and backup.parent == Path("/opt/gpoint/backups/eros-pilot"), "pilot backup path is outside its protected directory")
    ops_path=Path("/opt/gpoint/releases")/marker["sourceSha"]/"source/scripts/eros_native_pilot_ops.py"
    import importlib.util
    spec=importlib.util.spec_from_file_location("_eros_pilot_ops",ops_path)
    require(spec is not None and spec.loader is not None, "pilot backup verifier is unavailable")
    ops=importlib.util.module_from_spec(spec); spec.loader.exec_module(ops)
    try:
        backup_facts=ops.verify_backup(backup)
    except (ops.PilotGateError, OSError):
        raise RuntimeStateError("pilot rollback backup verification failed") from None
    require(backup_facts.get("sourceSha") == marker["sourceSha"], "pilot rollback backup source identity differs")
    marker["backupSha256"]=backup_facts["sha256"]
    postgres={"id":pg["Id"],"running":pg.get("State",{}).get("Running"),"networks":{k:{"aliases":v.get("Aliases",[])} for k,v in pgn.items()},**facts}
    nets={}
    for name in ("gpoint_eros_data","gpoint_eros_embeddings"):
        row=json.loads(command(["docker","network","inspect",name]))[0]; cfg=(row.get("IPAM",{}).get("Config") or [{}])[0]
        nets[name]={"id":row.get("Id"),"driver":row.get("Driver"),"internal":row.get("Internal"),"labels":row.get("Labels") or {},
                    "subnet":cfg.get("Subnet"),"gateway":cfg.get("Gateway")}
    return validate(marker,api,engine,relay,postgres,nets)

def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--output",type=Path,required=True); args=parser.parse_args()
    try:
        state=collect(); args.output.write_text(json.dumps(state,sort_keys=True,separators=(",",":"))+"\n"); os.chmod(args.output,0o600)
    except (RuntimeStateError,OSError,ValueError,KeyError,TypeError,json.JSONDecodeError) as error:
        print(f"EROS_ACTIVE_STATE=BLOCKED:{error}",file=sys.stderr); return 2
    print(f"EROS_ACTIVE_STATE=PASS sha256={state['sha256']}"); return 0

if __name__=="__main__": raise SystemExit(main())
