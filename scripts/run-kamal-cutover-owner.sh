#!/usr/bin/env bash
# GitHub is an observer after submission, never a second cutover mutator.
set -euo pipefail
umask 077
test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"
test "$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/main" --jq .object.sha)" = "$CANDIDATE_SHA"
[[ "$CANDIDATE_SHA" =~ ^[0-9a-f]{40}$ && "$GITHUB_RUN_ID" =~ ^[0-9]+$ && "$GITHUB_RUN_ATTEMPT" =~ ^[1-9][0-9]*$ ]]
stage="/run/gpoint-kamal-owner/${CANDIDATE_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
unit="gpoint-kamal-owner-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
archive="$RUNNER_TEMP/gpoint-kamal-owner-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.tar"
trap 'rm -f -- "$archive"' EXIT
manifest_sha="$(python3 scripts/package-kamal-cutover.py --output "$archive")"
archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
owner_sha="$(sha256sum scripts/kamal_cutover_owner.py | awk '{print $1}')"
credentials_sha="$(sha256sum scripts/kamal_cutover_credentials.py | awk '{print $1}')"
bootstrap_sha="$(sha256sum scripts/start_kamal_cutover_owner.py | awk '{print $1}')"
for digest in "$manifest_sha" "$archive_sha" "$owner_sha" "$credentials_sha" "$bootstrap_sha"; do
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]]
done
ssh_opts=(-i "$HOME/.ssh/id_ed25519" -o BatchMode=yes -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$HOME/.ssh/known_hosts"
  -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
ssh -T "${ssh_opts[@]}" "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$stage" <<'RESERVE'
set -euo pipefail
[[ "$1" =~ ^/run/gpoint-kamal-owner/[0-9a-f]{40}-[0-9]+-[1-9][0-9]*$ ]]
test "$(id -u)" = 0
parent=/run/gpoint-kamal-owner
if [[ ! -e "$parent" ]]; then mkdir -m 0700 "$parent"; fi
test ! -L "$parent" && test "$(stat -c '%u:%a' "$parent")" = 0:700
test ! -e "$1" && test ! -L "$1"
mkdir -m 0700 "$1"
RESERVE
scp "${ssh_opts[@]}" "$archive" "root@${GPOINT_PRODUCTION_HOST}:$stage/bundle.tar"
scp "${ssh_opts[@]}" scripts/start_kamal_cutover_owner.py "root@${GPOINT_PRODUCTION_HOST}:$stage/start.py"
# Never resubmit after this command, including a transport error: systemd
# may already own the transaction. Only read-only observation is permitted.
if ! ssh -T "${ssh_opts[@]}" "root@${GPOINT_PRODUCTION_HOST}" bash -s -- \
    "$stage" "$bootstrap_sha" "$archive_sha" "$manifest_sha" "$owner_sha" "$credentials_sha" <<'SUBMIT'
set -euo pipefail
stage="$1"; expected="$2"
[[ "$stage" =~ ^/run/gpoint-kamal-owner/[0-9a-f]{40}-[0-9]+-[1-9][0-9]*$ ]]
test "$(stat -c '%u:%a' "$stage")" = 0:700
test ! -L "$stage/start.py" && test ! -L "$stage/bundle.tar"
chmod 0600 "$stage/start.py" "$stage/bundle.tar"
test "$(sha256sum "$stage/start.py" | awk '{print $1}')" = "$expected"
python3 -B "$stage/start.py" --stage "$stage" --archive-sha "$3" \
  --manifest-sha "$4" --owner-sha "$5" --credentials-sha "$6"
SUBMIT
then
  echo 'GPOINT_KAMAL_SUBMIT=UNKNOWN_OBSERVING_ONLY'
fi
deadline=$((SECONDS + 3300))
while (( SECONDS < deadline )); do
  if facts="$(ssh -T "${ssh_opts[@]}" "root@${GPOINT_PRODUCTION_HOST}" python3 - "$stage" "$unit" <<'OBSERVE'
import json, os, re, stat, subprocess, sys
from pathlib import Path
stage, unit = Path(sys.argv[1]), sys.argv[2]
if not re.fullmatch(r'/run/gpoint-kamal-owner/[0-9a-f]{40}-[0-9]+-[1-9][0-9]*', str(stage)):
    raise SystemExit(1)
if not re.fullmatch(r'gpoint-kamal-owner-[0-9]+-[1-9][0-9]*', unit):
    raise SystemExit(1)
path = stage/'owner-result.json'
if not path.exists():
    state = subprocess.run(['systemctl','is-active','--quiet',unit]).returncode
    print(json.dumps({'state': 'PENDING' if state == 0 else 'UNKNOWN'}))
else:
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit(1)
    result=json.loads(path.read_text())
    sha,run,attempt=stage.name.split('-')
    if (result.get('candidateSha'),result.get('runId'),result.get('attempt')) != (sha,run,attempt):
        raise SystemExit(1)
    active=subprocess.run(['systemctl','is-active','--quiet',unit]).returncode == 0
    secrets=(stage/'environment.json',stage/'home/.ssh/id_ed25519',stage/'home/.docker/config.json',stage/'bundle.tar')
    marker=('gpoint-cutover-'+stage.name).encode()
    key_gone=marker not in Path('/root/.ssh/authorized_keys').read_bytes()
    if active or any(p.exists() or p.is_symlink() for p in secrets) or not key_gone:
        result={'state':'PENDING'}
    else:
        out=stage/'github-output'
        values=re.findall(r'^manifest_sha=([0-9a-f]{64})$',out.read_text() if out.exists() else '', re.M)
        if result.get('state') == 'COMPLETE':
            if len(values) != 1: raise SystemExit(1)
            result['manifestSha256']=values[0]
    print(json.dumps(result,sort_keys=True))
OBSERVE
  )"; then
    state="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])' <<< "$facts")"
    case "$state" in
      COMPLETE)
        manifest="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["manifestSha256"])' <<< "$facts")"
        [[ "$manifest" =~ ^[0-9a-f]{64}$ ]]
        echo "manifest_sha=$manifest" >> "$GITHUB_OUTPUT"
        echo "GPOINT_KAMAL_OWNER=COMPLETE UNIT=$unit MANIFEST_SHA256=$manifest" >> "$GITHUB_STEP_SUMMARY"
        exit 0 ;;
      RECOVERED|BLOCKED|UNKNOWN)
        echo "GPOINT_KAMAL_OWNER=$state UNIT=$unit; no resubmit or competing recovery" >&2
        exit 1 ;;
      PENDING) ;;
      *) echo 'GPOINT_KAMAL_OWNER=INVALID_RESULT' >&2; exit 1 ;;
    esac
  fi
  sleep 10
done
echo "GPOINT_KAMAL_OWNER=OBSERVATION_TIMEOUT UNIT=$unit; server operation may continue, reconciliation required" >&2
exit 1
