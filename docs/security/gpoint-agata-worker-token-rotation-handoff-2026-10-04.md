# GPOINT AGATA worker-token rotation handoff

**Status:** completed for the exact production SHA below on 2026-10-04; original V2 restored and authenticated polling verified.

This handoff is for a separately approved, exact-release maintenance operation.
It records no token value, fragment, hash, or other derived fingerprint.

## Verified target identity

- Current production release SHA: `68bf0212d7aa5125394b0b39ffba9226eb106c0d`.
- Current GPOINT API image: `sha256:940236e984cff5e0123af62c58cea6ae5986fe879ca219daa2c91ff6ca8620e2`.
- Current AGATA API image: `sha256:61299c506ed77971df0f75cb64cb91f739644ebbddfafc21d5cb903c4488dbb2`.
- Gateway: `https://gpoint.amiora.one/api/agata-worker`.
- Mac client: installed LaunchAgent `one.gpoint.agata-codex-worker`.
- The same worker credential is configured on the Mac client and validated by the VPS GPOINT API gateway. Rotation must coordinate both ends; changing only one side breaks polling/authentication.

The release and image identities above were verified against the running services
and the authoritative `gpoint-release-ledger` production manifest. The token
exposure itself is described in
`gpoint-agata-worker-token-incident-2026-10-04.md`.

## Exact-SHA handoff checklist

1. Before any change, re-verify that production is still at the exact SHA and
   image IDs above. If any identity differs, stop and prepare a new handoff for
   the actual release.
2. Obtain a distinct OWNER approval naming this exact SHA and authorizing
   coordinated Mac/VPS worker-token rotation. This document is not approval.
3. Prepare a fresh credential through the approved secret-management path. Use
   secret-safe file or secret-manager operations; never put the value in command
   arguments, shell history, Terminal output, Git, Linear, issue text, logs, or
   reports.
4. Perform a fresh authoritative read-only queue/TTL check and verify the Mac
   LaunchAgent is loaded, idle, and polling. Abort unless pending and running
   counts are zero and the effective TTL exceeds the bounded maintenance window
   with safety margin.
5. In the approved maintenance window, stop only the existing GPOINT Mac
   LaunchAgent, update the Mac worker secret and VPS gateway secret through their
   approved secret stores, and recreate only the GPOINT API service that reads
   the gateway credential. Do not modify the worker source, plist, other
   services, database, or release state.
6. Restore the existing LaunchAgent and verify gateway health authentication,
   fresh successful polling, no new auth/network errors, and unchanged source,
   plist, image and release identities. Keep all sending flags off; do not
   fabricate production queue work.
7. If either side fails, keep the worker stopped while completing repair with
   the new credential. Never restore the exposed credential. Record only
   non-secret completion facts and timestamps.

## Completion record

The OWNER approved one coordinated rotation on production SHA
`68bf0212d7aa5125394b0b39ffba9226eb106c0d`. The V2 LaunchAgent was already
unloaded from the earlier failed attempt, so successful polling was not treated
as a prerequisite to this repair. The controller verified it remained
unloaded, the worker lock was free, the installed worker/wrapper/plist hashes
matched the expected V2 artifacts, and the checkout was clean.

The fresh authoritative queue/runtime snapshot at
`2026-10-04T09:40:59.358231Z` identified the expected GPOINT tenant, production
SHA and immutable API/Core images; pending and running jobs were both zero and
effective inference TTL was 420 seconds. A second queue snapshot was taken
immediately before the secret update. No production jobs were added, removed,
or altered.

The credential was transferred through temporary mode-0600 files and replaced
on both the Mac worker config and VPS gateway config. The secret itself and any
derived value are intentionally absent from this handoff. Only the GPOINT API
service was recreated; there was no application deploy, migration, V3 install,
or worker/plist edit. The previously exposed credential was not used as
rollback.

The original V2 LaunchAgent was bootstrapped. Authenticated idle polling was
confirmed at `2026-10-04T09:41:33.616551Z` and again at
`2026-10-04T09:41:58.504466Z`, with no error code, no active job, and no change
to worker, wrapper, or plist hashes. A fresh read-only runtime/queue check at
`2026-10-04T09:42:02.989490Z` again confirmed the expected production SHA and
image IDs, zero pending/running jobs, and 420-second TTL.

The observed recovery interval was **24 minutes 15 seconds**, so the prior
300-second objective was missed. The exact bootout timestamp was not captured;
this duration is the recorded poll-to-poll recovery gap. This operation does
not authorize Real Codex QA. Before a later QA window, the detached recovery
watchdog must pass isolated tests and the changed candidate SHA must receive a
new exact-SHA OWNER approval. A complete loss of Mac availability remains
outside the protection a local watchdog can provide.

## Authorized Real Codex QA attempt — 2026-10-04

The OWNER subsequently approved candidate
`5be608d33cddca201b2cb9ce104652f74b887fee`. A fresh authoritative preflight at
`2026-10-04T10:42:06.909873Z` verified the production SHA and immutable image
IDs, expected GPOINT tenant, pending/running counts 0/0, and effective TTL
420 seconds. The controller repeated preflight before switch, started its
watchdog, and temporarily unloaded V2.

The disposable E2E harness then failed before claim or inference because its
temporary status-file parent directory had not been created. Neither Codex
Local generation ran and no production queue item was touched. The controller
restored V2 and confirmed a fresh authenticated idle poll, unchanged original
V2 artifact hashes, and no error code. The watchdog session's conservative
start-to-confirmed-restore interval was 10.3 seconds. The disposable database
container was removed.

The missing test-directory setup was fixed in the QA test only; production
worker, GSales runtime and installed V2 were not changed. That changes the
candidate SHA, so the approval above no longer authorizes another worker stop.
A new exact-SHA OWNER approval is required before retrying Real Codex QA.
