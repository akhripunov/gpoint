#!/usr/bin/env bash
# One-time server-owned phase; never invoke outside kamal_cutover_owner.
set -euo pipefail
# Prepare recovery for failure during the first stop, before retiring Caddy.
bash scripts/retire-gpoint-caddy.sh "$CANDIDATE_SHA" "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$CADDYFILE_SHA256" --prepare-only
remote_script_dir="/run/gpoint-kamal/${CANDIDATE_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
remote_script="$remote_script_dir/pause-legacy-gpoint.sh"
remote_media_tree_helper="$remote_script_dir/media-tree.py"
remote_eros_collector="$remote_script_dir/eros-state-collector.py"
remote_eros_state="$remote_script_dir/kamal_eros_state.py"
ssh_opts=(-i "$HOME/.ssh/id_ed25519" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=6 -o TCPKeepAlive=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$HOME/.ssh/known_hosts")
ssh -F "$HOME/.ssh/config" -T "${ssh_opts[@]}" "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$remote_script_dir" <<'REMOTE'
set -euo pipefail
[[ "$1" =~ ^/run/gpoint-kamal/[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]
# Reuse the same protected directory after successful backup/restore.
# A second creation guard would always fail before pausing services.
test -d "$1"
test ! -L "$1"
test "$(stat -c '%u:%a' "$1")" = '0:700'
test ! -e "$1/pause-legacy-gpoint.sh"
REMOTE
scp -F "$HOME/.ssh/config" "${ssh_opts[@]}" scripts/pause-legacy-gpoint.sh "root@${GPOINT_PRODUCTION_HOST}:$remote_script"
scp -F "$HOME/.ssh/config" "${ssh_opts[@]}" scripts/gpoint-media-tree.py "root@${GPOINT_PRODUCTION_HOST}:$remote_media_tree_helper"
scp -F "$HOME/.ssh/config" "${ssh_opts[@]}" scripts/collect_kamal_eros_state.py "root@${GPOINT_PRODUCTION_HOST}:$remote_eros_collector"
scp -F "$HOME/.ssh/config" "${ssh_opts[@]}" scripts/kamal_eros_state.py "root@${GPOINT_PRODUCTION_HOST}:$remote_eros_state"
scp -F "$HOME/.ssh/config" "${ssh_opts[@]}" scripts/sanitize-kamal-inventory.py "root@${GPOINT_PRODUCTION_HOST}:$remote_script_dir/sanitize-kamal-inventory.py"
scp -F "$HOME/.ssh/config" "${ssh_opts[@]}" scripts/sanitize-kamal-resources.py "root@${GPOINT_PRODUCTION_HOST}:$remote_script_dir/sanitize-kamal-resources.py"
eros_collector_sha="$(sha256sum scripts/collect_kamal_eros_state.py | awk '{print $1}')"
eros_state_sha="$(sha256sum scripts/kamal_eros_state.py | awk '{print $1}')"
eros_inventory_sanitizer_sha="$(sha256sum scripts/sanitize-kamal-inventory.py | awk '{print $1}')"
eros_resource_sanitizer_sha="$(sha256sum scripts/sanitize-kamal-resources.py | awk '{print $1}')"
ssh -F "$HOME/.ssh/config" -T "${ssh_opts[@]}" "root@${GPOINT_PRODUCTION_HOST}" bash "$remote_script" \
  "$CANDIDATE_SHA" "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$BASELINE_PATH" "$BASELINE_SHA" \
  "$MEDIA_SOURCE_MANIFEST_SHA" "$remote_media_tree_helper" "$remote_eros_collector" \
  "$remote_eros_state" "$eros_collector_sha" "$eros_state_sha" \
  "$eros_inventory_sanitizer_sha" "$eros_resource_sanitizer_sha"
