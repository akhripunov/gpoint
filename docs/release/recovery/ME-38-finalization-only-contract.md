# ME-38 Finalization-Only Recovery Contract

Status: `PREPARED_LOCALLY / OWNER_DECISION_REQUIRED`

This contract covers the post-switch incident for candidate
`d367c2ec24242a733dbb2c4145c91d2d4682ba2c`. It does not authorize a production
operation. Any later finalization must use the current canonical GPoint Deploy
executor and the existing release lock.

The recovery code must be checked out from the exact current `main` executor
SHA. The production candidate remains the pinned historical candidate above;
the selector proves that candidate is an ancestor of the executor and loads
the original Prepare run/attempt from the checksum-verified failed-run assets.
The executor SHA and production candidate SHA are recorded separately.

## Confirmed evidence

- Immutable Deploy evidence for run `39010750184`, attempt `1`, records a
  post-switch failure while publishing the production tag.
- The exact candidate and successful Prepare provenance are bound in the
  immutable result and manifest assets. The assets were checksum-verified and
  imported locally.
- The recovery records keep the immutable GitHub Release asset digest
  (`sha256:…`) separate from the SHA-256 of the raw failure-manifest bytes.
  Both are bound to run `39010750184`, attempt `1`, so the reconciliation
  cannot substitute one checksum for the other.
- At the last read-only VPS observation (`2026-10-17T08:24Z`), the runtime
  source SHA and application image identities matched the candidate; API,
  AGATA, GSales and PostgreSQL were healthy. This observation is stale and
  must be repeated before any production write.
- At the last DB read (`2026-10-17T08:26Z`), Prisma, AGATA Alembic and GSales
  migration heads were readable and matched the observed deployed state. This
  observation is stale and must be repeated.
- The last verified ledger read still named baseline
  `a700198a843c12a84f2e537178de752e3eddd54d` as `COMPLETE`, with
  `recoveryRequired=false`. The expected candidate production tag was absent
  at that observation. Both readings must be repeated.
- No runtime Apply, migration, rollback or production ledger update has been
  performed during this recovery investigation.

## Fresh metadata read on 2026-10-17 09:21 UTC

The GitHub connector read `main`, `gpoint-release-ledger`, PR #98, the
deterministic incident tag, and queued/in-progress Actions inventories.
`main` remains the historical candidate
`d367c2ec24242a733dbb2c4145c91d2d4682ba2c`. PR #98 remains open and unmerged
at head `16861d8c99f248a3b0abed5a24da32afb15dd5d0`. The latest GitHub Actions
queries returned zero queued and zero in-progress runs.

The ledger still records production
`a700198a843c12a84f2e537178de752e3eddd54d`, Prisma head
`20261015130000_codex_telegram_challenge`, AGATA head `agata0011`, GSales head
`001_gsales`, `COMPLETE`, `recoveryRequired=false`, Worker `HEALTHY`, and
service readiness `READY`. The deterministic incident production tag is
absent (GitHub API 404).

This is GitHub metadata, not a fresh VPS/runtime/DB/Worker observation. The
canonical `pnpm release:gpoint --dry-run --candidate d367…` could not start
because this execution context has no valid `GITHUB_TOKEN` for the
production-ledger Git read; local `gh auth status` also reports invalid
authentication. No token was read or copied. Therefore runtime health, image
IDs, current migration heads, fresh queue snapshots, release-lock state, and
backup readiness remain unverified and must be re-read through canonical
read-only checks before any later production write.

## Historical facts that remain unproven

- The immutable Deploy result does not include the command output or final
  per-engine migration heads from the failed run. It records a Prisma head
  before the operation and no head-after value.
- The current DB heads and the candidate migration inventory are consistent
  with no candidate migration being pending, but that does not prove that the
  failed run made no database change.
- No candidate-time backup exists. A later backup cannot establish a
  historical pre-Apply backup or replace that evidence.
- No real user or AGATA inference E2E has been recorded for this recovery.

These gaps must remain visible in every recovery result. Do not report “no DB
change” or `E2E_PASS` from health checks alone.

## Owner decisions required before production writes

1. Explicitly accept or reject the unresolved possibility that the failed
   Deploy changed the database, using the immutable failure evidence and the
   latest independently read migration state.
2. Authorize the exact finalization-only operations for candidate
   `d367c2ec24242a733dbb2c4145c91d2d4682ba2c`, baseline
   `a700198a843c12a84f2e537178de752e3eddd54d`, and the original release
   identity. This is limited to current protected backup creation/verification
   if still required, deterministic production-tag reconciliation, append-only
   recovery manifest and ledger state recording, followed by read-only
   production verification. It excludes image delivery, runtime switch,
   migration, rollback and source changes.

No decision is implied by this prepared contract. If the fresh production
baseline or migration state differs from the evidence above, stop and request a
new scope decision.

## Preconditions for the approved operation

1. Re-import and checksum-verify the original immutable Prepare and failed
   Deploy assets. Verify run, attempt, candidate, receipt, asset IDs and
   digests against GitHub.
2. Read the current ledger head and state, exact production ref/tag targets,
   VPS image IDs and source labels, service health, Prisma/Alembic/GSales heads,
   Worker GPOINT and GSALES lane snapshots, and active release lock. Require
   exact expected baseline/runtime parity, fresh timestamps, and no concurrent
   release.
3. Create a current protected backup only if explicitly authorized. Verify
   its path, permissions, nonzero size, checksum, database identity and
   successful `pg_restore --list` integrity check. Record its actual creation
   time and purpose `CURRENT_AFTER_CANDIDATE_DEPLOY`; never label it
   candidate-time.
4. Validate the original task authorization and the specific recovery
   finalization scope. Keep the database uncertainty decision separate and
   explicit. If either is absent, stop before any production write.

## Finalization-only sequence

1. Persist an append-only `RECOVERY_REQUIRED` reconciliation record against
   the exact ledger parent, retaining the old baseline and original failed
   manifest. Do not rewrite the historical `COMPLETE` record.
2. Re-read ledger head, baseline/ref targets, runtime, migration heads,
   Worker state and lock immediately before each external write. Any drift
   stops the sequence.
3. Reconcile the deterministic original production tag
   `gpoint-production-20261017T064101334Z`: if absent, create it for the exact
   candidate; if present at that candidate, treat the tag step as complete; if
   present elsewhere or unreadable, fail closed. Never force-update it.
4. Append one deterministic recovery manifest and the `COMPLETE` state in the
   same compare-and-swap ledger commit, retaining every prior manifest. The
   final manifest is never published separately from its matching ledger
   state. If a finalization manifest exists remotely while state is still
   `RECOVERY_REQUIRED`, stop as ambiguous; do not overwrite or adopt it.
5. Compare-and-swap the ledger from the exact parent SHA. Read the remote
   state and every manifest back; require the new head to be exactly the
   commit just published.
6. Read back the tag and ledger, then repeat runtime, health, database-head,
   image and Worker checks. No Apply, database migration, runtime switch or
   rollback is in this procedure.

## Partial failures and resume

- Before a recovery-state ledger append: no production metadata is changed.
- After the append but before the tag: resume from the matching durable
  `RECOVERY_REQUIRED` record after a fresh read.
- After the exact tag but before the manifest: read the tag; reuse it only if
  its target is exactly the candidate, then append the deterministic manifest.
- If ledger push/readback times out, re-read the remote ledger before deciding
  whether the atomic manifest+state commit landed. Resume only from the exact
  remote state; never infer success from local files.
- If the tag target, manifest contents, ledger parent, runtime, migrations,
  authorization, backup, worker evidence or production baseline is unknown or
  conflicting, stop. Never repeat an Apply or rewrite an existing ref.
- A ledger publication timeout is ambiguous until the remote ledger head is
  re-read. Do not retry based only on a local status.

## Success states

`COMPLETE` requires exact production tag, candidate runtime and image parity,
verified current DB heads, append-only recovery manifest, compare-and-swap
ledger state, healthy required services, and fresh Worker evidence. This is a
technical release result only. `E2E_PASS` additionally requires a separately
recorded real end-to-end conversation result; health or queue snapshots do not
count as inference.

## One future OWNER request

No production operation is authorized by this local contract. After fresh
read-only evidence is available, the single request must ask OWNER to authorize
one canonical `GPoint — Deploy` dispatch from current `main` with:

- `mode=recovery-finalization`, candidate
  `d367c2ec24242a733dbb2c4145c91d2d4682ba2c`, and
  `approve_production=true`;
- original failed Deploy run `39010750184`, attempt `1`, and the exact
  successful Prepare run/attempt recovered from its immutable evidence;
- the exact then-current production baseline and an explicit decision accepting
  or rejecting the unresolved historical database outcome;
- explicit permission to create and verify a current protected backup before
  metadata writes, reconcile only the deterministic production tag, append
  the recovery manifest and ledger state, and perform read-only verification;
- explicit exclusions: Docker delivery, runtime Apply/switch/restart, database
  migration or rollback, changes to Worker/configuration, and any unrelated
  production operation.

The dispatch must be made by GitHub actor `akhripunov`; mismatched actor, SHA,
baseline, risk decision, original evidence, current-main executor, fresh
readbacks, worker lanes, lock, tag or ledger state fails closed. This is not
authorization to send an AGATA message; a real conversation smoke remains
unverified until the OWNER completes it in their approved session.
