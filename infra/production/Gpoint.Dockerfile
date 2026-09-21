FROM node:22-bookworm-slim AS build
WORKDIR /workspace
ARG GPOINT_BUILD_SHA
ENV GPOINT_BUILD_SHA=${GPOINT_BUILD_SHA}
RUN apt-get update && apt-get install -y --no-install-recommends git openssl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/api/package.json apps/api/package.json
COPY apps/bot/package.json apps/bot/package.json
COPY apps/miniapp/package.json apps/miniapp/package.json
COPY apps/worker/package.json apps/worker/package.json
COPY packages/contracts/package.json packages/contracts/package.json
COPY packages/db/package.json packages/db/package.json
RUN corepack enable && corepack prepare pnpm@9.15.0 --activate
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm --filter @gpoint/db exec prisma generate
RUN pnpm --filter @gpoint/db build
RUN pnpm --filter @gpoint/api build
RUN pnpm --filter @gpoint/worker build
RUN pnpm --filter @gpoint/bot build
RUN pnpm --filter @gpoint/miniapp build

FROM node:22-bookworm-slim AS runtime
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /workspace /app
RUN corepack enable && corepack prepare pnpm@9.15.0 --activate
ENV NODE_ENV=production
