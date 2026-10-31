#!/usr/bin/env bash
# One-time server-owned phase; never invoke outside kamal_cutover_owner.
set -euo pipefail
remote_script="/run/gpoint-retire-caddy-${CANDIDATE_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.sh"
ssh_opts=(-i "$HOME/.ssh/id_ed25519" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$HOME/.ssh/known_hosts")
scp -F "$HOME/.ssh/config" "${ssh_opts[@]}" scripts/retire-gpoint-caddy.sh "root@${GPOINT_PRODUCTION_HOST}:$remote_script"
facts="$(ssh -F "$HOME/.ssh/config" -T "${ssh_opts[@]}" "root@${GPOINT_PRODUCTION_HOST}" bash "$remote_script" \
  "$CANDIDATE_SHA" "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$CADDYFILE_SHA256")"
grep -qx 'GPOINT_CADDY_RETIRED=PASS' <<< "$facts"
grep -qx "CADDYFILE_SHA256=$CADDYFILE_SHA256" <<< "$facts"
echo "$facts" >> "$GITHUB_STEP_SUMMARY"
