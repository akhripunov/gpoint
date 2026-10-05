# GPoint release rules

## Source of truth

- GitHub `origin/main` is the only durable source of application source.
- Production changes flow `Mac/repository → exact Git SHA → GitHub → immutable artifacts → VPS`.
- Never copy source edits from the VPS back into GitHub.
- Never release a dirty checkout, a branch name, `HEAD` without SHA verification, `latest`, or an unverified mutable image tag.
- Production secrets and environment files stay outside Git.

## Release gates

Every release must record the current production SHA and database migration head before mutation, then verify the exact candidate SHA on GitHub, build from an isolated clean checkout, prove API/Worker/Bot/Mini App artifact parity, render effective production configuration, create a GPoint-only backup when migrations are pending, migrate with `prisma migrate deploy`, switch runtime, run health/smoke checks, and persist a complete manifest.

The canonical entry point is `pnpm release:gpoint --dry-run --candidate <40-char-sha>`. Apply mode is deliberately fail-closed and requires an explicit operator confirmation. A release lock covers migration, runtime apply, and production-ref finalization; a stale lock is never silently removed.

## Release authorization modes

Use `docs/RELEASE_AUTHORIZATION.md` for the two OWNER authorization modes.
Exact-SHA approval is the default. An explicit OWNER instruction to implement
and deploy a named low-risk task authorizes one task-scoped release without a
second SHA approval, only after the operator records and verifies the exact
candidate, full unreleased diff and authorization in Linear. All technical
release gates and production confirmation inputs remain mandatory. High-risk
changes, failed gates and scope expansion stop this mode. This section governs
approval semantics; the existing canonical tooling and invariants still apply.

## Automated release control

- GitHub workflows `GPoint — Prepare` and `GPoint — Deploy` implement the release flow described in `docs/release/automation.md`.
- Prepare must succeed for the exact current main SHA, including publication of release facts to Linear. Deploy is manually dispatched from main with exact SHA: preflight is read-only; production mode requires explicit production approval. The shared Mac lock lives outside checkout at `~/.config/gpoint/release-production.lock`; legacy checkout locks also fail closed.
- The production runner is repository-scoped, Mac-only and labelled `gpoint-production`; never assign it untrusted PR jobs. VPS secrets stay on the Mac/VPS.
- CI artifacts are consumed by `GPOINT_RELEASE_BUNDLE`; verify successful GitHub run provenance, receipt/run ID, archive checksum and exact image IDs. Do not rebuild those application images during apply.
- Compare the installed Mac worker source to the candidate before apply. A changed worker requires installation from the published candidate before resuming.
- Technical COMPLETE and owner-confirmed Telegram replies are separate facts. Never create synthetic production Messages jobs or report heartbeat as an end-to-end reply.
- Linear publication preserves architect-owned Handoff sections and existing journal history. Missing publication or an offline runner is a visible blocker, not a successful automation setup.

## Release operations interface

- For any GPoint or MaxiBot release operation, do not use Computer Use, Chrome, mouse interaction, or GitHub Web UI to dispatch, approve, rerun, cancel, or coordinate workflows.
- Dispatch GitHub Actions only through the authorized `gh workflow run` command or GitHub Actions API, with the exact workflow, SHA, and required inputs.
- Check workflow status and logs only through GitHub MCP, GitHub API, or `gh run view/watch`.
- Before dispatching, check whether the required workflow is already running. Never create a duplicate production Deploy, and do not cancel or rerun an active release unless necessary.
- If CLI/API access, authorization, or the runner is unavailable, stop and report the concrete blocker. Do not fall back to browser UI or another unverified launch path.

## VPS policy

The VPS is a runtime and artifact target, not a development workspace. Application fixes must be committed and pushed before deployment. MaxiBot files, containers, database, domain, and release state are outside GPoint scope and must not be touched.

## GPoint UI/design skill

- For GPoint UI/design, Pen, visual-reference, Golden, and design-to-React tasks, use the repo-local `gpoint-design` skill.

## Canonical Pen workflow

- For reference-image to editable-design work, use the connected `pencil` Pen MCP against `design/gpoint-design-system.pen`; the current Mac has no `pen` CLI in PATH.
- Automated Pen editing must use Pen MCP/CLI only. Do not use Computer Use to open Finder, operate file dialogs, move canvas objects, or edit Pen properties; GUI inspection is diagnostic-only.
- The required design chain is `design/references/*.png` → editable `.pen` layers/components → Pen screenshot/render → existing `tools/visual-diff/` comparison → bounded MCP correction loop → owner review.
- A reference PNG is never the final flattened design layer. Reuse canonical GPoint tokens/components and keep new screens editable.
- Screenshot/diff validation is required before reporting visual fidelity. Automated checks do not grant owner approval.

## Clean worktree after task

- After completing any GPoint task, leave the main repository either clean with verified changes committed and pushed, or explicitly BLOCKED with every remaining uncommitted file listed.
- Do not report a task COMPLETE while unexplained tracked modifications remain.
- Before starting the next task, verify `git status --porcelain` is empty.
- The durable automation checks are `pnpm repo:clean:start` before work and
  `pnpm repo:clean:end` before reporting completion; both must print `PASS`.

## Server deploy vs Mac worker operational readiness

Worker source/protocol/tenant/lane/API/schema compatibility remains a blocking
pre-apply gate. Temporary polling/auth/status/network/heartbeat availability is
non-blocking postdeploy dependency evidence, separately recorded as
`workerOperationalStatus` and `overallServiceReadiness`; it cannot turn proven
server COMPLETE into RECOVERY_REQUIRED or trigger rollback/redeploy. Recheck a
completed dependency through `--recheck-worker` without changing VPS release.
Real GSales inference acceptance is independent. See docs/release/automation.md.
