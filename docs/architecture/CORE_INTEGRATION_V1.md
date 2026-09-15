# Core Integration V1 status

## Baseline

`GPOINT_CORE_INTEGRATION_V1 = PARTIAL` — the first Explore vertical slice is now verified; remaining core endpoints are intentionally deferred.

The repository now has shared public DTO contracts, deterministic ranking/cursor helpers, server-side auth helpers, approved Telegram redirect validation, bounded fixed-window rate limiting, local/S3 media storage interfaces, a Prisma migration baseline, fictional seed plan, and Docker development services.

## Not yet honestly complete

- Nest controllers are not yet wired to Prisma repositories.
- Mini App screens still use local fictional fixtures as their runtime source; replacing these with API queries is the next implementation increment.
- The committed SQL is a baseline subset and must be regenerated/validated with Prisma after registry access is restored.
- Seed plan is present but executable Prisma seed writes are not verified.
- Media upload intent/finalize endpoints are not wired.
- No browser review, build or test suite execution was possible.

## Intended API boundary

The next repository increment should wire `POST /auth/telegram`, `GET /me`, Explore/creator/reels/VIP endpoints, follow mutations, creator onboarding, media intent/finalize, and admin moderation to these helpers and DTOs. Public responses must never expose Prisma models, secret fields, raw storage keys, or moderation internals.

## Local boot

```sh
pnpm install
docker compose -f infra/docker/docker-compose.yml up -d
pnpm db:migrate
pnpm db:seed
pnpm dev
```

The environment recovery is complete; the first Explore slice has been installed, migrated, seeded, built, tested, and browser-checked. Remaining API surfaces are not yet wired.
