# GPoint Compose/V5 to Kamal cutover

This runbook describes the first production handoff from Compose/V5 to Kamal.
It is a fail-closed checklist, not an automated release controller. Do not
start Apply from this document. The release operator must use the approved
exact-SHA production authorization and the existing protected GitHub
Environment.

## Ready checks

Record the evidence against the exact candidate SHA and keep the legacy release
path available until the Kamal production Verify has passed.

- [ ] Confirm the owner-approved candidate SHA and its successful CI/BuildKit
      results. Record the immutable GHCR digest for Core, AGATA, and Mini App.
- [ ] Resolve `GPOINT_KAMAL_HOST` from the protected GitHub secret and verify
      that the pinned `GPOINT_KAMAL_KNOWN_HOSTS` entry matches that exact host.
      The repository must not treat a historical IP address as current target
      evidence.
- [ ] Capture a fresh VPS inventory: every container, image ID/digest, Compose
      project/service label, health state, restart policy, network, published
      port, mount, and owner. Include Caddy, PostgreSQL, Redis, Eros, and the
      stopped GSales containers. Do not infer this from an old manifest.
- [ ] Record the live Core and AGATA application SHAs, PostgreSQL server
      version, database names, migration heads, and pending migration set.
      Confirm that Kamal does not own or recreate PostgreSQL, Redis, Eros, or
      the isolated Worker services.
- [ ] Inventory named and bind-mounted volumes, especially media, PostgreSQL,
      and any application state. Match every persistent mount to its intended
      Kamal role. Do not remove or rename volumes during this cutover.
- [ ] Take a new encrypted backup of every affected database and required
      persistent data. Record checksums and retention location outside the
      repository. Restore it into an isolated disposable environment and run
      schema, migration-head, application-read, and media checks there.
- [ ] Compare the candidate migrations with the live migration heads and the
      isolated restore. Stop if an old application cannot read data after the
      candidate migration or if restore evidence is incomplete.
- [ ] Confirm `kamal-proxy` 0.9.2 can bind the configured loopback listener and
      that Caddy routes `/api/*`, rewritten `/media/*`, and Mini App paths to
      that one listener. Verify the existing TLS certificate and that the
      production Caddy service can validate and reload its config.
- [ ] Confirm the current Worker, Bot, and AGATA Memory Worker consumer counts and their shutdown
      behavior. Plan a short consumer pause so old Compose consumers are stopped
      and verified stopped before Kamal starts their replacements. Do not allow
      overlapping consumers. GSales must remain stopped.
- [ ] Confirm AGATA, Eros ACL, shared Postgres networks, and private network
      aliases against the live inventory. Do not disconnect or recreate them.
- [ ] Confirm the tested recovery procedure below, the protected Environment
      approval, deployment concurrency, and the Kamal deployment lock are all
      available for the exact candidate.

Any unchecked item is a hard stop. A missing VPS fact must be read from the VPS
at preparation time; never fill it from a previous release record.

## First cutover sequence

1. Recheck the production SHA, migration heads, container inventory, and backup
   immediately before Apply. Abort if any observed value differs from the
   approved record.
2. After the exact-SHA Deployment is marked `in_progress`, the workflow runs
   `scripts/pause-legacy-gpoint.sh` against the same-run baseline and inventory.
   It verifies both checksums, the live PostgreSQL/Eros/Caddy identities, and
   the GSales stopped state, then gracefully stops only the five exact
   inventoried Compose app container IDs with a 120-second timeout. If that
   step fails before Kamal starts, it attempts to restart only the containers
   it stopped and fails the job. PostgreSQL, Redis, Caddy, Eros, AGATA data,
   GSales, and all volumes remain running or stopped as inventoried.
3. Deploy only the Core API and AGATA API roles with Kamal while consumers
   remain paused. The deploy job must use role-scoped Kamal deploys and must not
   start Bot, Worker, or AGATA Memory Worker yet.
   Verify their exact SHA, immutable image digest, health checks, media volume,
   and required private network aliases.
4. Deploy Mini App through the same Kamal Proxy instance.
5. The gated Kamal workflow backs up `/etc/caddy/Caddyfile`, records its
   checksum, validates the candidate Caddyfile, atomically installs it, and
   reloads Caddy after the request-serving Kamal roles are deployed. A later
   job failure does not automatically restart legacy Compose services; treat
   that as an unconfirmed Apply and reconcile before any retry. Verify
   HTTPS API and Mini App routes before starting consumers. If the Apply step
   fails or its result is unclear, stop automation and reconcile the live Caddy
   and container state; do not rerun Deploy blindly.
6. Start the Kamal Worker, Bot, and AGATA Memory Worker roles. Verify the old
   Compose consumers remain stopped and each new consumer has exactly the
   expected running instance count.
7. Run the complete production Verify: service SHA and image digest, API and
   AGATA health, Worker/Bot health, Mini App routes, media read/write, Caddy TLS,
   PostgreSQL/migration heads, network aliases, absence of duplicate
   consumers, stopped GSales, and preserved Eros ACL/private services.
8. Record `GitHub Deployment SUCCESS` only after every Verify assertion passes.
   Keep Compose/V5 artifacts and the previous application images available for
   recovery until the owner confirms the verified cutover and the release
   record is complete.

## Unconfirmed Apply and recovery

If Apply times out, is interrupted, or returns an unclear result, do not retry
Deploy. Freeze further automated changes and inspect the live container
inventory, image digests, running consumer count, Caddy routes, networks,
volumes, database heads, and Kamal Proxy service list. Reconcile observed state
with the exact candidate and the last-known-good application version before
choosing recovery.

Use Kamal's normal previous-version rollback only after confirming the old
application image is still available and compatible with the current database
schema. If migration compatibility is uncertain, keep consumers paused and
restore only through the separately tested database recovery procedure. Never
drop, recreate, or roll back PostgreSQL data by assumption. Re-run the full
production Verify after recovery and record whether recovery or rollback
completed.

## Legacy removal gate

Do not delete V5, Compose release workflows, release controllers, rollback
scripts, or release history until production Verify passes, the deployment
record confirms `GitHub Deployment SUCCESS`, and the owner authorizes the
post-cutover cleanup. Before deleting code, audit each dependency and preserve
runtime, security, migration, backup, monitoring, data, and rollback assets.
Then remove only code proven exclusive to superseded release paths and verify
the one remaining Kamal workflow from a clean exact-SHA checkout.
