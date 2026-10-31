#!/usr/bin/env bash
# One-time server-owned phase; never invoke outside kamal_cutover_owner.
set -euo pipefail
kamal() {
  docker run --rm --network host --label gpoint.cutover.owner="$IMAGE_VERSION" \
    --volume "$GITHUB_WORKSPACE:/workdir" \
    --volume /var/run/docker.sock:/var/run/docker.sock \
    --volume "$HOME/.ssh:/root/.ssh:ro" \
    --volume "$HOME/.ssh:$HOME/.ssh:ro" \
    --volume "$HOME/.docker:/root/.docker:ro" \
    --workdir /workdir \
    --env GPOINT_COMMIT_SHA --env GPOINT_APP_SOURCE_SHA --env GPOINT_PRODUCTION_HOST --env GPOINT_SSH_KEY=/root/.ssh/id_ed25519 \
    --env GPOINT_KNOWN_HOSTS_FILE=/root/.ssh/known_hosts --env KAMAL_REGISTRY_PASSWORD \
    --env GPOINT_KAMAL_SERVICE --env GPOINT_KAMAL_EXPECTED_DIGEST --env KAMAL_ROLES \
    ghcr.io/basecamp/kamal:v2.12.0@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625 \
    "$@"
}
kamal config --config-file config/deploy.yml >/dev/null
kamal config --config-file config/deploy.agata.yml >/dev/null
kamal config --config-file config/deploy.miniapp.yml >/dev/null
[[ "$PROXY_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
ssh_opts=(-i "$GPOINT_SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$GPOINT_KNOWN_HOSTS_FILE")
ssh -F "$HOME/.ssh/config" -T "${ssh_opts[@]}" "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$PROXY_DIGEST" <<'PIN_PROXY'
set -euo pipefail
digest="$1"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]
docker pull "basecamp/kamal-proxy@$digest"
image_id="$(docker image inspect "basecamp/kamal-proxy@$digest" --format '{{.Id}}')"
docker image inspect "$image_id" --format '{{json .RepoDigests}}' | grep -Fq "basecamp/kamal-proxy@$digest"
docker image tag "$image_id" basecamp/kamal-proxy:v0.9.2
test "$(docker image inspect basecamp/kamal-proxy:v0.9.2 --format '{{.Id}}')" = "$image_id"
PIN_PROXY
GPOINT_KAMAL_EXPECTED_DIGEST="$APP_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-core KAMAL_ROLES=api \
  kamal deploy --skip-push --version "$IMAGE_VERSION" --roles api --config-file config/deploy.yml
ssh -F "$HOME/.ssh/config" -T "${ssh_opts[@]}" "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$PROXY_DIGEST" <<'VERIFY_PROXY'
set -euo pipefail
digest="$1"
container_image="$(docker inspect kamal-proxy --format '{{.Image}}')"
test "$(docker inspect kamal-proxy --format '{{.Config.Image}}')" = basecamp/kamal-proxy:v0.9.2
docker image inspect "$container_image" --format '{{json .RepoDigests}}' | grep -Fq "basecamp/kamal-proxy@$digest"
VERIFY_PROXY
GPOINT_KAMAL_EXPECTED_DIGEST="$AGATA_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-agata KAMAL_ROLES=api \
  kamal deploy --skip-push --version "$IMAGE_VERSION" --roles api --config-file config/deploy.agata.yml
GPOINT_KAMAL_EXPECTED_DIGEST="$MINIAPP_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-miniapp KAMAL_ROLES=web \
  kamal deploy --skip-push --version "$IMAGE_VERSION" --roles web --config-file config/deploy.miniapp.yml
