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
  -t gpoint-g-sales:<40-char-sha> services/gsales
```

Record image ID/digest, source SHA, migration checksum, effective configuration
with secrets redacted, test evidence and approval in a G Sales manifest. Runtime
consumes that verified immutable artifact, not a mutable tag. API and optional
listener use this same image/SHA with separate commands. Shared AGATA processes
use the canonical AGATA artifact and their own fixed source/tenant/login identity.
The owner-approved Unified Mac Worker V3 is a supported production inference
provider for G Sales through the shared AGATA Core. Tests manually complete its
durable RPC leases; they never invoke Codex/paid LLM. G Sales depends on the
authenticated AGATA HTTP contract and the shared worker's availability when
generation is requested. Production mode rejects local_fake/disabled inference;
`mac_worker` requires the approved shared worker configuration. If the worker is
offline, queued drafts remain durable and runtime readiness is reported degraded;
no generated response or delivery is claimed. The already installed GPOINT lane
is not redirected or replaced. An external always-on server provider remains a
separately approved alternative.

V1.1 prepares shared worker v3 two-target routing; installed worker remains v2.
See [worker integration](worker-integration.md) for the audited boundaries and
separate-approval controlled Codex rehearsal. Canonical build/test context is
`services/gsales/`; the production tenant is explicitly provisioned and must be
bound to `gsales_runtime` in the shared AGATA login-to-tenant registry. The
repository's default UUID is test-only and is rejected in production mode.

No G Sales production workflow is dispatched by this change. Existing GPoint
Prepare/Deploy and release locks remain authoritative for shared Core changes.
The canonical release workflow guards both 0009→0010 and 0010→0011 with a fresh
authoritative tenant census, a retained tenant lock, exact baseline checks and
a complete protected backup. agata0011 additionally requires the privileged
migration identity that enumerates the full tenant registry. This document/SQL
preparation is not permission to bypass those gates.

## Provisioning and migration prerequisites

1. Owner reviews full unreleased diff and approves exact full SHA separately.
   PostgreSQL/RLS/session automation are high risk; task-scoped auto-release
   does not apply. Validate GitHub candidate and installed Mac worker parity.
2. Record current heads and runtime facts. Acquire canonical release lock.
   Create and verify a GPoint-only backup for public/agata/gsales and role identity
   control schemas; MAXIBOT database/runtime remains outside scope.
3. Inventory existing GPOINT runtime login and privileges. Prepare separate
   non-owner runtime logins, including `gsales_runtime`, with
   NOSUPERUSER/NOBYPASSRLS/NOCREATEROLE/NOCREATEDB,
   without memberships or schema CREATE. Never map a privileged application login.
   Core runtime grants cover only needed agata tables; migration version/control
   registry DML and DDL stay with an independent migrator.
4. Provide explicit nonempty `AGATA_ROLE_TENANT_BINDINGS` mapping for actual
   GPOINT and GSALES Core/runtime logins before agata0011. Missing mappings must
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

The optional `gsales` Compose profile remains separate from the GPoint-only
base Compose file. Enabling it requires nine owner-provisioned
`GSALES_AGATA_*` settings: `DATABASE_URL` (dedicated `gsales_runtime` login to
the shared AGATA database), `TENANT_ID` (provisioned UUID bound to that login),
`INTERNAL_API_TOKEN` (distinct lane token, also used by the GSales API client),
`LLM_PROVIDER` (approved provider), `MESSAGE_AEAD_KEY` (base64 decoding to
exactly 32 bytes), and `REQUEST_FINGERPRINT_HMAC_KEY`,
`RETRIEVAL_QUERY_HMAC_KEY`, `SHADOW_CANDIDATE_HMAC_KEY`, and
`RESPONSE_FINGERPRINT_HMAC_KEY` (each base64 decoding to at least 32 bytes).
All five cryptographic keys must be unique and separate from GPOINT's. Compose
fails closed if any is absent. No production values are checked into this
repository; enabling the profile awaits provisioning and provider approval.
`GSALES_PRODUCTION_MODE=true` also enables the matching AGATA sales-draft and
sales-production settings; all Telegram and outreach sending flags remain off.

## Exact schema impact for OWNER review

`gsales/001_gsales.sql` creates six **business** tables. The earlier three-table
CRM sketch covered seller, lead and Telegram-account records only; it could not
preserve source provenance, the approval/delivery audit trail, or durable
per-dialogue safety controls. Those are separate business responsibilities:

| Table | Why it is required |
| --- | --- |
| `telegram_accounts` | Account identity and secret reference; never the session secret itself. |
| `sellers` | Seller profile and its AGATA Character binding. |
| `leads` | Tenant-scoped prospect identity, eligibility and sales stage. |
| `lead_sources` | Multiple discovery sources and provenance without overwriting history. |
| `outreach_attempts` | Owner approval, recipient/content binding, idempotency and delivery outcome. |
| `dialog_controls` | Durable pause, opt-out, manual takeover and auto-reply policy per dialogue. |

Two additional database objects are technical control-plane state rather than
business tables: `gsales_meta.migrations` records checksummed G Sales migration
receipts; `agata_identity.role_tenants` binds AGATA database login identities to
the complete tenant registry for FORCE RLS. The latter belongs to shared AGATA
and is created by guarded `agata0011`; it is not owned or migrated by G Sales.
This report does not apply either schema to production.

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
- Production tenant/role provisioning, fresh runtime identity, and restored
  production backup rehearsal require the later exact-SHA release operation.
  This preparation does not mutate production.
