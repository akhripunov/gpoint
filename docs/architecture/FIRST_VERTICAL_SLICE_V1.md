# First real vertical slice V1

## Release report — 2026-09-15

`GPOINT_FIRST_REAL_VERTICAL_SLICE = PARTIAL`

### Network diagnosis

- Node: `v22.23.1`
- npm: `10.9.8`
- pnpm: `11.19.0`
- npm/pnpm registry: `https://registry.npmjs.org/`
- proxy variables: not set
- DNS/HTTPS: `Could not resolve host: registry.npmjs.org`
- classification: `DNS_FAILURE / ENVIRONMENT_BLOCKED`

### Implementation prepared

- `GET /explore?tab=new|popular|trending` is a Nest controller backed by Prisma PostgreSQL query.
- Only approved creator profiles and published posts participate in the query.
- New uses latest published date; popular uses durable follow/event aggregates; trending uses 24-hour event windows with explainable weights.
- Shared Explore DTOs are returned; the API does not expose Prisma models.
- Explore now uses TanStack Query, with loading/error/empty states and no fixture fallback in its runtime path.
- A deterministic Prisma seed command defines 12 fictional approved creators and VIP offers.
- A Docker Compose file defines only PostgreSQL and Redis.

### Verified result

- `pnpm install --frozen-lockfile`: PASS
- Prisma validate/generate/migration: PASS
- PostgreSQL: accepting connections
- Redis: `PONG`
- Seed counts: 12 creators, 12 profiles, 12 VIP offers
- Nest `/health/live`: PASS
- Nest `/explore?tab=new|popular|trending`: PASS
- builds: miniapp/api/bot/worker/packages PASS
- tests: 6 assertions PASS; all workspace test scripts complete
- browser: 12 API-backed cards visible; `scrollWidth === clientWidth`; New tab verified
- DB-to-API: temporary DB display-name change observed in API
- API-to-frontend: same change observed after Mini App reload, then restored
- Explore fixture independence: PASS
- Mobile viewport matrix 320/360/375/390/430: NOT VERIFIED; browser capability exposed only the default 860px viewport

### Required next command sequence after environment recovery

```sh
pnpm install
docker compose -f infra/docker/docker-compose.yml up -d
pnpm db:migrate
pnpm db:seed
pnpm --filter @gpoint/api start
pnpm --filter @gpoint/miniapp dev
```
