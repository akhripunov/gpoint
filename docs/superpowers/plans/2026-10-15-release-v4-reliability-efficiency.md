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

Integrated code candidate before this results note:
`ce853b2d7c971fcf4243b7111e07c39592338b57`. This documentation-only
checkpoint records its verification; the exact final handoff commit SHA is
recorded in the handoff evidence and Linear. Branch:
`codex/release-v4-reliability-20261015`; base production/main
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
- At the initial verification checkpoint, `pnpm repo:clean:start/end` and the
  broader `pnpm test:release` could not complete because npm registry DNS
  returned `ENOTFOUND`; migration inventory pytest was unavailable in the
  bundled Python. The later continuation below records the clean-gate rerun
  and migration-inventory pytest using the system Python.
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

## Owner continuation — ME-33/34/35 (2026-10-15)

Candidate continuation started from clean local commit
`47e66818f92f7f06741deeaa7e24f448e241bdcb`; work remains on the isolated
feature branch and is not published.

- **ME-33:** historical Prepare `38714817433` shows the GSales PostgreSQL
  runner stage took 312.619 s and ended about 47.7 s after the final Docker
  build. It ended about 60 s before artifact roundtrip completion. Thus this
  stage is heavy work but the sampled run does not prove an equal end-to-end
  latency reduction. For a verified exact GSales-only component diff, the
  proposed narrow runner retains all GSales tests and the separate
  AGATA-to-GSales tenant PostgreSQL tests, while omitting only the duplicated
  AGATA Core suite inside that runner. The independent primary AGATA
  PostgreSQL gate remains mandatory on the same candidate. AGATA/shared/mixed,
  unknown, unsafe, baseline-mismatched, or missing-gate scope selects FULL.
  The immutable receipt records exact baseline, changed paths, mode and the
  independently covered subset; bundle validation fails closed on missing,
  contradictory or malformed evidence. No end-to-end or hosted-minute saving
  is claimed before an authorized hosted run.
- Detailed job log for `38714817433` shows the duplicate AGATA Core suite
  inside the GSales runner executed **355 tests in 38.11 s**; the separate
  primary AGATA PostgreSQL suite had already passed. The primary final
  artifact-migration check ended at `09:49:55.107Z`, and GSales regression at
  `09:49:56.447Z`, a gap of about **1.34 s**. The following image export and
  artifact roundtrip took **59.950 s**. Therefore the local change proves
  removal of 38.11 s of repeated suite execution for eligible candidates, but
  this historical run suggests only about 1.34 s of possible critical-path
  reduction before the unchanged export/roundtrip path; no rounded hosted
  minute is saved in that sample. This is timeline analysis, not a measured
  post-change run.
- Independent review found and reproduced a receipt-validation gap where a
  GSales component receipt could omit coverage after falsifying the V3 AGATA
  regression flag. The verifier now requires coverage for every exact
  GSales component candidate and validates both V3 regression flags and
  candidate identity. The new malformed-receipt regression test passes after
  the fix.
- **ME-34:** no repository change can repair GitHub's external ResultsLog /
  Azure Blob post-job finalization transport. Existing bounded workflow
  timeout, exact run/attempt controller claim, preserved log/artifact upload,
  and read-only reconciliation remain the safe controls. A cancelled or
  incomplete parent remains non-PASS; reconcile its exact child run before
  considering a new dispatch. No architecture, upload, runner, or network
  settings were changed.
- **ME-35:** additional tests cover queued/unstarted jobs, per-job rounding at
  59.999/60/60.001 seconds, and a failed completed attempt followed by a live
  retry. Completed attempts contribute measured runtime; active retry time is
  only an elapsed lower bound and never a completed cost estimate.
- Fresh final verification after restoring local workspace dependencies:
  **125 Python unittest cases**, **11 migration-inventory pytest cases**, and
  **176 Node release-tooling tests** passed. `pnpm test:release`, workspace
  `typecheck`, `build`, and `test`, plus `pnpm repo:clean:start/end` passed.
  The initial typecheck lacked generated Prisma Client types; after generating
  the client from the checked-in schema, the full workspace typecheck passed.
  Ruff remains unavailable and is `NOT_RUN`.
- No paid Actions run was authorized, so new runner-seconds and end-to-end
  savings remain `NOT_MEASURED`. The original dirty checkout, GitHub, and
  production remain untouched. ME-31 remains open and local pending release.

## Owner continuation — GSales critical-path accounting (2026-10-16)

Historical run [38723866244, prepare job](https://github.com/akhripunov/gpoint/actions/runs/38723866244/job/116384809404)
was re-read from the job and step timestamps. The job ran 398 seconds
(`10:57:07Z`–`11:03:45Z`), approximately 7 rounded hosted minutes for this job;
this is an estimate, not a billing statement. The main Prepare step ran 300 s
(`10:58:13Z`–`11:03:13Z`).

- Docker build batch: 82.658 s, ended `11:01:29.525Z`.
- GSales PostgreSQL child: 189.293 s, started `10:59:06.366Z` and ended
  `11:02:15.659Z`, 46.134 s after the build batch.
- The final AGATA artifact-migration/health gate ended `11:02:14.284Z`, only
  1.376 s before GSales. Removing the duplicate AGATA Core suite (35.260 s in
  this run) can expose at most about 1.376 s before that independent gate
  becomes the blocker.
- Image archive export and artifact roundtrip then took 13.896 s + 42.511 s.
  The overall prepare job would remain about 397 s after the possible 1.376 s
  wall reduction, still 7 rounded minutes. The component-aware skip therefore
  removes real repeated runner work for exact GSales-only candidates but
  proves **0 hosted-minute savings** in this run.

The old child log accounted for 94.143 s of the 189.293 s GSales stage; the
remaining 95.150 s was outside its 14 named timers. Its original timestamps
were emitted only when the parent reaped the child, so they do not locate that
gap. No hosted replay is authorized, and a local Mac run cannot explain a
hosted-only gap. The historical 95.150 s cause therefore remains
`NOT_MEASURED`.

For the next authorized rehearsal, `prepare-release.py` passes the parent
monotonic start into the disposable GSales runner. The runner accounts for
startup/imports, container/readiness, DB setup, both AGATA and GSales
migrations, each test group, cleanup, and every interval between those phases.
The final AGATA `alembic upgrade head` now has its own named phase; it had been
counted only as a gap in the first review. The summary identifies whether the
window begins at parent launch or direct runner entry; phase plus gap totals
are checked, residual is reported separately, cleanup and failures are
attributed, and failure details/credentials are not echoed. This is diagnostic
instrumentation, not a performance reduction.

Local disposable Mac measurements on the current source were: FULL 40.548 s
(Core PostgreSQL setup/suite 27.025 s) and GSales-only component mode 13.771 s.
That is 26.777 s lower runner wall time for the narrow mode in this sample.
The surrounding container/readiness timings varied, and this is not a hosted
or complete-Prepare comparison. The FULL mode still passed 78 GSales tests,
the separate AGATA sales-tenant PostgreSQL test, and 379 AGATA Core PostgreSQL
tests. Component mode passed GSales and AGATA sales-tenant tests while marking
the Core suite covered by the independent AGATA gate. Unknown/shared scope
continues to select FULL.

Final telemetry verification after independent review: 19 timing/Prepare
inventory tests PASS; Ruff check PASS; `git diff --check` and `py_compile` PASS.
A fresh full disposable PostgreSQL run passed 81 GSales tests, 1 AGATA
sales-tenant test, and 380 AGATA Core tests (including the separately invoked
baseline test). Its local runner window was 39.568 s: 39.567 s measured phases,
0.001 s explicit gaps, 0.000 s accounting residual. Docker cleanup returned
success. The observed phase lines were:

| Phase | Status | Seconds |
| --- | --- | ---: |
| runner_imports | PASS | 0.095 |
| runner_setup | PASS | 0.000 |
| container_start_and_port | PASS | 0.205 |
| postgres_readiness | PASS | 1.116 |
| database_setup_and_initial_migration | PASS | 1.237 |
| agata_head_migration | PASS | 0.412 |
| agata_head_migration_and_gsales_ledger | PASS | 0.288 |
| gsales_pytest_suite | PASS | 9.406 |
| agata_sales_tenant_postgres_suite | PASS | 1.010 |
| agata_core_postgres_setup_and_suite | PASS | 25.544 |
| container_cleanup | PASS | 0.254 |

This local Mac run is not comparable evidence of hosted or complete-Prepare
savings. A Python 3.12 disposable venv and uniquely named local Docker database
were used. No hosted Actions, production, persistent DB, or worker was changed.
A subsequent same-source local component-only runner invocation passed the
same 81 GSales tests and 1 sales-tenant test, then emitted
`AGATA_CORE_POSTGRES_REGRESSION=COVERED_UNCHANGED`: 14.239 s total versus the
FULL run's 39.568 s, a 25.329 s lower local runner window in this sequential
warm-image sample. The independent AGATA Core PostgreSQL gate was not part of
that direct component-only invocation and remains mandatory in a complete
Prepare. This supports only local elimination of duplicate GSales-runner work;
it does not demonstrate a shorter full Prepare critical path or lower hosted
minutes. The unresolved hosted gap and absence of measured end-to-end or
Actions-minute savings are carried forward explicitly.
