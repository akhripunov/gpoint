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
GPOINT_KAMAL_EXPECTED_DIGEST="$APP_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-core KAMAL_ROLES=worker,bot \
  kamal deploy --skip-push --version "$IMAGE_VERSION" --roles worker,bot --config-file config/deploy.yml
GPOINT_KAMAL_EXPECTED_DIGEST="$AGATA_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-agata KAMAL_ROLES=memory-worker \
  kamal deploy --skip-push --version "$IMAGE_VERSION" --roles memory-worker --config-file config/deploy.agata.yml
