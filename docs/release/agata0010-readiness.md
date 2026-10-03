# agata0010 production readiness

This change closes the production Unread Messages migration blocker. It does
not modify the unread feature, production credentials, RLS policies, or MaxiBot.

## Confirmed baseline

On 2026-10-03 the read-only production probe confirmed `agata0009`, effective
migrator `gpoint` selected from `AGATA_DATABASE_URL`, NOSUPERUSER/NOBYPASSRLS,
and enabled FORCE RLS. The authoritative administrator census contained one
tenant, 12 messages, and five historical assistant messages eligible for baseline.
The table occupied 114688 bytes. Tenant IDs and credentials are not published.

The old preflight did not prove migration readiness. The old apply neither
supplied complete `AGATA_MIGRATION_TENANT_IDS` nor guaranteed a complete logical
backup under FORCE RLS.

## Canonical flow

* Dry-run checks the effective Alembic URL precedence, role ownership/privileges,
  RLS facts, head, authoritative census, and matching GPoint database instance.
  Evidence is stored in the planned manifest. A successful Prepare is not apply.
* A pending agata0010 release creates a full public/agata logical backup through
  the existing administrator connection, with protected permissions, nonempty
  archive, archive TOC and checksum. This identity is not given to Alembic.
* Immediately before migration, a separate administrator transaction holds a
  SHARE lock on `agata.tenants`. Full census is collected using a separate
  read-only transaction that closes before Alembic takes its messages DDL lock.
  Tenant inventory must match the held census. IDs are supplied only to the
  transient restricted migration container using the immutable candidate image.
* Concurrent tenant writes cannot change that inventory. Guard loss or timeout
  aborts the migration container; the guard is always closed on exit. Existing
  message IDs eligible before migration are verified after Alembic commits.
  Newly arriving messages are not falsely marked read by the verification.
* Backup and baseline proof are parsed before runtime switching and retained in
  the production manifest, including exact candidate SHA and inventory checksum.

## Lock envelope and failure handling

The guard supports only `agata0009 -> agata0010`. It refuses other baselines,
missing or nonauthoritative inventories, inconsistent database identities, and
missing privileges. Conservative limits are 10000 baseline rows and 64 MiB table
size; larger volumes need a separately evaluated release plan.

`ADD COLUMN` obtains ACCESS EXCLUSIVE on messages for the migration transaction.
The migrator receives PGOPTIONS before connection establishment: lock timeout
15 seconds and statement timeout 120 seconds. Container deadline is 150 seconds,
guard idle timeout 180 seconds. A timeout is a failed gate, not automatic success
or a permission to retry apply. Forward-only migration recovery rules remain.

## Verification

* Unit regressions: missing inventory, restricted/filtered census, missing rights,
  wrong database instance, guard loss, incomplete baseline and withheld IDs.
* Release regressions: pending migration preflight and structured backup/SHA proof.
* Docker protocol regression uses an isolated PostgreSQL and the real AGATA image:
  failure without inventory; full backup restored across both tenant fixtures;
  tenant insert refused during the guard; both baselines populated with FORCE RLS.
* GitHub Prepare runs this protocol against the exact freshly built candidate
  image, in addition to the existing actual-image migration/health rehearsal.

Production apply and migrations require separate exact-SHA OWNER approval after
the new complete Prepare and canonical read-only preflight pass.
