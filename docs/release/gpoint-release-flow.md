# GPoint Durable Release Flow V1

Every release is fail-closed and starts from a known Git commit. `latest` is never a source of truth.

1. Read the actual production `CURRENT_PRODUCTION_SHA` and `CURRENT_PRODUCTION_DB_HEAD`; for the first release record `NONE_FIRST_RELEASE`.
2. Create a candidate from an exact committed `CANDIDATE_SHA`, push it, and verify that SHA remotely.
3. Run frozen install, Prisma validate/generate/status, typecheck, all builds, unit/integration/Core E2E tests, and secret scan.
4. Inspect effective deployment configuration and prove API, Bot, Worker, and Mini App artifacts correspond to `CANDIDATE_SHA` before migrations.
5. Validate migration compatibility and only then apply migrations.
6. Deploy the exact candidate, verify runtime health, actual running SHA, and DB head.
7. Publish an immutable remote release ref/tag resolving to the candidate SHA. Never force-move an existing baseline or release tag.

Unknown production state, remote availability, candidate tests, migration head, artifact parity, secrets/config, or rollback/recovery state blocks release. Production secrets remain outside Git. Staging is required when configured; otherwise report `STAGING=NOT_CONFIGURED` and do not claim staging proof.

Telegram live smoke is mandatory before public launch. Telegram affiliate live proof is a separate financial gate; until proven, affiliate status remains `UNVERIFIED_EXTERNAL_DEPENDENCY`.
