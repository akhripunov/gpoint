#!/usr/bin/env bash
# One-time server-owned phase; never invoke outside kamal_cutover_owner.
set -euo pipefail
[[ "$BASELINE_PATH" == "/opt/gpoint/release-logs/${CANDIDATE_SHA}/kamal-cutover-baseline-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.txt" ]]
[[ "$BASELINE_SHA" =~ ^[0-9a-f]{64}$ ]]
for digest in "$APP_DIGEST" "$AGATA_DIGEST" "$MINIAPP_DIGEST" "$PROXY_DIGEST"; do
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]
done
helper=scripts/recover-kamal-cutover.py
helper_sha="$(sha256sum "$helper" | awk '{print $1}')"
[[ "$helper_sha" =~ ^[0-9a-f]{64}$ ]]
remote_dir="/run/gpoint-kamal-recovery-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
remote_helper="$remote_dir/recover-kamal-cutover.py"
route_helper=scripts/kamal_proxy_routes.py
route_helper_sha="$(sha256sum "$route_helper" | awk '{print $1}')"
[[ "$route_helper_sha" =~ ^[0-9a-f]{64}$ ]]
remote_route_helper="$remote_dir/kamal_proxy_routes.py"
ssh_opts=(-i "$HOME/.ssh/id_ed25519" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$HOME/.ssh/known_hosts")
recovery_cleanup() {
  ssh -F "$HOME/.ssh/config" -T "${ssh_opts[@]}" "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$remote_dir" <<'REMOTE_CLEANUP' || true
set -euo pipefail
[[ "$1" =~ ^/run/gpoint-kamal-recovery-[0-9]+-[0-9]+$ ]]
if [[ -e "$1" ]]; then
  test "$(stat -c '%u:%a' "$1")" = '0:700'
  rm -rf -- "$1"
fi
REMOTE_CLEANUP
}
trap recovery_cleanup EXIT
ssh -F "$HOME/.ssh/config" -T "${ssh_opts[@]}" "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$remote_dir" <<'REMOTE_SETUP'
set -euo pipefail
[[ "$1" =~ ^/run/gpoint-kamal-recovery-[0-9]+-[0-9]+$ ]]
test ! -e "$1"
umask 077
mkdir -- "$1"
chmod 0700 "$1"
test "$(stat -c '%u:%a' "$1")" = '0:700'
REMOTE_SETUP
scp -F "$HOME/.ssh/config" "${ssh_opts[@]}" "$helper" "root@${GPOINT_PRODUCTION_HOST}:$remote_helper"
scp -F "$HOME/.ssh/config" "${ssh_opts[@]}" "$route_helper" "root@${GPOINT_PRODUCTION_HOST}:$remote_route_helper"
set +e
recovery_output="$(ssh -F "$HOME/.ssh/config" -T "${ssh_opts[@]}" "root@${GPOINT_PRODUCTION_HOST}" bash -s -- \
  "$remote_dir" "$helper_sha" "$route_helper_sha" \
  "$CANDIDATE_SHA" "$APP_SOURCE_SHA" "$IMAGE_VERSION" \
  "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$BASELINE_PATH" "$BASELINE_SHA" \
  "$APP_DIGEST" "$AGATA_DIGEST" "$MINIAPP_DIGEST" "$PROXY_DIGEST" <<'REMOTE_RUN'
set -euo pipefail
dir="$1"; expected_sha="$2"; expected_route_sha="$3"; shift 3
[[ "$dir" =~ ^/run/gpoint-kamal-recovery-[0-9]+-[0-9]+$ ]]
helper="$dir/recover-kamal-cutover.py"; route_helper="$dir/kamal_proxy_routes.py"
[[ "$expected_sha" =~ ^[0-9a-f]{64}$ && "$expected_route_sha" =~ ^[0-9a-f]{64}$ ]]
test "$(stat -c '%u:%a' "$dir")" = '0:700'
test "$(sha256sum "$helper" | awk '{print $1}')" = "$expected_sha"
test "$(sha256sum "$route_helper" | awk '{print $1}')" = "$expected_route_sha"
chmod 0600 "$helper" "$route_helper"
test "$(stat -c '%u:%a' "$helper")" = '0:600'
test "$(stat -c '%u:%a' "$route_helper")" = '0:600'
trap 'rm -rf -- "$dir"' EXIT
python3 -B "$helper" \
  --candidate-sha "$1" --app-source-sha "$2" --version "$3" \
  --run-id "$4" --attempt "$5" --baseline "$6" --baseline-sha "$7" \
  --app-digest "$8" --agata-digest "$9" --miniapp-digest "${10}" --proxy-digest "${11}"
REMOTE_RUN
)"
recovery_status=$?
set -e
printf '%s\n' "$recovery_output"
echo "$recovery_output" >> "$GITHUB_STEP_SUMMARY"
if (( recovery_status != 0 )); then
  exit "$recovery_status"
fi
grep -qx 'GPOINT_CUTOVER_RECOVERY=PASS' <<< "$recovery_output"
