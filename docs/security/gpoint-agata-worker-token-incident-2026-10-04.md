# GPOINT AGATA worker token exposure and rotation

**Status:** exposure recorded; credential rotation is prepared but not performed.

On 2026-10-04, a GPOINT AGATA worker bearer token was accidentally printed in a local diagnostic session while inspecting the Mac worker configuration. The token value is intentionally omitted from this record and must not be copied into Git, Linear, chat, logs, or a release report. No token hash or partial value is recorded either.

## Scope

The affected credential is the Mac worker's `GPOINT_AGATA_WORKER_TOKEN`. The installed `one.gpoint.agata-codex-worker` uses it to authenticate to the GPOINT AGATA worker gateway at `https://gpoint.amiora.one/api/agata-worker`. The gateway compares it with the corresponding VPS production environment value and authorizes worker health, claim, complete, and fail requests. A holder could potentially claim available inference work and read its payload through the worker API.

The exposure does not include the G Sales worker credential, database credentials, VPS SSH credentials, Codex/LLM API keys, or application encryption/HMAC keys. No credential was changed during discovery. The installed LaunchAgent and VPS runtime were not stopped or modified.

## Rotation plan — separate OWNER approval required

The gateway accepts one bearer value at a time, so rotation requires a coordinated maintenance window and briefly interrupts worker polling. Do not start this procedure until the OWNER separately approves production credential rotation.

1. Before the window, prepare a fresh high-entropy token through the approved secret-management path, prepare secret-safe updates for the Mac config and VPS environment, and rehearse the exact commands against disposable files. Never place the token in command arguments, shell history, terminal output, Git, Linear, or this record.
2. Immediately before the change, take a fresh read-only GPOINT queue/TTL snapshot and confirm the installed Mac worker is loaded, idle, and polling. Abort unless the queue is empty and the effective TTL safely exceeds the full bounded outage window.
3. Stop the existing LaunchAgent only after the preflight passes. Update the VPS environment and recreate only the GPOINT API gateway service that reads the worker token; update the Mac worker config while the agent remains stopped. Do not alter the worker source or plist.
4. Bootstrap the existing LaunchAgent and verify gateway health authentication, a fresh successful polling timestamp, and no new network/authentication errors. Keep all sending flags off; do not enqueue a synthetic production job.
5. If the coordinated update or verification fails, keep the worker stopped and repair both sides using the new credential. Do not restore the exposed credential. Remove temporary secret material using the approved secure handling process and record only completion facts, timestamps, and non-secret artifact identities.

The API service recreation causes a brief GPOINT API availability window and therefore must be scheduled and approved separately. This document records the procedure only; it does not authorize or perform rotation.
