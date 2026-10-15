# GPoint Release V4 Reliability & Efficiency

## Verified baseline

- Production/main baseline: `a700198a843c12a84f2e537178de752e3eddd54d`.
- Production ledger: COMPLETE, recoveryRequired=false, DB heads AGATA `agata0011`, GSales `001_gsales`.
- ME-26 / PR #85 already contains bounded Docker build concurrency and GSales PostgreSQL phase timing. Its branch rehearsal measured a ~70 second faster Docker stage, but PostgreSQL remained on the critical path and ended after the build stage; end-to-end Prepare savings were not demonstrated.
- Existing V4 component classifier is advisory only; existing V3 is the only Prepare gate selector. Prepare and automatic preflight remain separate workflows.
- Worker compatibility preflight detects installed-source drift but does not prove candidate migration/staging/rollback feasibility before READY_FOR_OWNER. Apply remains fail-closed.
- Runner run #38769710148 reached finalization after workflow steps passed; diagnostics show GitHub ResultsLog/Azure Blob upload timeouts followed by a cancellation timeout. The upload/finalization transport is not controlled by repository workflow code.

## Scope and invariants

Implement only ME-32, ME-33, ME-34, and ME-35. Preserve immutable artifacts, exact SHA, provenance, tenant/lane isolation, queue/JIT checks, rollback, owner approval, and all mandatory readiness gates. Unknown impact remains FULL. A skipped test is `COVERED_UNCHANGED`, never PASS. No changes to production, credentials/network settings, AGATA Conversation Lab, or memory behavior. Do not merge Prepare and Preflight.

## Work sequence

1. **ME-32 Worker readiness:** add a read-only, candidate-bound feasibility result to preflight. It must distinguish exact match, verified migration plan, and blocked; it must not stage/switch the worker or reuse stale queue snapshots. Keep existing just-in-time queue checks for apply. Add regression and fail-closed tests.
2. **ME-33 component impact:** reuse the existing advisory classifier and V3 plan. Add a deterministic dependency/test-suite matrix and permit skipping only heavy suites proven unchanged by exact production-baseline diff. Changed tests, AGATA shared consumers, migrations/config/infra/lockfiles, rename/delete/unknown all force the relevant tests or FULL. Keep every build/image/provenance/health/migration/rollback safety gate unless evidence proves an operation itself is independent and the exact receipt records `COVERED_UNCHANGED`.
3. **ME-34 runner reliability:** preserve the externally managed log upload. Add bounded in-workflow evidence/reconciliation only if a repository-controlled gap is proven; otherwise document the external ResultsLog transport cause and safe non-blind recovery, without pretending code can control GitHub runner finalization.
4. **ME-35 telemetry:** extend local telemetry calculations to distinguish queue wait, job execution, workflow elapsed, stage critical path, per-job rounded minute estimates and duplicated run IDs. Missing source timestamps remain `NOT_MEASURED`; estimates are not billed minutes. Add tests for incomplete jobs, rounding, duplicates, queued wait and multi-run aggregation.
5. Run targeted tests, workflow/static validation, diff check; conduct independent review; record exact local checkpoint SHAs and clean status. Update existing ME-31 and child issues with local handoff evidence only; do not publish code or trigger Actions.

## Acceptance

- Security gates remain fail-closed and no Prepare/Preflight workflow topology changes.
- Worker readiness never claims a transition has occurred; migration feasibility and JIT apply safety remain distinct.
- Impact optimization produces deterministic, exact-source decisions and no false skips.
- Runner behavior is accurately bounded where code controls it; external upload failure remains non-PASS and is explicitly identified.
- Telemetry is tested and does not overclaim billing or end-to-end savings.
- Local branch is committed and clean; the original dirty checkout is untouched; no GitHub write, paid Actions run, or production mutation occurs.

## Local execution result — 2026-10-15

Integrated local candidate: `ce853b2d7c971fcf4243b7111e07c39592338b57`, branch
`codex/release-v4-reliability-20261015`, base production/main
`a700198a843c12a84f2e537178de752e3eddd54d`.

- **ME-32 Worker readiness:** preflight now requires a candidate-bound static
  migration-feasibility plan before READY evidence is accepted. It proves
  source/assets, tenant-binding provenance, canonical runtime-root containment,
  and rollback-source presence. It does not stage or switch a worker. Fresh
  tenant queue snapshots, no-active-inference, rollback snapshot capture,
  candidate startup, lock checks, and queue revalidation remain JIT apply gates.
  Regression coverage includes explicit tenant fallback with an escaped
  `runtime/current` symlink; it is rejected.
- **ME-33 component impact:** Prepare binds the V4 impact advisory to the V3
  verified production baseline and exact candidate diff, and includes it in
  FULL and STATIC_UI receipts. AGATA and GSales PostgreSQL suites and all three
  exact-SHA image builds remain required. `skippedHeavyWork=[]`; image reuse
  and selective deploy are not enabled because independence/provenance is not
  proven. This records impact; it does not yet reduce hosted work.
- **ME-34 runner finalization:** existing automation documentation records
  run `38769710148` and the observed GitHub ResultsLog/Azure Blob upload
  timeouts. Repository-controlled workflow steps had finished; available
  evidence does not identify an owner-controlled network or runner cause.
  Preserve log uploads, treat cancellation as non-PASS, and reconcile the exact
  child run before considering any retry. No global network or runner setting
  was changed.
- **ME-35 telemetry:** run/job summaries now separate queued wait, completed
  runner seconds and per-job ceiling minute estimates from in-progress job
  elapsed lower bounds. In-progress jobs are never counted as completed cost.
  A live run remains `PARTIAL`; final workflow elapsed and dependency-DAG path
  remain `NOT_MEASURED` until the source provides completion/dependency data.
  Estimates are not GitHub billing.

### Verification and performance evidence

- Integrated affected Python suites: **105 passed**.
- Full Node release-tooling test file: **92 passed**. The test needed temporary
  Git worktree metadata under the shared repository `.git`; it passed with the
  narrowly approved local test execution.
- Python compile, Prepare/Deploy YAML parsing, and `git diff --check`: **PASS**.
- Independent review of the integrated commit: **PASS**. Prepare remains
  `coalesce → prepare → publish`; Deploy remains a separate
  `validate → deploy → publish` workflow.
- `pnpm repo:clean:start/end` and the broader `pnpm test:release` could not
  complete: missing dependencies required npm registry access and DNS returned
  `ENOTFOUND`. The migration-inventory pytest module also could not be loaded
  in the bundled Python because `pytest` is unavailable. These are unrun gates,
  not passing tests.
- Historical baseline Prepare `38714817433`: 528 s of recorded hosted job
  runtime plus a separately recorded 21 s publication step; rounded job-minute
  estimate 10, not billed minutes. The Docker rehearsal reduced the Docker
  stage by about 70 s, but its build stage ended about 46–48 s before GSales
  PostgreSQL completed. In those samples, measured end-to-end critical-path
  savings were **0 s**.
- This local candidate has no new GitHub Actions run by owner instruction.
  New hosted seconds, per-job minutes, queue wait, and end-to-end savings are
  therefore **NOT_MEASURED**. No Actions-minute reduction is claimed.

The isolated candidate is committed and clean. The original dirty checkout was
not modified. No push, PR, merge, Actions dispatch, production change, database
mutation, worker transition, or AGATA inference occurred. Status remains
`LOCAL_PENDING_RELEASE`; any hosted rehearsal or publication needs separate
owner authorization.
