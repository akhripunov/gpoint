# ME-36: Preserve intentionally stopped GSales during a V5 Eros release

Status: PROPOSED. OWNER selected shared PostgreSQL and a separate ACL plan; the dedicated-container fallback is NOT APPROVED. No new release-path implementation or ACL Apply is approved by this document.

## Intent and confirmed requirements

OWNER selected: keep GSales stopped and prepare/verify a corresponding V5 path.
The approved permanent Eros plan already requires GSales stopped for RAM.
Release GPoint plus permanent stock Eros in synthetic-only mode, retaining AGATA
until separate functional/data acceptance. Do not touch Incy/VPN/network policy,
installed AGATA Workers, MAXIBOT, real-user Eros routing or production secrets.

## Approaches

1. Start GSales during ordinary full V5 Deploy: existing code, but conflicts with
   OWNER's chosen stopped state and increases RAM. Rejected by OWNER.
2. Direct SSH/Compose installation of Eros: avoids GSales start but bypasses V5
   provenance/approval and does not update the GPoint API gateway. Rejected.
3. Explicit stopped-service contract in existing V5: selected proposal. Keep the
   ordinary full-release path strict; represent stopped GSales as preserved,
   inactive baseline services, rather than claiming candidate runtime/health.

## Proposed behavior

- Add an explicit preserve-stopped-GSales release option, authenticated by the
  same OWNER/exact-SHA authorization as the release. Default behavior stays strict.
- The contract names exactly compose-gsales-api-1, compose-gsales-agata-api-1 and
  compose-gsales-agata-memory-worker-1. All must already exist and be stopped.
  Missing services, mixed running/stopped state, unknown identity or stale state
  block before any mutation. The option never stops a running service.
- Fresh canonical preflight, under the release lock, captures container IDs,
  image IDs and source revisions, restart policies, GSales DB heads/checksums,
  and the independently verified successful parent Deployment. Preserve rollback
  images. Record a digest of the safe identity/state snapshot, without secrets.
- Prepare continues all required tests, migration rehearsals, build/receipt and
  immutable GHCR gates for the full candidate. No skipped security/test gate.
- Apply may update approved GPoint/API/Worker/Bot, primary AGATA and Eros. It does
  not start, recreate, migrate, or configure the three preserved GSales services
  or their schema/tenant. Existing PostgreSQL/shared-resource effects require
  the ordinary protected backup, migration compatibility and restore gates.
- Pre-Apply compatibility rejects migrations/shared configuration that would
  invalidate preserved GSales state. Unknown compatibility blocks. Do not infer
  compatibility from a matching migration head alone.
- Verify the stopped-service snapshot again immediately before mutation and
  after Apply. Container/image identity, restart policy and DB head/checksum
  drift block success. No GSales inference/health PASS is claimed while stopped.
- GitHub Deployment and immutable evidence distinguish active candidate runtime
  from preserved inactive GSales. Parent-state and future-release validation
  understand this distinction; a release cannot silently resume stale GSales.
- GSales reactivation is a separate explicit OWNER action using compatible
  images/migrations and fresh normal health/security checks.
- Keep mandatory GPoint/AGATA/Worker compatibility, current live baseline,
  synthetic-only routing, RAM/disk reserve, protected backup/restore, immutable
  receipt/image identity, one-Apply, health and evidence publication gates.
- Unknown or partial Apply outcome remains reconciliation-only. No second Apply
  to repair metadata; no removal of locks, failed guards or original evidence.

## Fresh database isolation finding and proposed fallback

Fresh read-only VPS checks in this continuation confirmed production SHA
`56e987a8abe3d6e65c530c34d4bbcb4d04bb5d32`, Prisma
`20261015130000_codex_telegram_challenge`, AGATA `agata0011`, GSales `001_gsales`
(checksum `8de19ec2efd81848010379645d51243ac3f4a4cd37af0be211d71cdf36bcd111`).
Eros database and protected Eros environment are absent. At least one existing
non-template database permits PUBLIC CONNECT. No database was altered.

Current provisioner correctly rejects this condition: Eros runtime requires
BYPASSRLS and must not inherit access outside its own database. This is another
blocking prerequisite, not a reason to bypass the isolation check or rewrite
existing shared database grants.

Rejected fallback (retained for decision history, do not implement): use a dedicated persistent
Eros PostgreSQL container on an internal-only Eros data network, no published
port and no access to shared_internal. Reuse the already reviewed pilot role /
one-shot migration separation, with root-owned persistent credentials and a
persistent Eros-only volume. Keep one existing GPoint API; do not restore a
second pilot API. Pin PostgreSQL/pgvector by exact digest, verify image identity
and include the dependency in canonical evidence. GPoint/AGATA/GSales database
roles, ACLs and volumes remain untouched by Eros provisioning.

The original ME-36 plan names an isolated database container as fallback when
existing-cluster isolation cannot be proven. Queue wording more recently names
existing PostgreSQL, OWNER explicitly rejected this fallback and selected a separate ACL plan for the shared server.
Existing-cluster use is the alternative only after a separately reviewed and
explicitly approved ACL plan; no ACL mutation is included in this proposal.

Reserve memory for the extra container, include its limits in the pre-Apply
capacity gate, and measure actual VPS usage after start. Fail before mutation
if current RAM/disk cannot accommodate the configured caps plus reserve; do not
count swap, stop AGATA, start GSales or reduce safety-service limits to fit.
Protect Eros-only backups outside GHCR, prove restore in disposable state, and
roll back only Eros services/config while retaining its persistent volume.

## Current OWNER decision — shared PostgreSQL

Prepare and review `../plans/2026-10-24-me36-shared-postgres-acl.md` before any ACL mutation. Preserve stopped GSales. Do not create a dedicated PostgreSQL container. GSales preservation behavior above remains proposed pending its implementation plan/approval; no release approval is inferred.

## Builder decisions for the implementation plan

- Preserved database objects are the `gsales` business schema,
  `gsales_meta.migrations` ledger, the GSales AGATA schema identified from the
  existing protected runtime DSN, source-specific role attributes/memberships
  and grants, and the GSales tenant configuration. Read secret values only
  through existing protected code paths; evidence contains safe identity hashes.
  Shared PostgreSQL is not owned by the inactive service. Record all planned
  primary GPoint/AGATA schema effects and fail if they alter the preserved set.
- Evidence will add an explicit schema-versioned `inactiveServices.gsales`
  record with `state=INTENTIONALLY_STOPPED`, the three exact service identities,
  protected-state snapshot digest and database migration identity. Active image
  mappings must not invent candidate identity for these services. Candidate,
  baseline SHA and Prepare/Deploy run/attempt bind the record. Unknown versions,
  omitted/changed records and mismatched fresh snapshots fail parent validation.
  All consumers of parent state and component reuse need tests for this record.
- Provider repair is exactly the existing API hostname `api`, chat provider
  `gpoint` with `GPOINT_API_KEY` bound to `EROS_BRIDGE_API_KEY`, and embedding
  provider `gpoint_embeddings` with `GPOINT_EMBEDDINGS_API_KEY` bound to
  `EROS_EMBEDDING_API_KEY`. The embedding model remains `qwen3-embedding:0.6b`.
  Stock source at pinned upstream SHA supports this provider naming/key contract.

## Validation and acceptance

1. Default V5 path remains strict and rejects missing/unhealthy active services.
2. Preserve mode rejects running, mixed, missing or unverified GSales before any
   mutating command, and cannot run without applicable OWNER authorization.
3. Simulated canonical command trace contains no GSales start/recreate/migration
   and preserves exact stopped service and DB snapshots.
4. Drift before/during Apply blocks completion; repeated/ambiguous dispatch does
   not create another Apply. Failure evidence remains truthful.
5. Parent Deployment/read-back round trip preserves inactive source identities;
   future releases cannot mislabel them as candidate images or healthy services.
6. Full existing release suite plus targeted security/migration/state tests and
   independent review pass on a clean exact commit. Handoff Gate PASS is scoped
   evidence only, never production approval.
7. New exact-current-main Prepare passes, then fresh canonical read-only VPS
   preflight proves baseline, capacity, protected backup/restore, DB isolation
   and installed Worker compatibility. All production approvals still apply.

## Limits and open implementation facts

Native runner TLS EOF/timeouts and current SSH failures are transport blockers;
this design neither attributes them to Incy nor changes network settings.
The current permanent Eros candidate also needs independent provider DNS/key
corrections; those are a separate small repair in this same release task.
Exact live VPS identities and schema ownership must be read afresh before the
implementation can claim a complete compatibility proof. If that proof cannot
be obtained, retain BLOCKED and do not weaken the contract to force a release.
