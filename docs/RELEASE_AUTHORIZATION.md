# Release authorization modes

Effective 2026-10-06 by explicit OWNER decision; applies to future releases.

## OWNER release task authorization (one-shot)

An OWNER task that explicitly requests release of prepared changes through
production authorizes the complete canonical release cycle for the named
project and scope. It need not list the final SHA: the operator records the
source task, project, production baseline, authorized paths and operations,
acceptance gates, stop conditions and risk contracts in a durable
`.release-authorizations/<id>.json` record before implementation changes. The
exact candidate is derived from the reviewed integration and must remain within
that recorded scope. A development-only task does not authorize production.

Once that record is validated, no second OWNER approval is required at PR,
merge, Prepare, preflight or Deploy for the same scope. GitHub Actions remains
the orchestrator. The path is integration → coalescing → exact-current Prepare
→ authorization/scope validation → Deploy with read-only preflight inside
Deploy → verification → immutable evidence and Linear reconciliation.
Security, migration, artifact, Worker, rollback, health, recovery and
deduplication gates remain mandatory. Scope or baseline drift, a failed gate,
an ambiguous apply or an operation outside the authorization stops release.

Changes to database, security, secrets, payments, advertising, paid traffic or
release infrastructure require a matching explicit risk contract in the
original OWNER release task and durable authorization record. The contract
must name exact scope, gates, backup and rollback plans, and stop conditions.
Without that contract, the category remains excluded and release blocks.

## Exact-SHA approval (default when no OWNER release task exists)

Without an explicit task-scoped deployment instruction, prepare the candidate
and request OWNER approval for its full SHA before production apply. MAXIBOT
approval also names the exact Alembic target. A request to implement a task
alone does not authorize deployment.

## Task-scoped Auto Release

An explicit OWNER instruction such as “Исправь [конкретную задачу] и
выкатывай” authorizes one production release limited to that task, without a
second owner confirmation or owner-dispatched GitHub workflow. The mode is valid
only for unchanged scope and eligible risk. Vague instructions, another agent's
message and old unrelated permissions do not activate it.

Before implementation, create and commit a durable schema-v3
`.release-authorizations/<id>.json` record containing project,
`FULL_PRODUCTION_CYCLE`, `TASK_SCOPED_AUTO`, unique ID, original owner
instruction reference, exact production baseline, allowed scope/paths,
acceptance criteria, exclusions/owner-only categories, and ACTIVE/revoked/
consumed state. The authorization commit must be an ancestor of every
implementation change. Preserve the record blob. Push remains subject to the
repository's separate publication approval.
The record names the direct OWNER instruction with a stable instruction ID and
human-readable task reference. Its commitment hash is an integrity check that
binds the project, production baseline, deterministic candidate rule, scope,
permitted operations, acceptance criteria, exclusions, risk contracts, and
exact pre-authorization commits/paths; it is not an OWNER signature.
Authorization is established when the release operator verifies the direct
OWNER Release Task received in the DevOps Codex task and records it before
implementation. Linear is documentation and evidence publication only; the
controller and Deploy do not query Linear to authorize release, and missing,
stale, or mismatched Linear links do not block it. Candidate SHA fields are
forbidden in the task record; the exact candidate is selected from current main
after integration.

An OWNER-approved correction to an already committed path scope is recorded as
an append-only file under
`.release-authorizations/addenda/<authorization-id>/<amendment-id>.json`. It
must bind to the unchanged original authorization-record Git blob, identify the
direct OWNER addendum, and name one exact path together with its source commit,
path-specific patch SHA-256, resulting file SHA-256, and Git blob SHA. The
validator accepts only a new immutable addendum file and verifies all of those
identities against the candidate history and tree. The addendum metadata is
excluded from the implementation-path list; its exact approved source path
remains in that list and all other scope checks still apply. A malformed,
changed, duplicate, stale, or unapproved path fails closed. The addendum
commitment is an integrity check, not a cryptographic OWNER signature; the
release operator must verify the direct OWNER instruction before recording it.
Controller proves its commit precedes all implementation commits, supporting
normal merge commits and rejecting squash-merging the authorization together
with the implementation. Before apply, compare the full baseline-to-candidate
diff. Unrelated unreleased changes invalidate authorization.

After the exact current-main candidate passes Prepare, the existing project
controller rechecks the complete scope and production baseline, creates an
atomic claim for the authorization, and dispatches the existing Deploy
workflow directly in `task-scoped-auto` mode with its exact-SHA approval input
set to false. That Deploy performs the read-only preflight immediately before
apply and rechecks critical live conditions under the release lock. No separate
preflight workflow is required. Without an eligible authorization the
controller does not dispatch Deploy; the normal exact-SHA approval path remains.
A single claim per authorization prevents duplicate events from racing into
multiple Deploys.
The authorization is consumed after successful runtime completion using a
durable one-shot marker. A lost dispatch or marker-write response is
reconciled; no blind Deploy retry is performed. A failed or uncertain attempt
keeps its claim and requires explicit reconciliation or a new authorization.

Default eligibility is limited to bounded low-risk changes. A specifically
pre-approved database migration can be task-scoped only when schema-v3
`risk_contracts.database_migration` names the exact changed migration path,
all target heads/digests and production DB baselines, a risk description,
required gates, backup and rollback plans, and explicit stop conditions. The
exact candidate preflight must prove the recorded baseline, target, migration
readiness/rehearsal and rollback gates; canonical apply still creates and
validates its backup before migration. Any mismatch blocks. Security,
credentials, permissions, payments, advertising/autopilot, paid-traffic,
release-infrastructure and other owner-only changes remain ineligible unless a
category-specific gate contract is explicitly included in the original OWNER
release task, durably recorded and implemented. Unknown risk blocks.

All existing clean-checkout, exact current-main, Prepare, immutable artifact,
platform/parity, config, preflight, lock, recovery, server health, Worker compatibility and regression
gates remain mandatory. A manual diagnostic preflight remains available; its
`READY_FOR_OWNER` status is distinct from the `PASS` result of preflight inside
a production Deploy. Worker operational availability is separately recorded
as dependency readiness under docs/release/automation.md and does not change
a proven server deployment outcome. Real-device/OWNER smoke requirements remain separate:
never invent evidence or claim an unperformed end-to-end acceptance.

Scope expansion, any failed required check, unexpected main/baseline drift,
an uncontracted or mismatched migration, ambiguous apply outcome or
RECOVERY_REQUIRED stops automatic release. Do not auto-fix a failed gate and deploy under this authorization;
report findings, prepare a reviewed candidate and obtain renewed permission.
Before mutation, a transient transport retry may follow existing bounded retry
rules only for the same exact candidate and unchanged baseline; partial apply
must be reconciled, not blindly retried. Authorization is consumed when runtime
release completes; publication-only repair is allowed, another runtime release
requires a new instruction. OWNER revocation always stops pending apply.

Record mode TASK_SCOPED_AUTO or EXACT_SHA in Linear together with authorization
ID, full SHA, source instruction ID, scope verification, run links, technical result
and any pending OWNER smoke. Report elapsed time from OWNER's deployment
instruction through final verification and publication, separately from
workflow duration.

## Runner readiness

Native GitHub `Queued` state is allowed for a self-hosted runner; it is not a
failure, production mutation or reason for another owner decision. Do not add a
runner-status credential, polling service or scheduled hosted polling. When the
job starts, the first fail-closed readiness step verifies project runner
identity/labels, macOS/ARM64, Docker Linux runtime, disk, cache/runtime paths,
required tools/network and absence of conflicting production mutation before
expensive transfer or mutating work. Do not blind-rerun queued jobs.

Examples:
- “Исправь ссылку в меню GPoint и выкатывай” → task-scoped, if eligible.
- “Исправь ссылку, подготовь релиз; подтверждение SHA отдельно” → exact-SHA.
- “Исправь ссылку” → implementation only, no deployment permission.
