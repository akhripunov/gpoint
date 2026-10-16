# Release authorization modes

Effective 2026-10-06 by explicit OWNER decision; applies to future releases.

## Exact-SHA approval (default)

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
Controller proves its commit precedes all implementation commits, supporting
normal merge commits and rejecting squash-merging the authorization together
with the implementation. Before apply, compare the full baseline-to-candidate
diff. Unrelated unreleased changes invalidate authorization.

After the exact current-main candidate passes Prepare and its immutable
read-only preflight receipt is `READY_FOR_OWNER`, the existing project
controller rechecks the complete scope and production baseline, creates an
atomic claim for the authorization, and dispatches the existing Deploy
workflow in `task-scoped-auto` mode with its explicit SHA approval input set
to false. It does not create another Prepare or preflight. A single claim per
authorization prevents different events from racing into duplicate Deploys.
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
release-infrastructure and other owner-only changes remain ineligible until a
category-specific gate contract is implemented and reviewed. Release-infra
changes always require separate exact-SHA OWNER approval. Unknown risk blocks.

All existing clean-checkout, exact current-main, Prepare, immutable artifact,
platform/parity, config, preflight, lock, recovery, server health, Worker compatibility and regression
gates remain mandatory. Worker operational availability is separately recorded
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
ID, full SHA, source reference, scope verification, run links, technical result
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
