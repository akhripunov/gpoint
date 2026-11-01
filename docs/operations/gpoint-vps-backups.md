# GPoint scheduled VPS backups

The Native Release workflow does not install or update the backup runner. A
separate `backup_maintenance` dispatch installs the versioned runner and systemd
units, enables `gpoint-backup.timer`, and performs one initial full backup plus
isolated restore. It requires the exact current main SHA and explicit OWNER
approval, uses the existing protected production environment, and creates no
GitHub Deployment. Regular application releases only read the timer state and
the freshness/integrity/restore evidence for the existing backup; they do not
reinstall the timer or force a backup run.

The timer runs daily at 03:00 UTC with a persistent catch-up and up to 20 minutes
of scheduling jitter. It backs up the `gpoint` PostgreSQL database and the full
`/opt/gpoint/media` tree. Both artifacts are age-encrypted before they are
written under `/opt/gpoint/backups/scheduled`; plaintext database archives and
media tar streams exist only in `/run` during the run. Encrypted artifacts and
run evidence are retained locally for 30 days.

Each run verifies the live Kamal API, Proxy, PostgreSQL identity and migration
heads, writable media mount, encryption key identity, SHA-256 of both encrypted
files, a network-isolated restore using the same PostgreSQL image, restored
Prisma/Alembic heads, and a restored media tree against its source manifest.
`latest.json` is written atomically only after all checks pass. The freshness
gate re-reads the files and restore evidence and rejects missing, altered, unsafe
or older-than-26-hour results.

The VPS must have a root-owned mode-0600 age identity at
`/opt/gpoint/env/gpoint-backup.agekey`, plus Docker and the pinned database image.
The private identity is provisioned outside GitHub Actions and is never copied
to the runner. A failed run leaves the prior `latest.json` intact and exits
nonzero; inspect `journalctl -u gpoint-backup.service` before retrying.

These scheduled copies are VPS-local recovery copies. They do not satisfy or
replace the separately required offsite backup/read-back/isolated-restore gate
in the first Kamal cutover workflow. That gate remains unchanged. Changing the
offsite policy requires an explicit OWNER amendment to the canonical GPoint
release rules and cutover runbook.
