# GPoint Production Infrastructure Plan V1

Status: planning only. No production deployment, migration, DNS change, TLS issuance, or Telegram configuration change has been performed.
Date: 2026-09-15

## Proposed topology

One VPS runs a reverse proxy and the long-lived GPoint services. PostgreSQL and Redis bind only to the private/container network. Media is stored in an external S3-compatible service.

```text
Telegram WebView
      |
      v
HTTPS reverse proxy
  |             |
  v             v
Mini App      API
                |
       +--------+--------+
       v                 v
  PostgreSQL          Redis

Bot and Worker run as internal long-lived services.
API/Worker -> external S3-compatible media storage
```

Public endpoints, once a real domain is selected:

- Mini App: `https://app.<GPOINT_DOMAIN>`
- API: `https://api.<GPOINT_DOMAIN>`

Only the reverse proxy and the two HTTPS endpoints are public. PostgreSQL, Redis, Bot, Worker, and internal service ports remain private.

## Immutable release identity

- Candidate SHA: `5326a89ee1d489d28d040270b16cfd95aa94246e`
- Candidate tag: `gpoint-v1-core-baseline-2`
- Candidate remote verification: PASS
- API, Bot, Worker: immutable image digests built from the candidate SHA.
- Mini App: immutable static artifact checksum built from the candidate SHA.
- `latest` is not a release identity.

Before any migration, render effective configuration and prove image/artifact parity with the candidate. Unknown parity blocks migration and deploy.

## Service requirements

| Service | Runtime requirement | Exposure |
| --- | --- | --- |
| Reverse proxy | HTTPS termination, renewal, upstream routing | Public 443 |
| Mini App | Static artifact tied to candidate SHA | Via `app.<domain>` |
| API | Immutable candidate artifact; explicit Mini App CORS origin | Via `api.<domain>` |
| Bot | Long-lived process, production Telegram token | Private |
| Worker | Long-lived process, Redis connectivity | Private |
| PostgreSQL | Persistent volume, healthcheck, backups | Private only |
| Redis | Internal access, authentication/configuration | Private only |
| S3 storage | External bucket with signed/public read contract | Provider endpoint |

## Environment contract

Secrets are supplied by the deployment secret manager and never committed.

| Service | Required configuration | Owner/state |
| --- | --- | --- |
| Mini App | `VITE_API_URL=https://api.<domain>`, `VITE_DEV_AUTH_MODE=false` | Infrastructure / MISSING |
| API | `DATABASE_URL`, `REDIS_URL`, `TELEGRAM_BOT_TOKEN`, `DEV_AUTH_MODE=false`, `MINIAPP_ORIGIN=https://app.<domain>` | Infrastructure / MISSING |
| API media | `MEDIA_STORAGE_PROVIDER=s3`, `S3_ENDPOINT`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_PUBLIC_BASE_URL` or `S3_SIGNED_READ=true` | Infrastructure / MISSING |
| Bot | `TELEGRAM_BOT_TOKEN` | Telegram owner / MISSING |
| Worker | `REDIS_URL` | Infrastructure / MISSING |

## Staging option

Recommended staging shape is the same topology with separate application config and data stores:

- `staging-app.<GPOINT_DOMAIN>` and `staging-api.<GPOINT_DOMAIN>`.
- Separate PostgreSQL database.
- Separate Redis instance or namespace.
- Separate S3 bucket or prefix.
- Separate Telegram test bot/configuration.
- No production business data sharing.

Staging decision is still required from the product owner.

## Release gates

1. Read `CURRENT_PRODUCTION_SHA` and `CURRENT_PRODUCTION_DB_HEAD`; for first release both are `NONE_FIRST_RELEASE`.
2. Select and remotely verify an exact candidate SHA.
3. Run frozen install, Prisma checks, tests, typecheck, builds, and secret scan.
4. Build immutable service artifacts and static Mini App artifact.
5. Pass `PRE_APPLY_IMAGE_PARITY_GATE` against the exact candidate.
6. Apply migration head `20260915143000_post_moderation_reason` only after parity.
7. Deploy exact candidate and verify actual runtime SHA and DB head.
8. Create immutable production ref only after `RELEASE_COMPLETE`.

## Owner decisions required

```text
VPS_PROVIDER = UNSELECTED
VPS_PLAN = UNSELECTED
GPOINT_DOMAIN = UNSELECTED
S3_PROVIDER = UNSELECTED
STAGING = YES / NO decision required
```

Until these values and production credentials/URLs are supplied, `ENVIRONMENT_GATE=FAIL`, `PRE_APPLY_IMAGE_PARITY_GATE=NOT_READY`, and deployment remains blocked.
