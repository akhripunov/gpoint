# AGATA Conversation Lab V2 Live E2E Preparation Plan

## Approved execution boundary

Implement only the existing Promptfoo → AGATA Lab adapter → canonical Worker LAB queue → Slot 03 → receipt path for one hash-verified V2 DEV scenario. Keep this work local. Do not install/restart Worker, create/activate live approvals, invoke a model or judge, optimize, publish, or touch production/network settings.

## Work plan

1. Verify the isolated candidate baseline and existing V2 split provenance without reading legacy V1 or SEALED HOLDOUT.
2. Add failing offline tests for V2 DEV source binding, exact prompt parity, independent arm histories, max eight generator calls, strict LAB-only jobs, receipt correlation, and no retry.
3. Extend the Worker LAB contract and asset inventory with only V2 DEV commitments derived from the existing split manifest; reject arbitrary prompts, other splits, API-key routes, and wrong model/reasoning.
4. Generalize the existing Promptfoo Python provider/runner to execute one invocation per turn, reserve the durable ledger before queue submission, and bind run/arm/scenario/turn/attempt/job/receipt identities. Continue to use the real Promptfoo CLI in offline tests with mock providers/Worker receipts.
5. Add a fail-closed sequential approval-phase contract: Phase A permits exactly 8 generator calls; after Phase A is closed and receipts are verified, Phase B permits exactly 3 judge calls. Never create live approval files in this task.
6. Exercise the existing three-judge Promptfoo path and deterministic aggregator with mocked LAB receipts; assert phase boundaries, blind isolation, budget accounting, errors, missing receipts, expiry/scope mismatch, and zero retries.
7. Install only exact pinned Promptfoo 0.123.1 dependencies in the Lab-local tooling directory if the exact Node runtime/dependencies are available. Prove the local CLI version; do not make a live provider call.
8. Run focused worker/provider/coordinator/judge tests, guarded full Conversation Lab offline tests, worker regressions, and `git diff --check`; inspect workflow triggers without dispatching any Actions.
9. Review the resulting diff and asset inventory, create a local commit only, record exact source and bundle hashes, and leave all changes `LOCAL_PENDING_RELEASE`.

## Acceptance evidence

- Promptfoo CLI `0.123.1` is installed and actually starts local mocked `eval` runs.
- V2 DEV → Promptfoo → Worker LAB → terminal receipt → Promptfoo passes offline for the bounded 8-generator/3-judge contract.
- Phase A and B approvals are sequential, uniquely scoped, bundle-bound, distinct, and tested fail-closed for expiry and overbroad scope.
- No legacy V1 or SEALED HOLDOUT source is read; no live generation/judge, Worker lifecycle operation, GitHub Actions, publication, or production mutation occurs.
- Exact local candidate commit and bundle identity are recorded with all relevant tests.
