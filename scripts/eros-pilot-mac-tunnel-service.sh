#!/usr/bin/env bash
set -euo pipefail

label='com.gpoint.eros-pilot-ollama-tunnel'
plist_path="/Library/LaunchDaemons/$label.plist"
user_home="${HOME:?HOME is required}"
service_dir="$user_home/Library/Application Support/GPoint/Eros Pilot"
stable_script="$service_dir/open-mac-ollama-tunnel.sh"
stable_verifier="$service_dir/verify-mac-ollama-tags.mjs"
stable_manager="$service_dir/eros-pilot-mac-tunnel-service.sh"
stable_known_hosts="$service_dir/known_hosts"
generator="$service_dir/eros_pilot_mac_tunnel_service.py"
source_root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
action="${1:-}"

fail() { echo "EROS_PILOT_TUNNEL_SERVICE_BLOCKED=$1" >&2; exit 2; }
[[ "$(uname -s)" == Darwin ]] || fail macOS_required

resolve_identity() {
  if [[ -n "${GPOINT_SSH_IDENTITY_FILE:-}" ]]; then printf '%s' "$GPOINT_SSH_IDENTITY_FILE"; return; fi
  local config="${GPOINT_RELEASE_CONFIG:-$user_home/.config/gpoint/release-tools.env}"
  [[ -r "$config" ]] || fail durable_release_config_missing
  local value
  value="$(sed -n 's/^GPOINT_SSH_IDENTITY_FILE=//p' "$config" | tail -n 1)"
  value="${value#\"}"; value="${value%\"}"; value="${value#\'}"; value="${value%\'}"
  case "$value" in
    '$HOME') value="$user_home" ;;
    '$HOME/'*) value="$user_home/${value#\$HOME/}" ;;
    '~') value="$user_home" ;;
    '~/'*) value="$user_home/${value#\~/}" ;;
  esac
  [[ -n "$value" ]] || fail durable_ssh_identity_missing
  printf '%s' "$value"
}

read_plist_value() {
  /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:$1" "$plist_path" 2>/dev/null
}

check_plist_owner() {
  local owner_mode
  owner_mode="$(stat -f '%u:%Lp' "$plist_path" 2>/dev/null || true)"
  [[ "$owner_mode" == '0:644' ]] || fail launchdaemon_must_be_root_owned_mode_0644
}

check_plist_runtime_settings() {
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :RunAtLoad' "$plist_path" 2>/dev/null || true)" == true ]] || fail launchdaemon_run_at_load_disabled
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :KeepAlive' "$plist_path" 2>/dev/null || true)" == true ]] || fail launchdaemon_keepalive_disabled
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$plist_path" 2>/dev/null || true)" == /bin/bash ]] || fail launchdaemon_program_mismatch
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:1' "$plist_path" 2>/dev/null || true)" == "$stable_script" ]] || fail launchdaemon_script_mismatch
}

case "$action" in
  install)
    target="${EROS_PILOT_VPS_SSH_TARGET:-}"
    [[ -n "$target" ]] || fail exact_vps_ssh_target_required
    identity="$(resolve_identity)"
    known_hosts_source="${GPOINT_KNOWN_HOSTS_FILE:-$user_home/.ssh/known_hosts}"
    [[ -r "$identity" && ! -L "$identity" ]] || fail durable_ssh_identity_unreadable_or_symlinked
    [[ -r "$known_hosts_source" ]] || fail verified_known_hosts_missing
    node_bin="$(command -v node || true)"
    [[ -n "$node_bin" && -x "$node_bin" ]] || fail node_runtime_missing
    sudo -n true || fail noninteractive_sudo_required_for_launchdaemon
    : "${CANDIDATE_SHA:?exact candidate SHA is required for tunnel installation}"
    mkdir -p "$service_dir"
    chmod 700 "$service_dir"
    cp "$source_root/services/eros-engine/open-mac-ollama-tunnel.sh" "$stable_script"
    cp "$source_root/services/eros-engine/verify-mac-ollama-tags.mjs" "$stable_verifier"
    cp "$source_root/services/eros-engine/eros_pilot_mac_tunnel_service.py" "$generator"
    cp "$source_root/scripts/eros-pilot-mac-tunnel-service.sh" "$stable_manager"
    chmod 700 "$stable_script"
    chmod 700 "$stable_manager"
    chmod 600 "$stable_verifier" "$generator"
    install -m 600 "$known_hosts_source" "$stable_known_hosts"
    host="${target#*@}"
    ssh-keygen -F "$host" -f "$stable_known_hosts" >/dev/null || fail host_key_not_in_verified_known_hosts
    key_mode="$(stat -f '%Lp' "$identity")"
    [[ "$key_mode" == 600 || "$key_mode" == 400 ]] || fail durable_ssh_identity_permissions_must_be_0600_or_0400
    if [[ -e "$plist_path" ]]; then
      check_plist_owner
      [[ "$(/usr/libexec/PlistBuddy -c 'Print :Label' "$plist_path" 2>/dev/null || true)" == "$label" ]] || fail launchdaemon_path_is_owned_by_another_service
    fi
    sudo -n launchctl bootout "system/$label" >/dev/null 2>&1 || true
    tmp_plist="$(mktemp "${TMPDIR:-/tmp}/$label.XXXXXX")"
    trap 'rm -f -- "$tmp_plist"' EXIT
    python3 "$source_root/services/eros-engine/eros_pilot_mac_tunnel_service.py" \
      --username "$(id -un)" --home "$user_home" --target "$target" \
      --identity "$identity" --known-hosts "$stable_known_hosts" \
      --script "$stable_script" --node "$node_bin" --source-sha "$CANDIDATE_SHA" > "$tmp_plist"
    plutil -lint "$tmp_plist" >/dev/null || fail generated_launchdaemon_plist_invalid
    sudo -n install -o root -g wheel -m 644 "$tmp_plist" "$plist_path"
    sudo -n launchctl enable "system/$label"
    sudo -n launchctl bootstrap system "$plist_path"
    "$0" verify
    ;;
  verify)
    [[ -r "$plist_path" ]] || fail launchdaemon_not_installed
    check_plist_owner
    [[ "$(/usr/libexec/PlistBuddy -c 'Print :Label' "$plist_path" 2>/dev/null || true)" == "$label" ]] || fail launchdaemon_label_mismatch
    check_plist_runtime_settings
    [[ -r "$generator" ]] || fail launchdaemon_verifier_missing
    launchd_details="$(sudo -n launchctl print "system/$label" 2>/dev/null)" || fail launchdaemon_not_loaded
    launchd_pid="$(printf '%s\n' "$launchd_details" | python3 "$generator" --launchd-pid)" || fail launchdaemon_not_running
    launchd_command="$(ps -ww -p "$launchd_pid" -o command= 2>/dev/null || true)"
    [[ "$launchd_command" == *"$stable_script"* ]] || fail launchdaemon_process_identity_mismatch
    identity="$(read_plist_value GPOINT_SSH_IDENTITY_FILE)"
    known_hosts="$(read_plist_value GPOINT_KNOWN_HOSTS_FILE)"
    target="$(read_plist_value EROS_PILOT_VPS_SSH_TARGET)"
    node_bin="$(read_plist_value EROS_PILOT_NODE_BIN)"
    installed_sha="$(read_plist_value EROS_PILOT_SOURCE_SHA)"
    if [[ -n "${EROS_PILOT_EXPECTED_SOURCE_SHA:-}" ]]; then
      [[ "$installed_sha" == "$EROS_PILOT_EXPECTED_SOURCE_SHA" ]] || fail launchdaemon_source_sha_mismatch
    fi
    [[ -r "$identity" && -r "$known_hosts" && -x "$node_bin" ]] || fail launchdaemon_inputs_missing
    EROS_PILOT_OLLAMA_URL=http://127.0.0.1:11434 "$node_bin" "$stable_verifier" local
    ssh -F /dev/null -T -i "$identity" -o BatchMode=yes -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$known_hosts" \
      -o ConnectTimeout=5 "$target" \
      'curl --fail --silent --show-error --max-time 5 http://127.0.0.1:11435/api/tags' \
      | "$node_bin" "$stable_verifier" --stdin vps-loopback
    echo 'EROS_PILOT_MAC_TUNNEL=PASS launchd=system qwen3=exact_digest public_ollama=disabled'
    ;;
  uninstall)
    if [[ -e "$plist_path" ]]; then
      check_plist_owner
      [[ "$(/usr/libexec/PlistBuddy -c 'Print :Label' "$plist_path" 2>/dev/null || true)" == "$label" ]] || fail launchdaemon_label_mismatch
      sudo -n launchctl bootout "system/$label" >/dev/null 2>&1 || true
      sudo -n launchctl disable "system/$label" >/dev/null 2>&1 || true
      sudo -n rm -f -- "$plist_path"
    fi
    rm -f -- "$stable_script" "$stable_verifier" "$stable_manager" "$stable_known_hosts" "$generator"
    echo 'EROS_PILOT_MAC_TUNNEL=REMOVED'
    ;;
  *) fail 'expected install, verify, or uninstall' ;;
esac
