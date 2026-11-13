# GPoint scheduled VPS backups

The Native Release workflow does not install or update the backup runner. A
standalone protected procedure, `scripts/gpoint-backup-install.sh`, installs the
versioned runner and systemd units, enables `gpoint-backup.timer`, and performs
one initial full backup plus isolated restore. It runs from the operator
workstation, requires the exact current main SHA and explicit OWNER approval
(`APPROVE_PRODUCTION=true`), refuses a dirty or mismatched checkout, and creates
no GitHub Deployment. A retry reconciles an already installed release by content
hash; a different payload never overwrites a working version. Regular application releases do not depend on scheduled
backup freshness. They do not reinstall the timer or force a backup run. Backup
freshness and run failures must be monitored independently of application
releases.

The timer runs daily at 03:00 UTC with a persistent catch-up and up to 20 minutes
of scheduling jitter. It backs up the `gpoint` PostgreSQL database and the full
`/opt/gpoint/media` tree. PostgreSQL uses the standard custom archive consumed
by `pg_restore`; media uses a standard tar archive. Both are age-encrypted.
Plaintext database archives, media tar streams, and the media source manifest
exist only in `/run` during the run.

Each routine run verifies the live Kamal API, Proxy, PostgreSQL identity and
migration heads, writable media mount, encryption key identity, then uploads the
encrypted database, media archive, and encrypted source manifest to the existing
private Bunny S3 zone `gpoint-backups`. It downloads the same exact objects,
checks their SHA-256 values, and performs the isolated `pg_restore` and media
restore from those read-back files. It compares restored Prisma/Alembic heads
and the restored media tree to the source manifest recovered from Bunny. A
snapshot manifest and restore receipt are uploaded immutably and read back before
`latest.json` can be updated. Any missing config or failed step exits nonzero and
preserves the previous `latest.json`.

The root-owned mode-0600 `/opt/gpoint/env/gpoint-backup-offsite.conf` contains
only `GPOINT_BACKUP_BUNNY_REMOTE=<rclone-alias>:gpoint-backups`. The configured
alias must exist in the root-owned mode-0600 `/root/.config/rclone/rclone.conf`,
use rclone S3 with `provider=Other`, an HTTPS Bunny Storage S3 endpoint, and the
zone name as access key ID. Credentials remain in that protected rclone file;
they are not written to Git, manifests, or logs. Provisioning must verify that
this is the existing private zone with S3 compatibility already enabled; the
code neither creates a zone nor changes zone privacy or credentials. Bunny
currently documents S3 support as public preview and says S3 compatibility can
only be enabled when a zone is created. If the existing zone is not S3-enabled,
stop without creating a replacement zone.

For the transition, encrypted VPS recovery files and evidence are still retained
locally for the existing 30-day period. This implementation does not change the
schedule or retention and does not remove previous local copies. The later
storage-only change requires a successful Bunny restore and separate approval of
the updated backup policy.

The VPS must have a root-owned mode-0600 age identity at
`/opt/gpoint/env/gpoint-backup.agekey`, plus Docker and the pinned database image.
The private identity is provisioned outside GitHub Actions and is never copied
to the runner. A failed run leaves the prior `latest.json` intact and exits
nonzero; inspect `journalctl -u gpoint-backup.service` before retrying.

**Monitoring gap:** repository inspection found no independent alert for a
failed timer run or a stale `latest.json`. The timer/service records failures in
systemd and the freshness policy can detect stale evidence, but neither sends
an alert. Track independent alerting in [ME-56](https://linear.app/bestuzhev/issue/ME-56/gpoint-independent-scheduled-backup-failure-and-freshness-alert);
do not restore a release-blocking freshness check to the ordinary application
Deploy.

This scheduled Bunny backup remains independent of ordinary application
releases. The first Kamal cutover's separate offsite backup workflow is unchanged
until its own replacement is proven; scheduled backup PASS does not retroactively
replace that historical cutover gate.
