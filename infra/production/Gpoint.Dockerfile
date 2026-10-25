# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim AS build
WORKDIR /workspace
ENV PNPM_HOME=/pnpm
ENV PATH=${PNPM_HOME}:${PATH}
RUN apt-get update && apt-get install -y --no-install-recommends git openssl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY pnpm-lock.yaml pnpm-workspace.yaml ./
COPY --from=dependency-manifests /package.json ./package.json
COPY --from=dependency-manifests /apps/api/package.json apps/api/package.json
COPY --from=dependency-manifests /apps/bot/package.json apps/bot/package.json
COPY --from=dependency-manifests /apps/miniapp/package.json apps/miniapp/package.json
COPY --from=dependency-manifests /apps/worker/package.json apps/worker/package.json
COPY --from=dependency-manifests /packages/agata-client/package.json packages/agata-client/package.json
COPY --from=dependency-manifests /packages/contracts/package.json packages/contracts/package.json
COPY --from=dependency-manifests /packages/db/package.json packages/db/package.json
RUN corepack enable && corepack prepare pnpm@9.15.0 --activate && pnpm config set store-dir /pnpm/store
RUN --mount=type=cache,id=gpoint-pnpm-store,target=/pnpm/store pnpm install --frozen-lockfile
# Separate stable backend builds from Mini App sources. Previously COPY . .
# invalidated every backend compilation on UI-only changes.
COPY tsconfig.base.json ./tsconfig.base.json
COPY packages ./packages
RUN pnpm --filter @gpoint/db exec prisma generate
RUN pnpm --filter @gpoint/agata-client build
RUN pnpm --filter @gpoint/db build
COPY apps/api ./apps/api
RUN pnpm --filter @gpoint/api build
COPY apps/worker ./apps/worker
RUN pnpm --filter @gpoint/worker build
COPY apps/bot ./apps/bot
RUN pnpm --filter @gpoint/bot build

# Keep the exact-SHA Mini App build separate from stable backend/dependencies.
FROM build AS miniapp-build
COPY apps/miniapp ./apps/miniapp
ARG GPOINT_BUILD_SHA
ENV GPOINT_BUILD_SHA=${GPOINT_BUILD_SHA}
RUN pnpm --filter @gpoint/miniapp build

FROM node:22-bookworm-slim AS runtime
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN corepack enable && corepack prepare pnpm@9.15.0 --activate
ENV NODE_ENV=production

# Keep stable dependencies and compiled backend in a separately cacheable layer.
COPY --from=build /workspace /app
# Vite creates this empty workspace cache directory during the original
# build; retain its runtime inventory without coupling dependencies to UI.
RUN mkdir -p /app/apps/miniapp/node_modules/.vite-temp
# Preserve the original complete runtime source inventory. The Docker context
# explicitly excludes node_modules and dist, so compiled outputs stay intact.
COPY . /app
# Only the small Mini App distribution depends on the exact candidate SHA.
COPY --from=miniapp-build /workspace/apps/miniapp/dist /app/apps/miniapp/dist

FROM nginx:1.27-alpine AS miniapp-runtime
ARG GPOINT_BUILD_SHA
LABEL org.opencontainers.image.revision=${GPOINT_BUILD_SHA} org.opencontainers.image.source="https://github.com/akhripunov/gpoint"
COPY --from=miniapp-build /workspace/apps/miniapp/dist/ /usr/share/nginx/html/
