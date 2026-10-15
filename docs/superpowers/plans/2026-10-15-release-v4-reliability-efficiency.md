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
