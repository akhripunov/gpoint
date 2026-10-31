#!/usr/bin/env bash
# One-time server-owned phase; never invoke outside kamal_cutover_owner.
set -euo pipefail
curl --retry 12 --retry-connrefused --retry-delay 5 --fail --silent --show-error \
  https://gpoint.one/api/health/live | grep -Fq '"status":"ok"'
for path in / /creators; do
  curl --retry 6 --retry-connrefused --retry-delay 2 --fail --silent --show-error \
    "https://gpoint.one$path" | grep -Fq '<div id="root">'
done
media_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  https://gpoint.one/media/assets/__kamal_proxy_route_probe_not_found__)"
test "$media_status" = 404
echo 'KAMAL_PROXY_TLS_VERIFY=PASS'
