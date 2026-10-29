#!/usr/bin/env bash
set -euo pipefail

command -v docker >/dev/null || { echo 'docker is required' >&2; exit 2; }
docker info >/dev/null

suffix="${$}-${RANDOM}"
network="gpoint-kamal-proxy-test-${suffix}"
proxy="gpoint-kamal-proxy-test-${suffix}"
core="gpoint-kamal-core-test-${suffix}"
miniapp="gpoint-kamal-miniapp-test-${suffix}"
port="${GPOINT_KAMAL_PROXY_TEST_PORT:-18081}"
proxy_image='basecamp/kamal-proxy:v0.9.2'
backend_image='python:3.11-slim'

docker image inspect "$proxy_image" >/dev/null 2>&1 || docker pull "$proxy_image"
docker image inspect "$backend_image" >/dev/null 2>&1 || docker pull "$backend_image"

cleanup() {
  docker rm -f "$proxy" "$core" "$miniapp" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if docker ps --format '{{.Ports}}' | grep -Fq "127.0.0.1:${port}->"; then
  echo "test port 127.0.0.1:${port} is already in use" >&2
  exit 2
fi

docker network create "$network" >/dev/null
docker run -d --name "$core" --network "$network" --network-alias gpoint-core-api \
  "$backend_image" python -u -c 'from http.server import BaseHTTPRequestHandler,HTTPServer; H=type("H",(BaseHTTPRequestHandler,),{"do_GET":lambda s:(s.send_response(200),s.end_headers(),s.wfile.write(("core:"+s.path).encode()))}); HTTPServer(("0.0.0.0",80),H).serve_forever()' >/dev/null
docker run -d --name "$miniapp" --network "$network" --network-alias gpoint-miniapp-web \
  "$backend_image" python -u -c 'from http.server import BaseHTTPRequestHandler,HTTPServer; H=type("H",(BaseHTTPRequestHandler,),{"do_GET":lambda s:(s.send_response(200),s.end_headers(),s.wfile.write(("miniapp:"+s.path).encode()))}); HTTPServer(("0.0.0.0",80),H).serve_forever()' >/dev/null
docker run -d --name "$proxy" --network "$network" -p "127.0.0.1:${port}:80" \
  --entrypoint kamal-proxy "$proxy_image" run --http-port 80 --https-port 443 >/dev/null

ready=0
for _ in {1..30}; do
  if docker exec "$proxy" kamal-proxy list >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
test "$ready" -eq 1 || { docker logs "$proxy" >&2; exit 1; }
docker exec "$proxy" kamal-proxy deploy gpoint-miniapp \
  --host gpoint.example.test --target gpoint-miniapp-web:80 --health-check-path=/ >/dev/null
docker exec "$proxy" kamal-proxy deploy gpoint-core \
  --host gpoint.example.test --target gpoint-core-api:80 --path-prefix=/api \
  --strip-path-prefix --health-check-path=/ >/dev/null
docker exec "$proxy" kamal-proxy deploy gpoint-core-media \
  --host gpoint.example.test --target gpoint-core-api:80 --path-prefix=/media \
  --strip-path-prefix --health-check-path=/ >/dev/null

expect() {
  local path="$1" expected="$2" actual
  actual="$(curl --fail --silent --show-error --header 'Host: gpoint.example.test' "http://127.0.0.1:${port}${path}")"
  test "$actual" = "$expected" || {
    printf 'route %s returned %s, expected %s\n' "$path" "$actual" "$expected" >&2
    exit 1
  }
}

expect / 'miniapp:/'
expect /settings 'miniapp:/settings'
expect /api/health/live 'core:/health/live'
expect /media/assets/image.png 'core:/assets/image.png'
route_table="$(docker exec "$proxy" kamal-proxy list)"
python3 - "$route_table" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("kamal_proxy_routes", "scripts/kamal_proxy_routes.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
routes = module.parse_routes(sys.argv[1])
expected = {
    # This fixture uses HTTP only: it deliberately omits --tls on deploy.
    ("gpoint-miniapp", "gpoint.example.test", "/", "no"),
    ("gpoint-core", "gpoint.example.test", "/api", "no"),
    ("gpoint-core-media", "gpoint.example.test", "/media", "no"),
}
actual = {(row["Service"], row["Host"], row["Path"], row["TLS"]) for row in routes}
if actual != expected or len(routes) != len(expected):
    raise SystemExit("pinned Kamal Proxy route-list contract changed")
PY
docker exec "$proxy" kamal-proxy remove gpoint-core-media >/dev/null
expect /media/assets/image.png 'miniapp:/media/assets/image.png'
route_table="$(docker exec "$proxy" kamal-proxy list)"
python3 - "$route_table" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("kamal_proxy_routes", "scripts/kamal_proxy_routes.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
routes = module.parse_routes(sys.argv[1])
if {row["Service"] for row in routes} != {"gpoint-core", "gpoint-miniapp"}:
    raise SystemExit("kamal-proxy remove did not remove exactly the requested media route")
PY
docker exec "$proxy" kamal-proxy remove gpoint-core >/dev/null
docker exec "$proxy" kamal-proxy remove gpoint-miniapp >/dev/null
route_table="$(docker exec "$proxy" kamal-proxy list)"
python3 - "$route_table" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("kamal_proxy_routes", "scripts/kamal_proxy_routes.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if module.parse_routes(sys.argv[1]):
    raise SystemExit("proxy route removal did not leave an empty route table")
PY
echo 'KAMAL_PROXY_ROUTE_REMOVE=PASS'
echo 'KAMAL_PROXY_SHARED_ROUTING=PASS'
