#!/usr/bin/env bash
# One-shot GPoint Bot bootstrap inside the protected existing release transaction.
set -euo pipefail

authorization=".release-authorizations/gpoint-bot-restore-20261123-001.json"
if [[ "$GITHUB_EVENT_NAME" != push || "$GITHUB_REF" != refs/heads/main ]]; then
  echo "GPOINT_BOT_BOOTSTRAP=SKIPPED event"
  exit 0
fi
[[ "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ && "$GITHUB_EVENT_BEFORE" =~ ^[0-9a-f]{40}$ ]] || {
  echo "BOT_BOOTSTRAP=BLOCKED invalid push identities" >&2; exit 1;
}
# A new, specifically approved record in THIS push is the sole activation trigger.
change="$(git diff --name-status --no-renames "$GITHUB_EVENT_BEFORE" "$GITHUB_SHA" -- "$authorization")"
if [[ "$change" != $'A\t'"$authorization" ]]; then
  echo "GPOINT_BOT_BOOTSTRAP=SKIPPED no new authorization"
  exit 0
fi
: "${GPOINT_PRODUCTION_HOST:?}"
: "${KAMAL_REGISTRY_PASSWORD:?}"
: "${GPOINT_SSH_KEY:?}"
: "${GPOINT_KNOWN_HOSTS_FILE:?}"
[[ "$GPOINT_PRODUCTION_HOST" == "95.133.236.59" ]] || { echo "BOT_BOOTSTRAP=BLOCKED host" >&2; exit 1; }
test -f "$GPOINT_SSH_KEY" && test -f "$GPOINT_KNOWN_HOSTS_FILE"

before="$(ssh -T "root@$GPOINT_PRODUCTION_HOST" python3 - < "$GITHUB_WORKSPACE/scripts/verify-kamal-production-baseline.py")"
details="$(python3 - "$authorization" "$before" <<'PY'
import json, re, sys
record=json.load(open(sys.argv[1], encoding="utf-8"))
live=json.loads(sys.argv[2])
allowed={".github/workflows/gpoint-release.yml", "scripts/gpoint-bot-bootstrap.sh", "scripts/native_release_intent.py",
         "tests/test_gpoint_bot_bootstrap.py", ".release-authorizations/gpoint-bot-restore-20261123-001.json"}
risk=record.get("risk_contracts",{}).get("release_infrastructure",{})
if (record.get("schema_version")!=3 or record.get("project")!="gpoint"
    or record.get("authorization_id")!="gpoint-bot-restore-20261123-001"
    or record.get("authorization_mode")!="TASK_SCOPED_AUTO"
    or record.get("owner_instruction_scope")!="FULL_PRODUCTION_CYCLE"
    or record.get("environment")!="production" or record.get("state")!="ACTIVE"
    or record.get("revoked") is not False
    or record.get("production_baseline_sha")!="66ed5fcd61af29dabadc2948886ccb40ee1c2f33"
    or set(record.get("allowed_paths",[]))!=allowed
    or set(risk.get("allowed_paths",[]))!=allowed):
    raise SystemExit("BOT_BOOTSTRAP=BLOCKED authorization or risk contract")
roles=live.get("roles",{})
expected={"gpoint-core-api":"a6c1934ce03af070aa5119a6026373f823323195",
 "gpoint-core-worker":"a6c1934ce03af070aa5119a6026373f823323195",
 "gpoint-miniapp-web":"66ed5fcd61af29dabadc2948886ccb40ee1c2f33",
 "gpoint-eros-pilot-engine":"f389145043d82eaeb1ff49d07e0ea2d60fb70a2c"}
if (set(roles)!=set(expected) or set(live.get("missing_roles",[]))!={"gpoint-core-bot"}
    or live.get("mixed_components")
    or any(roles[k].get("source_sha")!=v for k,v in expected.items())):
    raise SystemExit("BOT_BOOTSTRAP=BLOCKED changed live component baseline")
api,worker=roles["gpoint-core-api"],roles["gpoint-core-worker"]
if any(api[k]!=worker[k] for k in ("version","image_id","repo_digest","source_sha")):
    raise SystemExit("BOT_BOOTSTRAP=BLOCKED Core identity mismatch")
digest=api["repo_digest"].removeprefix("ghcr.io/akhripunov/gpoint-runtime@")
if (not re.fullmatch(r"sha256:[0-9a-f]{64}",digest)
    or not re.fullmatch(r"[0-9a-f]{40}-[0-9]+-[0-9]+",api["version"])):
    raise SystemExit("BOT_BOOTSTRAP=BLOCKED Core version/digest invalid")
print("\t".join((api["version"],api["source_sha"],digest)))
PY
)"
IFS=$'\t' read -r live_core_version live_core_sha live_core_digest <<< "$details"

# VPS-only Telegram/API and credential checks: NEVER print tokens or secret URLs.
ssh -T "root@$GPOINT_PRODUCTION_HOST" python3 - <<'PY'
import json, pathlib, subprocess, sys, urllib.parse, urllib.request
def stop(reason):
    print("BOT_BOOTSTRAP_PREFLIGHT=BLOCKED "+reason, file=sys.stderr)
    raise SystemExit(1)
path=pathlib.Path("/opt/gpoint/env/production.env")
if not path.is_file(): stop("environment absent")
env={}
try:
    for row in path.read_text().splitlines():
        row=row.strip()
        if not row or row.startswith("#") or "=" not in row: continue
        k,v=row.split("=",1)
        v=v.strip()
        if len(v)>1 and v[0]==v[-1] and v[0] in ("'",'"'): v=v[1:-1]
        env[k.strip()]=v
except (OSError,UnicodeError):
    stop("environment unreadable")
for k in ("TELEGRAM_BOT_TOKEN","TELEGRAM_WEBHOOK_SECRET","PUBLIC_APP_URL","GPOINT_INGEST_URL"):
    if not env.get(k): stop(k+" missing")
public=urllib.parse.urlparse(env["PUBLIC_APP_URL"])
ingest=urllib.parse.urlparse(env["GPOINT_INGEST_URL"])
if not (public.scheme=="https" and public.hostname=="gpoint.one"
    and public.path in ("","/") and public.username is None and public.password is None
    and ingest.scheme=="https" and ingest.hostname=="gpoint.one"
    and ingest.path=="/api/internal/telegram/update"
    and ingest.username is None and ingest.password is None):
    stop("unapproved public or ingest destination")
ps=subprocess.run(["ps","-eo","args="],capture_output=True,text=True)
if ps.returncode or any("node apps/bot/dist/main.js" in s for s in ps.stdout.splitlines()):
    stop("existing bot process or unreadable process inventory")
docker=subprocess.run(["docker","ps","--format","{{.Names}}"],capture_output=True,text=True)
if docker.returncode or any("gpoint-core-bot" in s for s in docker.stdout.splitlines()):
    stop("existing bot container or unreadable Docker inventory")
token=env["TELEGRAM_BOT_TOKEN"]
def api(method):
    try:
        with urllib.request.urlopen("https://api.telegram.org/bot"+token+"/"+method,timeout=9) as resp:
            result=json.load(resp)
    except Exception: stop("Telegram API or credentials unavailable")
    if not result.get("ok") or not isinstance(result.get("result"),dict):
        stop("Telegram API check failed")
    return result["result"]
bot=api("getMe")
if not bot.get("is_bot"): stop("Telegram identity not a bot")
expected=env.get("TELEGRAM_BOT_USERNAME","").lstrip("@")
if expected and bot.get("username","").lower()!=expected.lower():
    stop("Telegram username does not match configured bot")
if api("getWebhookInfo").get("url"):
    stop("active Telegram webhook conflicts with long polling")
print("BOT_BOOTSTRAP_PREFLIGHT=PASS")
PY

# Boot ONLY the previously absent Bot using the exact installed Core image.
GPOINT_APP_SOURCE_SHA="$live_core_sha" GPOINT_COMMIT_SHA="$live_core_sha" \
GPOINT_KAMAL_EXPECTED_DIGEST="$live_core_digest" GPOINT_KAMAL_SERVICE=gpoint-core KAMAL_ROLES=bot \
docker run --rm --pull=never --platform linux/amd64 \
  --volume "$GITHUB_WORKSPACE:/workdir" --volume "$HOME/.ssh:/root/.ssh:ro" \
  --volume "$HOME/.ssh:$HOME/.ssh:ro" --workdir /workdir \
  --env GPOINT_APP_SOURCE_SHA --env GPOINT_COMMIT_SHA --env GPOINT_PRODUCTION_HOST \
  --env GPOINT_SSH_KEY --env GPOINT_KNOWN_HOSTS_FILE --env KAMAL_REGISTRY_PASSWORD \
  --env GPOINT_KAMAL_EXPECTED_DIGEST --env GPOINT_KAMAL_SERVICE --env KAMAL_ROLES \
  ghcr.io/basecamp/kamal:v2.12.0@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625 \
  redeploy --skip-push --version "$live_core_version" --roles bot --config-file config/deploy.yml

ssh -T "root@$GPOINT_PRODUCTION_HOST" bash -s -- gpoint-core-bot "$live_core_version" "$live_core_sha" \
  < "$GITHUB_WORKSPACE/scripts/verify-kamal-role.sh"
ssh -T "root@$GPOINT_PRODUCTION_HOST" bash -s -- "$live_core_version" <<'REMOTE'
set -euo pipefail
version="$1"
for i in {1..15}; do
  if docker exec "gpoint-core-bot-$version" node -e '
fetch("http://127.0.0.1:9091/health/ready",{signal:AbortSignal.timeout(2500)})
.then(async r=>{const v=await r.json();if(!r.ok||v.status!=="ready")process.exitCode=1})
.catch(()=>process.exitCode=1)' >/dev/null 2>&1; then
    echo "GPOINT_BOT_READY=PASS"
    exit 0
  fi
  sleep 2
done
echo "GPOINT_BOT_READY=BLOCKED polling not ready" >&2
exit 1
REMOTE
echo "GPOINT_BOT_BOOTSTRAP=PASS version=$live_core_version source_sha=$live_core_sha" >> "$GITHUB_STEP_SUMMARY"
