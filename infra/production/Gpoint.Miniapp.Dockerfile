# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim AS miniapp-build
WORKDIR /workspace
ENV PNPM_HOME=/pnpm
ENV PATH=${PNPM_HOME}:${PATH}
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The workflow context supplies this Dockerfile and nginx configuration. The
# named app-source Git context is independently pinned to APP_SOURCE_SHA.
COPY --from=app-source /pnpm-lock.yaml ./pnpm-lock.yaml
COPY --from=app-source /pnpm-workspace.yaml ./pnpm-workspace.yaml
COPY --from=dependency-manifests /package.json ./package.json
COPY --from=dependency-manifests /apps/api/package.json ./apps/api/package.json
COPY --from=dependency-manifests /apps/bot/package.json ./apps/bot/package.json
COPY --from=dependency-manifests /apps/miniapp/package.json ./apps/miniapp/package.json
COPY --from=dependency-manifests /apps/worker/package.json ./apps/worker/package.json
COPY --from=dependency-manifests /packages/agata-client/package.json ./packages/agata-client/package.json
COPY --from=dependency-manifests /packages/contracts/package.json ./packages/contracts/package.json
COPY --from=dependency-manifests /packages/db/package.json ./packages/db/package.json
RUN corepack enable && corepack prepare pnpm@9.15.0 --activate \
    && pnpm config set store-dir /pnpm/store
RUN --mount=type=cache,id=gpoint-miniapp-pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile

COPY --from=app-source /tsconfig.base.json ./tsconfig.base.json
COPY --from=app-source /apps/miniapp ./apps/miniapp
ARG GPOINT_BUILD_SHA
ENV GPOINT_BUILD_SHA=${GPOINT_BUILD_SHA}
RUN pnpm --filter @gpoint/miniapp build

FROM nginx:1.27-alpine AS miniapp-runtime
ARG GPOINT_BUILD_SHA
ARG GPOINT_WORKFLOW_SHA
LABEL org.opencontainers.image.revision=${GPOINT_BUILD_SHA} \
      org.opencontainers.image.source="https://github.com/akhripunov/gpoint" \
      org.gpoint.workflow.revision=${GPOINT_WORKFLOW_SHA}
COPY --from=miniapp-build /workspace/apps/miniapp/dist/ /usr/share/nginx/html/
COPY infra/production/nginx-miniapp.conf /etc/nginx/conf.d/default.conf
