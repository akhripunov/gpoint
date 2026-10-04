# GPOINT AGATA worker-token rotation handoff

**Status:** prepared; no credential rotation performed.

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

## Current decision

**NO-GO for rotation now.** There is no separate exact-SHA OWNER authorization
for credential rotation in this task. Production was not changed. The
identity-corrected read-only queue snapshot supports a later Queue Preflight,
but does not authorize this credential maintenance or Real Codex QA.
