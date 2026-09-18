FROM node:22-bookworm-slim AS build
ARG SENSITIVE_DETECTOR_COMMIT=f91d4bc978bea30aee7c1be1c2b6932c5818c7ec
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/misskey-dev/sensitive-detector.git . && git checkout "$SENSITIVE_DETECTOR_COMMIT"
RUN corepack enable
RUN pnpm install --frozen-lockfile
RUN pnpm run build
RUN pnpm deploy --legacy --filter=@misskey-sensitive-detector/server --prod /app/deploy

FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production
WORKDIR /app
COPY --from=build /app/deploy ./
COPY --from=build /src/nsfw-model/ /models/
COPY --from=build /src/scripts/healthcheck.mjs /scripts/healthcheck.mjs
USER node
EXPOSE 3009
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 CMD ["node", "/scripts/healthcheck.mjs"]
CMD ["node", "dist/main.mjs", "--config", "/config/config.mjs"]
