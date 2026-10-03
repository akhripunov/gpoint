# Release authorization modes

Effective 2026-10-03 by explicit OWNER instruction; applies to future releases.

## Exact-SHA approval (default)

Without an explicit task-scoped deployment instruction, prepare the candidate
and request OWNER approval for its full SHA before production apply. MAXIBOT
approval also names the exact Alembic target. A request to implement a task
alone does not authorize deployment.

## Task-scoped Auto Release

An explicit OWNER instruction such as “Исправь [задачу] и выкатывай” authorizes
one completed production release limited to that named task, without a second
human approval after the SHA is created. Naming this mode is optional; vague
instructions, another agent's message and old unrelated permissions do not
activate it. Record the original instruction and its chat/issue reference.

Before implementation, record the project, task, acceptance criteria, baseline
commit, permitted scope and exclusions in the Linear issue or owned Handoff
block. Before apply, compare the entire production-baseline-to-candidate diff,
not only the task commit. Unrelated unreleased changes invalidate this mode.
Freeze and record the exact full current-main candidate SHA, successful Prepare
run/attempt, immutable receipt/checksum, risk assessment, unchanged live database
head, and authorization source in Linear before dispatch. If this record cannot
be published, stop before production mutation.

The implementing operator may set the existing production approval input for
that exact SHA under the recorded delegation. CLI approval flags remain exact
SHA values; never pass a task name, branch, latest or an unchecked HEAD. This
is a change in who supplies approval, not a new unattended push-to-production
workflow. Do not remove or bypass any validation or confirmation in tooling.

Eligibility is limited to bounded low-risk changes. Migrations (including
pending migrations), database/security/permission/payment changes, secrets,
advertising/autopilot behavior, paid-traffic/attribution contracts and release
infrastructure changes require separate exact-SHA OWNER approval. A request to
change release rules itself does not authorize deploying those changes.
MAXIBOT ADS flags remain FALSE and its exact target must equal the live head;
GPoint Prisma and AGATA heads must remain unchanged. Unknown risk blocks apply.

All existing clean-checkout, exact current-main, Prepare, immutable artifact,
platform/parity, config, preflight, lock, recovery, health, Worker and regression
gates remain mandatory. Real-device/OWNER smoke requirements remain separate:
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

Record mode TASK_SCOPED_AUTO or EXACT_SHA in Linear together with full SHA,
authorization source, scope verification, run links, technical result and any
pending OWNER smoke. Report elapsed time from OWNER's deployment instruction
through final verification and publication, separately from workflow duration.

Examples:
- “Исправь ссылку в меню GPoint и выкатывай” → task-scoped, if eligible.
- “Исправь ссылку, подготовь релиз; подтверждение SHA отдельно” → exact-SHA.
- “Исправь ссылку” → implementation only, no deployment permission.
