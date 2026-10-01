#!/bin/sh
set -eu

: "${REMOTE_CMD:?REMOTE_CMD is required}"
: "${HOME:?HOME is required}"

umask 077

config_file="${GPOINT_RELEASE_CONFIG:-$HOME/.config/gpoint/release-tools.env}"
[ -r "$config_file" ] || { echo 'GPoint release config is unreadable' >&2; exit 78; }

identity="${GPOINT_SSH_IDENTITY_FILE:-}"
if [ -z "$identity" ]; then
  identity=$(sed -n 's/^GPOINT_SSH_IDENTITY_FILE=//p' "$config_file" | tail -n 1)
  identity=${identity#\"}; identity=${identity%\"}
  identity=${identity#\'}; identity=${identity%\'}
  case "$identity" in
    '$HOME') identity=$HOME ;;
    '$HOME/'*) identity="$HOME/${identity#\$HOME/}" ;;
    '~') identity=$HOME ;;
    '~/'*) identity="$HOME/${identity#\~/}" ;;
  esac
fi

[ -n "$identity" ] && [ -r "$identity" ] || { echo 'GPoint SSH identity is missing or unreadable' >&2; exit 78; }

deadline="${GPOINT_REMOTE_COMMAND_TIMEOUT_SECONDS:-600}"
case "$deadline" in ''|*[!0-9]*) echo 'GPoint remote timeout must be an integer number of seconds' >&2; exit 78 ;; esac
[ "$deadline" -ge 1 ] && [ "$deadline" -le 1800 ] || { echo 'GPoint remote timeout is outside the allowed range' >&2; exit 78; }

debug_log=$(mktemp "${TMPDIR:-/tmp}/gpoint-ssh-debug.XXXXXX")
ssh_stderr=$(mktemp "${TMPDIR:-/tmp}/gpoint-ssh-stderr.XXXXXX")
trap 'rm -f "$debug_log" "$ssh_stderr"' EXIT HUP INT TERM

set +e
perl - "$deadline" ssh \
  -E "$debug_log" \
  -vv \
  -i "$identity" \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes \
  -o BatchMode=yes \
  -o ConnectTimeout=20 \
  -o ConnectionAttempts=1 \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -p 22 \
  root@38.180.110.92 \
  "$REMOTE_CMD" 2>"$ssh_stderr" <<'PERL'
use strict;
use warnings;
my $seconds = shift @ARGV;
my $pid = fork();
defined $pid or die "GPoint SSH runner could not start child\n";
if ($pid == 0) {
  exec { $ARGV[0] } @ARGV or do {
    print STDERR "GPoint SSH runner could not execute ssh\n";
    exit 127;
  };
}
$SIG{ALRM} = sub {
  kill 'TERM', $pid;
  select undef, undef, undef, 1;
  kill 'KILL', $pid;
  waitpid($pid, 0);
  exit 124;
};
alarm($seconds);
waitpid($pid, 0);
alarm(0);
my $status = $?;
exit(($status & 127) ? 128 + ($status & 127) : $status >> 8);
PERL
ssh_status=$?
set -e
cat "$ssh_stderr" >&2

if [ "$ssh_status" -eq 255 ] && [ -n "${GPOINT_REMOTE_ATTEMPT_STATUS_FILE:-}" ]; then
  command_sent=no
  if grep -Fq 'Sending command:' "$debug_log"; then command_sent=yes; fi
  auth_or_hostkey_failure=no
  if grep -Eiq 'Permission denied|publickey|Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED' "$ssh_stderr" "$debug_log"; then auth_or_hostkey_failure=yes; fi
  pre_exec_transport_failure=no
  if grep -Eiq 'Could not resolve hostname|Could not resolve host|Name or service not known|nodename nor servname|temporary failure in name resolution|Network is unreachable|No route to host|Connection refused|Connection timed out|banner exchange|kex_exchange_identification.*(closed|reset|timeout)|Connection reset by peer|Connection closed by' "$ssh_stderr" "$debug_log"; then pre_exec_transport_failure=yes; fi
  if [ "$command_sent" = no ] && [ "$auth_or_hostkey_failure" = no ] && [ "$pre_exec_transport_failure" = yes ]; then
    printf '%s\n' 'PRE_EXEC_TRANSPORT_FAILURE:255' > "$GPOINT_REMOTE_ATTEMPT_STATUS_FILE"
    exit 75
  fi
fi

exit "$ssh_status"
