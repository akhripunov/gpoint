#!/bin/sh
set -eu

: "${REMOTE_CMD:?REMOTE_CMD is required}"
: "${HOME:?HOME is required}"

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

perl - "$deadline" ssh \
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
  "$REMOTE_CMD" <<'PERL'
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
