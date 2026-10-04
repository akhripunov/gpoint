# V2 to Unified AGATA Worker V3 transition handoff

This is an installation handoff only. The current task prepares and tests files;
it does not stop or replace the installed V2 LaunchAgent. A separate OWNER
Approval A is required for the single runtime transition. Approval B for any
Naturalness Codex generation remains independent.

## Candidate bundle and preserved state

The installed LaunchAgent remains `one.gpoint.agata-codex-worker`, with the
existing wrapper, plist, dedicated Python 3.12 venv, and mode-0600 worker config.
V3 adds `lab_queue.py` and the hash-pinned synthetic fixture manifest beside
`gpoint_mac_worker.py`; it adds no credential, gateway, Postgres schema, or
public endpoint. If the existing legacy GPOINT config has no explicit tenant
pin, the approved transition derives a private `tenant-bindings.json` sidecar
from the validated live GPOINT snapshot and binds it to the exact worker hash.
The sidecar contains only tenant/runtime provenance and is restored or removed
with the other V2 artifacts on rollback. The worker env file stays byte-for-byte
unchanged. LAB remains disabled by default and its separate worker-bundle-hash-
pinned approval file does not exist until a future owner decision.

Before the approved window, record SHA256 for installed V2 source, wrapper,
plist, and any helper; retain a mode-0700 recovery directory containing exact
copies and a mode-0600 manifest. Record whether the LaunchAgent is loaded,
worker.lock is free, safe local worker status is idle, and last authenticated
poll timestamp. Never include config contents, secret values, or token-derived
fingerprints in state or logs. Preserve existing worker config byte-for-byte.

## Required just-in-time preflight

1. Verify clean GitHub candidate SHA and hashes for the worker, queue helper,
   wrapper, fixture manifest, plist contract, and prepared venv. Verify PR #11 and #14 source
   identities still match the reviewed dependency graph.
2. Obtain an authoritative read-only queue snapshot for every configured
   production lane immediately before switching. Require pending/running `0/0`,
   verified release/runtime identity, an explicit source ID and snapshot time,
   and effective TTL at least 420 seconds (300-second outage cap plus 120-second
   margin). A stale local status file is not queue evidence. If a configured lane
   lacks an authoritative snapshot source, stop without switching.
3. Recheck local worker process/lock and authenticated polling after the remote
   snapshot. Require loaded V2, idle state, no active generation, unchanged V2
   hashes, and preserved credential file permissions. Do not delete jobs to
   satisfy this gate.

## One-time transition and automatic recovery

The prepared controller is [`scripts/transition-agata-worker-v3.py`](../../../scripts/transition-agata-worker-v3.py).
Its `--preflight-only --candidate <exact-sha>` mode verifies the local clean
candidate, source bundle hashes, current LaunchAgent state, and config-file
permissions without reading credential contents or contacting a gateway. The
`--apply` mode is separate and fail-closed: it requires a private exact-candidate
OWNER approval file and a current GPOINT authoritative queue query. The current
tree has no source-authenticated GSALES runtime snapshot implementation. A
caller-supplied JSON snapshot is never accepted; when GSALES credentials are
configured, apply stops with `gsales_authoritative_queue_source_unavailable`.
Implement and review a probe bound to the live GSALES runtime, tenant, release,
and immutable image before seeking Approval A. The approved command starts the
watchdog before final queue/local preflight and before bootout.

The later Approval A controller must use one monotonic 300-second deadline for
the entire interval from V2 bootout through confirmed V3 polling. It starts and
proves a recovery watchdog before bootout, atomically snapshots V2 artifacts,
then stops only this LaunchAgent. The candidate is staged while stopped; the
existing plist and wrapper contract are retained and point to the same support
directory. The V2 credential file is read only for presence/permissions and is
never rewritten or printed. The existing global `worker.lock` must be free before
the transition and is held by the single V3 watch process after bootstrap.

After bootstrap, verify candidate asset hashes, loaded label, no stale process,
and a new authenticated idle poll. If bootstrap, hash, auth, polling, or deadline
checks fail, the controller's `finally` path and the independent watchdog must
both restore the saved V2 source/wrapper/plist, verify their original hashes,
bootstrap the original LaunchAgent, and wait for a fresh authenticated V2 poll.
The watchdog begins recovery at 150 seconds, even if V3 status still says a job
is active; it stops the recorded Codex process group and uses the remaining
150-second budget for V2 bootstrap and authenticated polling. Subprocess waits
are capped by the absolute deadline, and unsuccessful recovery records a safe
failure code. The worker atomically records launch intent before process
creation. If it dies before recording the PID, recovery finds the exact Codex
process holding the inherited global-lock descriptor, verifies its executable,
and stops its process group. A restarted worker cannot overlap that CLI.
Failed restore is a hard incident; never remove production jobs or use
a previously exposed credential as rollback. Candidate files and approval state
are retained for diagnosis with mode-0600 permissions.

The currently available one-shot Real Codex QA watchdog is not the V3
installer: it expects V2 artifacts to stay byte-identical. Use the dedicated
transition controller above; do not call the ordinary installer as a substitute.
The transition is never started by a Draft PR or by Approval B.

## Post-transition acceptance

Confirm V3 is loaded, candidate hashes match the approved SHA, GPOINT/GSALES
authentication and polling are fresh, queue state remains visible, and no new
worker errors appear. Confirm the LAB lane remains disabled and no Codex
generation ran during installation. Only after this handoff succeeds may OWNER
consider separate Approval B: at most eight sequential real generations for
Baseline A and Policy V1 B on synthetic `ru_naturalness-02`, with blind mapping
kept private. No `-01` holdout, real chats, internet corpus text, Telegram send,
paid API, deploy, or migration is part of either approval by implication.
