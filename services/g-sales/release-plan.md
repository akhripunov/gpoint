# G Sales prepared release plan — OWNER exact SHA required

This task applies no production migration/deploy and enables no real sending.
Baseline from canonical release ledger: production and AGATA SHA
`68bf0212d7aa5125394b0b39ffba9226eb106c0d`, Prisma head
`20260922090000_telegram_channel_avatar`, AGATA head `agata0010`, status COMPLETE.
Re-read actual ledger/runtime/DB immediately before any later mutation.

## Artifacts and separate processes

Build G Sales from a clean isolated checkout of the exact reviewed GitHub SHA:

```sh
docker build --build-arg GPOINT_BUILD_SHA=<40-char-sha> \
  -t gpoint-g-sales:<40-char-sha> services/g-sales
```

Record image ID/digest, source SHA, migration checksum, effective configuration
with secrets redacted, test evidence and approval in a G Sales manifest. Runtime
consumes that verified immutable artifact, not a mutable tag. API and optional
listener use this same image/SHA with separate commands. Shared AGATA processes
use the canonical AGATA artifact and their own fixed source/tenant/login identity.
Mac worker is an existing real asynchronous Codex provider used only in the
local development/QA profile for G Sales, not a synthetic LLM implementation.
Tests manually complete its durable RPC leases; they never invoke Codex/paid LLM.
G Sales itself depends on the AGATA HTTP contract, not on Mac hardware. Production
must use an OWNER-approved always-on server inference provider. A powered-on owner
MacBook is not an accepted production prerequisite. `AGATA_SALES_PRODUCTION_MODE`
rejects mac_worker/local_fake/disabled or local Codex execution; it also requires
explicit always-on provider approval and credentials. `GSALES_PRODUCTION_MODE`
checks that Core exposes the matching production readiness fact. No existing
GPoint worker is redirected or replaced. Server provider selection and availability
rehearsal are prerequisites to the later production release; no provider is enabled
or charged by this task.

No G Sales production workflow is dispatched by this change. Existing GPoint
Prepare/Deploy and release locks remain authoritative for shared Core changes.
The old guarded AGATA production migrator explicitly covers 0009→0010 only:
0010→0011 needs an operator-reviewed guarded transition before a later release.
This document/SQL preparation is not permission to bypass that gate.

## Provisioning and migration prerequisites

1. Owner reviews full unreleased diff and approves exact full SHA separately.
   PostgreSQL/RLS/session automation are high risk; task-scoped auto-release
   does not apply. Validate GitHub candidate and installed Mac worker parity.
2. Record current heads and runtime facts. Acquire canonical release lock.
   Create and verify a GPoint-only backup for public/agata/gsales and role identity
   control schemas; MAXIBOT database/runtime remains outside scope.
3. Inventory existing GPOINT runtime login and privileges. Prepare separate
   non-owner runtime logins, NOSUPERUSER/NOBYPASSRLS/NOCREATEROLE/NOCREATEDB,
   without memberships or schema CREATE. Never map a privileged application login.
   Core runtime grants cover only needed agata tables; migration version/control
   registry DML and DDL stay with an independent migrator.
4. Provide explicit nonempty `AGATA_ROLE_TENANT_BINDINGS` mapping for both
   actual GPOINT and GSALES Core logins before agata0011. Missing mappings must
   stop before mutation. Migration requires a trusted bypass administrator to
   enumerate every actual tenant; a restricted migrator is refused even with an
   operator-supplied inventory. All existing tenants must be mapped. Unknown logins fail closed; mutable app.tenant_id is
   no longer sufficient. Provision the GSALES tenant and approved policy profile
   through administrator setup, without inventing recipient consent.
5. Shared AGATA migration agata0011 adds restrictive role-binding FORCE RLS,
   private agata_identity registry, typed encrypted sales draft worker job and
   canonical message delivery metadata. It is forward-only. Test actual prod
   role/grant parity in an isolated restored GPoint backup before approval/apply.
6. Apply `gsales/001_gsales.sql` through its independent checksum migrator with
   `GSALES_MIGRATION_DATABASE_URL` outside Git and matching candidate/approved SHA.
   Runtime startup never migrates. Six business tables plus private migration
   receipts; no cross-product FKs/public rename. Existing Prisma migrations, if
   pending in the candidate, still use canonical `prisma migrate deploy`.
7. Configure secrets externally: distinct Core token/keys, owner API token,
   fixed tenant, database logins, selected existing management bot identity,
   personal-account vault key/path. Do not duplicate or repurpose GPoint tokens.

## Runtime acceptance and rollback

Start G Sales with all send/automation flags OFF. Verify API liveness/readiness,
Core runtime-security result, bot getMe identity, personal-account identity,
role isolation in both directions and no GPoint regression. Perform only an
explicitly approved real-account test with a known recipient and exact text;
confirm actual Telegram message/reply separately from health/heartbeat.

Before enabling any auto reply, owner enables seller and dialog permissions,
approved recipient policy/consent, bounded max replies and verified product facts.
Operator owns webhook setup and source routing. No broad account history import.

On failure: keep G Sales flags OFF, pause controls, preserve uncertain delivery
attempts, stop its API/listener only and restore the previous verified application
artifact. Do not drop schema, remove restrictive RLS or downgrade agata0011.
Database rollback requires the verified backup and a separate operator decision.
Finalize canonical release facts/manifest and agreed Linear publication only after
all technical gates pass; live owner acceptance remains a distinct recorded fact.

## External blockers

- Owner-selected management bot and approved MTProto test account are absent;
  live login/send/reply/takeover E2E is NOT RUN.
- Existing GPoint APIs are user-scoped. Authenticated Sales→Creator registration
  lookup contract is not approved/implemented; registration adapter remains OFF.
- Always-on production inference provider selection/approval and availability QA are pending; Mac development mode cannot satisfy production readiness.
- Production role inventory and guarded agata0010→0011 transition/restore rehearsal
  require the later exact-SHA release operation. This preparation does not mutate them.
