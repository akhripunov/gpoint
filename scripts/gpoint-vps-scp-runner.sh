#!/bin/sh
set -eu

: "${LOCAL_FILE:?LOCAL_FILE is required}"
: "${REMOTE_FILE:?REMOTE_FILE is required}"
: "${HOME:?HOME is required}"
[ -f "$LOCAL_FILE" ] || { echo 'LOCAL_FILE must name an existing regular file' >&2; exit 66; }

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

deadline="${GPOINT_SCP_COMMAND_TIMEOUT_SECONDS:-60}"
case "$deadline" in ''|*[!0-9]*) echo 'GPoint SCP timeout must be an integer number of seconds' >&2; exit 78 ;; esac
[ "$deadline" -ge 1 ] && [ "$deadline" -le 600 ] || { echo 'GPoint SCP timeout is outside the allowed range' >&2; exit 78; }

perl - "$deadline" scp \
  -i "$identity" \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes \
  -o BatchMode=yes \
  -o ConnectTimeout=20 \
  -o ConnectionAttempts=1 \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -P 22 \
  -- "$LOCAL_FILE" "root@38.180.110.92:$REMOTE_FILE" <<'PERL'
use strict;
use warnings;
my $seconds = shift @ARGV;
my $pid = fork();
defined $pid or die "GPoint SCP runner could not start child\n";
if ($pid == 0) {
  exec { $ARGV[0] } @ARGV or do {
    print STDERR "GPoint SCP runner could not execute scp\n";
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
