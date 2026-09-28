# GPoint VPS SSH transport

Canonical GPoint release runners use the dedicated Ed25519 identity configured
in `~/.config/gpoint/release-tools.env` as `GPOINT_SSH_IDENTITY_FILE`.
The durable local runner paths are `~/.local/bin/gpoint-vps-runner` and
`~/.local/bin/gpoint-vps-scp-runner`; release tooling discovers those paths
from the same config file in a fresh process.

The runners connect only to `root@38.180.110.92` using OpenSSH with
`IdentitiesOnly=yes`, `StrictHostKeyChecking=yes`, and `BatchMode=yes`. Missing
or unreadable identity fails before network access. There is no automatic
password or Expect fallback. The previous password-based bootstrap remains
outside canonical GPoint release tooling as a temporary manual break-glass
path and is not removed because it may serve other projects.

Each connection uses `ConnectionAttempts=1` and `ConnectTimeout=20`. SSH remote
commands have a hard 600-second default wall-clock deadline (configurable only
within 1–1800 seconds); SCP has a hard 60-second maximum. The release adapter
may make at most one controlled SCP retry, so an upload operation is bounded
to two attempts. Read-only probes may set a shorter remote deadline.

Private key material and production credentials must never be stored in Git.
