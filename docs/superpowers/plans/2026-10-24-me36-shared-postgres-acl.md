# ME-36 Shared PostgreSQL ACL Implementation Plan

> **For agentic workers:** After OWNER approval, use superpowers:executing-plans or superpowers:subagent-driven-development. This is a PROPOSED plan, not authorization to run SQL or Deploy.

**Goal:** Make Eros database isolation possible on the existing PostgreSQL server without changing application database ACLs or starting GSales.

**Architecture:** Remove only PUBLIC CONNECT from the two verified maintenance databases, preserve owners and explicit grants, then let the existing fail-closed Eros provisioner verify isolation again. Treat ACL hardening and Eros provisioning as distinct protected steps. Keep all existing V5 gates.

**Tech Stack:** Existing PostgreSQL in `shared_postgres_1`, reviewed Python/SQL release tooling, V5 Mac runner, GitHub/GHCR evidence, Linear ME-36.

**Spec:** OWNER decision in Codex chat `01a16c9a-a660-7d52-bec5-cb45a8a1d8c6`: keep shared PostgreSQL and first prepare a separate ACL plan. Related stopped-GSales proposal: `../specs/2026-10-24-me36-preserve-stopped-gsales-design.md`.

## Status and fresh facts — 24 October 2026

No ACL or production mutation has been made. Read-only catalog evidence:

| Database | Owner | PUBLIC CONNECT | Recorded ACL |
|---|---|---|---|
| gpoint | gpoint | false | PUBLIC TEMP; owner CREATE/TEMP/CONNECT; explicit CONNECT for gsales_runtime and gsales_agata_runtime |
| postgres | postgres | true | default ACL |
| template1 | postgres | true | PUBLIC CONNECT; owner CREATE/TEMP/CONNECT |

The other existing application database has no PUBLIC CONNECT; its name/ACL are excluded from the plan. No connection to it was made. The maintenance-connection snapshot showed only one `postgres` role connection to `postgres` (the inspector), and no template1 sessions. This is a momentary snapshot, NOT proof that no scheduled/future client relies on PUBLIC CONNECT. Existing Eros roles count is zero, and the eros database/environment are absent.

Production source is `56e987a8abe3d6e65c530c34d4bbcb4d04bb5d32`; Prisma `20261015130000_codex_telegram_challenge`; AGATA `agata0011`; GSales `001_gsales`, checksum `8de19ec2efd81848010379645d51243ac3f4a4cd37af0be211d71cdf36bcd111`. GSales' three containers are stopped. Recheck all facts before execution.

The existing provisioner at `scripts/eros-production-secrets.py:202` includes template databases that allow connections. Therefore closing only `postgres` is insufficient: template1 also blocks isolation. Eros runtime has BYPASSRLS; granting it indirect access to another database is unacceptable. The current rejection remains mandatory.

## Global constraints

- Proposed SQL scope is EXACTLY `postgres` and `template1`, and only the PUBLIC CONNECT privilege. Do not generalize to every database.
- Do not change ACLs, ownership, roles, passwords, schemas or data of gpoint, AGATA, GSales, MAXIBOT or another application database in this ACL step.
- Do not revoke PUBLIC TEMP, CREATE, role membership or explicit application grants. Do not create a second PostgreSQL container.
- Incy/VPN/DNS/routes/proxy/TLS and installed Product/LAB Workers remain unchanged. GSales remains stopped.
- Require explicit OWNER approval of this plan's exact ACL scope and final reviewed source SHA. A request to prepare the plan is not permission to Apply.
- Preserve release lock, backup/restore, current-main Prepare, immutable image/receipt proof, source/Worker compatibility, RAM/disk, synthetic-only routing and one-Apply rules.

## Review focus

- A scheduled maintenance client may use PUBLIC CONNECT even when no session is present; identify its role or stop before applying.
- A concurrent ACL/database/role change invalidates the captured baseline; stop rather than overwrite it.
- Default/null ACL and explicit ACL may have identical semantics; compare normalized effective privileges, not raw ACL string equality.
- A transaction/transport failure may have an unknown commit result; reconcile read-only, never blindly repeat SQL.
- Re-granting PUBLIC CONNECT AFTER Eros BYPASSRLS roles exist would reopen cross-database access. Do not do that as ordinary release rollback.

---

### Task 1: Freeze exact scope and prove prerequisites (read-only)

**Files to implement after approval:** `scripts/eros-postgres-acl.py` (new narrowly scoped planner/apply helper), `tests/test_eros_postgres_acl.py`, `tests/test_eros_postgres_acl_docker.py`, `.github/workflows/gpoint-deploy.yml` and selector/workflow-security tests. No automatic ACL Apply.

- [ ] Record exact approved source SHA, production SHA/DB heads, cluster/container identity and OWNER instruction in existing ME-36.
- [ ] Re-read only the two target maintenance database ACLs, their OIDs/owners, applicable role attributes/memberships and connection counts; inventory known healthchecks, backups and maintenance clients that connect to them. Do not collect query text, client IPs, passwords or private payloads.
- [ ] Require OWNER/operator confirmation that any legitimate non-owner maintenance client has an explicit approved CONNECT grant. If a client relies solely on PUBLIC, stop and add its exact grant to a separately reviewed amendment; do not invent grants from current sessions.
- [ ] Confirm every other connectable database already denies effective CONNECT to prospective Eros roles, including through PUBLIC/membership; require the known allowlist of target exceptions only. Check template1, not just non-template databases.
- [ ] Confirm no Eros roles/database exist and no release/DDL operation is active. If existing Eros state appears, this initial-bootstrap plan is inapplicable and stops.
- [ ] Save the exact two-database ACL preimage and normalized effective privilege proof in a root-owned mode-0600 protected VPS evidence file. No plaintext secrets or global role dumps. Hash and read back the file; record only safe identity/hash in Linear.

**Planned interfaces / operator route:**

- Helper CLI: `plan --candidate <40-char SHA> --output <protected plan JSON>`;
  `inspect --plan <protected plan JSON>`; `apply --plan <protected plan JSON>
  --candidate <40-char SHA> --owner-approved-sha <same SHA>`; `rollback` with the
  same exact confirmation and explicit pre-provisioning guard.
- JSON schema `gpoint-eros-postgres-acl/v1`: exact candidate, production baseline,
  cluster/container identity, target database OIDs/owners, normalized before/
  intended-after ACLs, protected preimage path/hash, maintenance-window evidence,
  and operation/result identity. Exactly two target database names are permitted.
  Unknown/extra target, schema version, mutable identity or missing proof fails.
- Extend the existing trusted Deploy workflow with explicit `eros-acl-plan`
  read-only and `eros-acl-apply` protected modes. `approve_eros_acl` defaults to
  false; Apply requires exact candidate and direct repository-owner actor/
  triggering_actor. Existing production, task-auto and pilot jobs are excluded
  for both modes. No generic environment variable may opt out of those guards.
- Reuse exact-current-main successful Prepare/immutable provenance validation,
  canonical Mac runner and release lock. No standalone SSH/manual SQL shortcut,
  automatic controller dispatch, general runtime Apply, or GSales start.
- Unit/workflow tests must prove unauthorized actor, missing confirmation,
  wrong SHA/Prepare, extra database and default mode each execute zero mutations.

### Task 2: Prove semantics in disposable PostgreSQL

- [ ] Write a failing isolation test: an isolated BYPASSRLS NOINHERIT login can CONNECT to postgres/template1 through PUBLIC before hardening; that is the reproduced defect.
- [ ] Use the same PostgreSQL major version and synthetic owner/application/maintenance/Eros roles. No production DSN or real credentials.
- [ ] Execute the exact two REVOKEs inside a transaction. Prove new Eros connections to the maintenance databases fail, direct approved maintenance/owner access stays, and explicit GPoint/GSales database grants are unchanged.
- [ ] Prove unrelated database ACL snapshots are identical and no table/schema/user data changes occur. Test absent/unexpected database, changed owner/ACL, running Eros roles, failed SQL, repeated invocation and unknown-commit reconciliation. Require explicit RESTRICT and test grant/ownership drift; never switch to CASCADE to force a result.
- [ ] Prove pre-provisioning rollback restores original normalized privileges, retaining the original TEMP/CREATE/explicit grants. Prove rollback is rejected as soon as Eros roles/database appear.
- [ ] Run focused unit/integration and existing Eros provisioning regressions. Independently review the security/rollback logic; commit scoped changes and require Handoff Gate PASS.

### Task 3: Separately authorize and apply only the reviewed ACL delta

**Reviewable SQL delta (not executed):**

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '10s';
-- The reviewed helper rechecks the exact saved owners/OIDs/effective ACL
-- baseline and absence of Eros roles before reaching these statements.
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC RESTRICT;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC RESTRICT;
-- Validate expected post-state and unchanged explicit grants before COMMIT.
COMMIT;
```

- [ ] Obtain separate explicit OWNER authorization naming this delta, the tested helper SHA and the recorded baseline. Use the canonical protected GPoint operator path under the release lock; never paste an unreviewed ad-hoc SQL replacement into production.
- [ ] Establish an explicit OWNER-coordinated exclusive ACL/DDL maintenance window for this shared PostgreSQL cluster. Its recorded scope must cover every actor able to change databases, roles/memberships or grants, including scheduled automation and interactive DBAs, from the final baseline read through COMMIT and independent readback. The GPoint release/advisory lock alone does NOT fence non-cooperating actors. Require a documented operator barrier/coordination mechanism; if it cannot be established without unapproved interference with another project, stop before Apply. Do not terminate other sessions, alter their roles or stop their automation to manufacture a barrier.
- [ ] Under that window and the release lock, require fresh baseline equality and successful protected evidence read-back immediately before mutation. Stop on any out-of-scope database requiring change. No CASCADE or expansion of the target list.
- [ ] Execute once in one transaction, with bounded timeouts and no connection termination. Existing sessions are not forcibly disconnected. SQL error before COMMIT rolls back the whole delta.
- [ ] If transport becomes ambiguous, inspect the same saved pre/post identity and actual ACL state. Do not repeat Apply merely because the client did not get a response.
- [ ] Independently read back: PUBLIC CONNECT false on both targets; owner/explicit approved access retained; gpoint/application ACLs unchanged; service health/DB heads unchanged; GSales still stopped. Record exact result in ME-36/Queue/journal.

### Task 4: Rollback boundary and release resumption

- [ ] BEFORE Eros roles/database exist, an explicitly authorized ACL rollback may restore only the two PUBLIC CONNECT grants that were present in the saved preimage, after checking no later ACL drift. Validate normalized privileges after restoration.
- [ ] AFTER Eros BYPASSRLS roles/database exist, do NOT restore PUBLIC CONNECT. Ordinary Eros application rollback must retain the hardened maintenance ACLs. A full reversal requires a separately approved Eros decommission/role-revocation plan and fresh isolation proof first.
- [ ] ACL success does not authorize Eros install, migration or GSales restart. Resume only after the separate V5 stopped-GSales implementation is approved/reviewed and passes tests, exact-current-main Prepare and fresh canonical preflight.
- [ ] Eros provisioning must independently verify zero foreign effective CONNECT for its actual runtime/migrator roles before startup and again during postflight. New databases/role grants that reopen access block subsequent releases; this plan adds no cluster-wide trigger or autonomous ACL policy.
- [ ] Preserve protected GPoint-only data backup/restore/rollback for the pending application migrations, and report actual runtime, Worker dependency readiness and owner/device smoke separately.

## Acceptance / stop conditions

Accept only a verified two-database PUBLIC CONNECT removal with preserved explicit access and unchanged application DB ACLs, data and service state. Stop for unknown maintenance clients, broader ACL needs, live baseline drift, existing Eros state, failed/ambiguous SQL, failed backup/evidence, or missing OWNER authorization. No automatic retry or topology substitution.

## Not approved or performed

No REVOKE/GRANT, role creation, secret provisioning, migration, Eros Apply, GSales start, Worker transition or VPN change is performed by writing this plan. Release remains BLOCKED pending plan/operation approval and the remaining V5 gates.
