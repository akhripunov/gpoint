# GPoint First Production Deploy Readiness V1

Status: blocked by missing production infrastructure configuration. No production migration or deployment was attempted.
Date: 2026-09-15

## Release identity

- Verified candidate: `5326a89ee1d489d28d040270b16cfd95aa94246e`
- Immutable candidate tag: `gpoint-v1-core-baseline-2`
- Remote candidate verification: PASS
- Current production SHA: `NONE_FIRST_RELEASE`
- Current production DB head: `NONE_FIRST_RELEASE`
- Rollback SHA: `NONE_FIRST_RELEASE`
- Target DB head: `20260915143000_post_moderation_reason`

The only commit between the verified candidate and current `main` (`046be97`) changes the baseline document only. No application, runtime, test, migration, or deployment files changed; the candidate remains the tested SHA.

## Infrastructure matrix

| Component | State | Required owner/configuration |
| --- | --- | --- |
| Mini App hosting | NOT_CONFIGURED | Product/Infrastructure |
| API hosting | NOT_CONFIGURED | Infrastructure |
| Bot runtime | NOT_CONFIGURED | Infrastructure |
| Worker runtime | NOT_CONFIGURED | Infrastructure |
| PostgreSQL | NOT_CONFIGURED | Infrastructure |
| Redis | NOT_CONFIGURED | Infrastructure |
| S3-compatible storage | NOT_CONFIGURED | Infrastructure |
| Domain/DNS | NOT_CONFIGURED | Infrastructure |
| TLS/HTTPS | NOT_CONFIGURED | Infrastructure |
| Staging | NOT_CONFIGURED | Product/Infrastructure decision |
| Telegram Bot | MISSING | Product/Telegram owner |
| Telegram Mini App URL | MISSING | Product/Telegram owner |

The repository contains only local development PostgreSQL, Redis, and MinIO compose services. They are not production targets.

## Production environment contract

Values are intentionally not stored here. Secrets must remain in the deployment secret manager.

| Service | Required variables | State |
| --- | --- | --- |
| Mini App | `VITE_API_URL`, `VITE_DEV_AUTH_MODE=false` | MISSING |
| API | `DATABASE_URL`, `REDIS_URL`, `TELEGRAM_BOT_TOKEN`, `DEV_AUTH_MODE=false`, `MINIAPP_ORIGIN` | MISSING |
| API media | `MEDIA_STORAGE_PROVIDER=s3`, `S3_ENDPOINT`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, public or signed-read configuration | MISSING |
| Bot | `TELEGRAM_BOT_TOKEN` | MISSING |
| Worker | `REDIS_URL` | MISSING |

## Mandatory gates before deployment

- `CANDIDATE_REMOTE_VERIFIED`: PASS.
- Candidate tests/builds: PASS on clean checkout.
- `ENVIRONMENT_GATE`: FAIL until all infrastructure targets and public URLs are known.
- `PRE_APPLY_IMAGE_PARITY_GATE`: NOT_READY; no production artifacts or image identities exist.
- Migration applied: NO.
- Production deploy: NOT_ATTEMPTED.
- Telegram live smoke: `DEFERRED_PREPRODUCTION`.
- Telegram affiliate proof: `DEFERRED_PREPRODUCTION`; affiliate remains `UNVERIFIED_EXTERNAL_DEPENDENCY`.

Unknown production state, hosting, secrets/configuration, public HTTPS URLs, artifact parity, and rollback state are release blockers.
