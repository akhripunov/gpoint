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
- [ ] Review the same-run resource sample in the sanitized inventory: each
      running container's instantaneous CPU percentage, memory use/limit,
      memory percentage, and PID count, plus host free disk, Docker-root free
      disk, total RAM, and available RAM. This is a point-in-time sample, not a
      historical peak; assess it against the current host workload and planned
      candidate images before authorizing cutover.
- [ ] After the fresh encrypted backup and isolated restore, the workflow
      compares current Docker-root free space with the exact candidate image
      size upper bound (sum of their uncompressed sizes) plus a 1 GiB reserve.
      A failed capacity check blocks before creating the GitHub Deployment or
      stopping any Compose service.
- [ ] Record the live Core and AGATA application SHAs, PostgreSQL server
      version, database names, migration heads, and pending migration set.
      Confirm that Kamal does not own or recreate PostgreSQL, Redis, Eros, or
      the isolated Worker services.
- [ ] The workflow's read-only migration parity gate must prove that every
      candidate Prisma migration is already applied and that the live AGATA
      Alembic head equals the candidate's sole head. A mismatch blocks before
      the GitHub Deployment is created and before any Compose service is
      stopped. This cutover does not apply database migrations; pending or
      forward-only AGATA migrations require a separately scoped, authorized
      migration operation and a later fresh cutover baseline.
- [ ] Inventory named and bind-mounted volumes, especially media, PostgreSQL,
      and any application state. Match every persistent mount to its intended
      Kamal role. Do not remove or rename volumes during this cutover.
- [ ] Take a new encrypted backup of every affected database and required
      persistent data. Record checksums and retention location outside the
      repository. Restore it into an isolated disposable environment and run
      schema, migration-head, application-read, and media checks there.

  The automated GPoint database gate requires the protected GitHub Actions
  secrets `GPOINT_KAMAL_BACKUP_AGE_RECIPIENT` (the public age recipient) and
  `GPOINT_KAMAL_BACKUP_OFFSITE_REMOTE` (an attempt-scoped rclone remote path).
  On the production host, provision `age`/`age-keygen`, `rclone`, the matching
  root-owned mode-0600 identity at `/opt/gpoint/env/gpoint-backup.agekey`, and
  a root-owned mode-0600 rclone config at `/root/.config/rclone/rclone.conf`.
  The identity is checked against the public recipient before backup. The
  plaintext custom archive and downloaded offsite copy live only in the
  `/run` tmpfs attempt directory and are removed after the isolated restore;
  `/opt/gpoint/backups` retains only the encrypted `.dump.age` object. Missing
  configuration or failed upload/readback/restore stops the cutover.
- [ ] Compare the candidate migrations with the live migration heads and the
      isolated restore. Stop if an old application cannot read data after the
      candidate migration or if restore evidence is incomplete.
- [ ] Confirm Kamal Proxy 0.9.2 can own the public HTTP/HTTPS listeners on
      ports 80 and 443 with TLS for `gpoint.amiora.one`. Verify `/api/*` and
      `/media/*` route to Core, while `/` and Mini App paths route to Mini App.
      Record the current Caddyfile and its checksum for recovery; Caddy must be
      stopped before Kamal Proxy binds public ports and remain inactive after
      successful cutover.
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
   GSales, and all volumes remain as inventoried.
3. Before starting Kamal Proxy on public ports, copy `/etc/caddy/Caddyfile` to
   the attempt-scoped recovery path under `/opt/gpoint/backups`, verify its
   checksum against the fresh baseline, then stop Caddy. The cutover helper
   fails closed if Caddy changed or remains active. Do not rewrite or reload
   Caddy.
4. Deploy only the Core API and AGATA API roles with Kamal while consumers
   remain paused. The deploy job must use role-scoped Kamal deploys and must not
   start Bot, Worker, or AGATA Memory Worker yet.
   Verify their exact SHA, immutable image digest, health checks, media volume,
   and required private network aliases.
5. Deploy Mini App through the same Kamal Proxy instance. Verify its public
   TLS listener on 443, API and media paths, and Mini App root before starting
   consumers. Caddy remains stopped. A later job failure does not automatically
   restart legacy Compose services; treat it as an unconfirmed Apply and
   reconcile the live Kamal Proxy, Caddy, and container state before any retry.
   Do not rerun Deploy blindly.
6. Start the Kamal Worker, Bot, and AGATA Memory Worker roles. Verify the old
   Compose consumers remain stopped and each new consumer has exactly the
   expected running instance count.
7. Run the complete production Verify: service SHA and image digest, API and
   AGATA health, Worker/Bot health, Mini App routes, media read/write, Kamal
   Proxy TLS and public ports, inactive Caddy,
   PostgreSQL/migration heads, network aliases, absence of duplicate
   consumers, stopped GSales, and preserved Eros ACL/private services.
8. Record `GitHub Deployment SUCCESS` only after every Verify assertion passes.
   Keep Compose/V5 artifacts and the previous application images available for
   recovery until the owner confirms the verified cutover and the release
   record is complete.

## Unconfirmed Apply and recovery

If Apply times out, is interrupted, or returns an unclear result, do not retry
Deploy. Freeze further automated changes and inspect the live container
inventory, image digests, running consumer count, Kamal Proxy routes, Caddy
service state, networks,
volumes, database heads, and Kamal Proxy service list. Reconcile observed state
with the exact candidate and the last-known-good application version before
choosing recovery.

Use Kamal's normal previous-version rollback only after confirming the old
application image is still available. The cutover migration parity gate proves
the database schema was unchanged by this workflow, so rollback does not depend
on reversing candidate migrations. If a separate migration operation occurred
after the baseline or migration compatibility is otherwise uncertain, keep
consumers paused and restore only through the separately tested database
recovery procedure. Never drop, recreate, or roll back PostgreSQL data by
assumption. Re-run the full production Verify after recovery and record whether
recovery or rollback completed.

## Legacy removal gate

Do not delete V5, Compose release workflows, release controllers, rollback
scripts, or release history until production Verify passes, the deployment
record confirms `GitHub Deployment SUCCESS`, and the owner authorizes the
post-cutover cleanup. Before deleting code, audit each dependency and preserve
runtime, security, migration, backup, monitoring, data, and rollback assets.
Then remove only code proven exclusive to superseded release paths and verify
the one remaining Kamal workflow from a clean exact-SHA checkout.
