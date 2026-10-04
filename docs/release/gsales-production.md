# GSales production integration

The release candidate includes GSales and the already installed unified V3
Mac worker source. The Mac is the production inference provider. GSales service,
its Core API and memory worker run on the VPS; the V3 inference worker stays on
the Mac. No installation or source replacement occurs during this rollout.

Prepare validates three immutable application images, source and migration
checksums, guarded migration replay, real tenant isolation, complete backup
restore, protected first provisioning and deployment image-store parity.
Publish one reviewed final main SHA; a branch rehearsal does not authorize
production. A successful Prepare and Linear publication are mandatory.

This rollout changes database permissions and schemas. OWNER must approve the
exact full candidate SHA and schema impact before production. The existing
Deploy production confirmation supplies that exact approval. Its approved
setup runs through `release:gpoint`, under the shared Mac production lock.
Read-only Deploy preflight does not perform setup.

First setup records the old production SHA and Prisma/AGATA/GSales heads,
checks the real tenant census, creates a complete GPoint-only protected backup,
provisions a new real GSales tenant and two restricted login roles, and writes
private configuration. Unknown tenants, privileged existing runtime roles,
ambiguous commits or unsafe credential files stop the release. Migration DSNs
remain in `/opt/gpoint/env/migration.env`, mode 0600, outside application
runtime environment files. Credentials are never emitted to logs or Linear.

The Mac configuration step preserves the GPOINT and LAB settings, checks the
installed source bundle against the candidate, and adds the authenticated
GSALES lane. A required restart uses the existing LaunchAgent and source.
An idle process is frozen briefly to prevent claiming a new job during the
configuration change; active or ambiguous inference prevents the change.
Recovery preserves protected configuration evidence and never blindly kills
a new inference process. Startup proves renewed authenticated GPOINT polling;
GSALES polling is checked after its server route exists.

Apply consumes the prepared images without rebuilding them, verifies protected
effective Compose configuration, deploys Prisma migrations and AGATA agata0011,
then runs the exact-image GSales checksum-ledger migrator to `001_gsales`.
AGATA agata0011 fixes tenant identity to the authenticated login and removes
runtime DDL ownership. GSales contains six business tables; the private
`gsales_meta.migrations` ledger is included in backup/restore.

The public `/gsales/agata-worker/*` route forwards only the worker protocol to
the authenticated GSales gateway. Final checks prove GSales/Core/Mac readiness,
login tenant isolation, all image/source identities and migration checksums.
The release manifest retains these facts and the effective configuration hash.
Outreach, automatic replies, account management and Telegram listeners stay
disabled. A Mac offline state is DEGRADED; heartbeat is never reported as a
completed Telegram reply. OWNER device smoke remains a separate fact.
