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

Before implementation, create a durable `.release-authorizations/<id>.json`
record containing project, `TASK_SCOPED_AUTO`, unique ID, original owner
instruction reference, production baseline, allowed scope/paths, acceptance
criteria, exclusions/owner-only categories, and ACTIVE/revoked/consumed state.
Commit and push it before implementation commits. Preserve the record blob.
Controller proves its commit precedes all implementation commits, supporting
normal merge commits and rejecting squash-merging the authorization together
with the implementation. Before apply, compare the full baseline-to-candidate
diff. Unrelated unreleased changes invalidate authorization.

After verifying exact eligible current SHA, unchanged baseline, full-scope
diff, active/unconsumed authorization, successful Prepare, immutable
provenance, migration/risk gates and read-only preflight, the controller passes
the existing exact-SHA approval input. No second owner confirmation is needed.
The authorization is consumed after successful runtime completion using a
durable one-shot marker. A lost marker-write response is reconciled by exact
readback and never by repeating runtime apply.

Eligibility is limited to bounded low-risk changes. Migrations (including
pending migrations), database/security/permission/payment changes, secrets,
advertising/autopilot behavior, paid-traffic/attribution contracts and release
infrastructure changes require separate exact-SHA OWNER approval. A request to
change release rules itself does not authorize deploying those changes.
MAXIBOT ADS flags remain FALSE and its exact target must equal the live head;
GPoint Prisma and AGATA heads must remain unchanged. Unknown risk blocks apply.

All existing clean-checkout, exact current-main, Prepare, immutable artifact,
platform/parity, config, preflight, lock, recovery, server health, Worker compatibility and regression
gates remain mandatory. Worker operational availability is separately recorded
as dependency readiness under docs/release/automation.md and does not change
a proven server deployment outcome. Real-device/OWNER smoke requirements remain separate:
never invent evidence or claim an unperformed end-to-end acceptance.

Scope expansion, any failed required check, unexpected main/baseline drift,
new migration, ambiguous apply outcome or RECOVERY_REQUIRED stops automatic
release. Do not auto-fix a failed gate and deploy under this authorization;
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
