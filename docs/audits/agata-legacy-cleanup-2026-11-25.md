# AGATA Legacy Cleanup — 2026-11-25

## Summary

The accepted read-only production inventory found no AGATA Engine container or AGATA database schema in the audited production deployment. Production chat is served through GPoint and Eros. Repository audit also found live compatibility obligations that prevent deleting the shared Worker bundle safely: GSales configures and hash-checks files in `services/agata/worker/`, including `agata_worker_pool.py` and `lab_queue.py`. This cleanup therefore removes the retired AGATA API fallback and the standalone Conversation Lab executable stack, while retaining those shared Worker files pending a separately authorized GSales Worker transition.

This is a local candidate only. No push, PR, Actions run, merge, or production change was performed.

## Runtime use and repository findings

| Area | Finding | Action |
| --- | --- | --- |
| Production AGATA Engine | The accepted read-only inventory reported no AGATA-named container and no AGATA schema/version table in the audited shared databases. Current production chat path is GPoint → Eros. | Removed stale API fallback and old gateway. |
| GPoint Messages API | `messages.ts` contained the retired AGATA client/integration path and merged AGATA/Eros state. Current Messages routes can use GPoint-owned state and Eros. | Removed the AGATA branches while preserving API response shapes, age checks, authenticated user scoping, and legacy-history fencing. |
| Old AGATA Worker Gateway | It was registered in the API and exposed the dual legacy route. Current Worker integration uses `GpointWorkerGatewayController` and `/gpoint-worker`. | Removed the old controller and migrated cancellation integration coverage to the current gateway/token boundary. |
| GPoint Mac Worker | `services/agata/worker/gpoint_mac_worker.py` is the current Eros-facing worker and uses `/api/gpoint-worker`. Its historical directory and environment names remain. | Retained; this is an active GPoint/Eros path, not the retired AGATA Engine. |
| Shared legacy Worker bundle | GSales hash-binds `agata_worker_pool.py`, `lab_queue.py`, and GPoint worker files. `agata_worker_pool.py` contains GPOINT, GSALES, and LAB lanes. | Retained pending independent GSales host/bundle reconciliation and transition. Deleting it now can break GSales and the existing worker contract. |
| Conversation Lab | Evaluators, Promptfoo runner/provider/package, and their executable tests/pipeline are isolated from the production GPoint → Eros request path. Queue durability/safety is separately exercised in `services/agata/tests/test_worker_lab_queue.py`. | Removed executable evaluator and Promptfoo stack. Kept historical configs, experiment records, and synthetic datasets. Reworked the queue tests to preserve queue-level safety coverage without evaluator imports. |
| Eros local test fixture | An obsolete synthetic AGATA HTTP API was started alongside the local Eros fixture. | Removed that mock API and its routes/credentials; retained synthetic embedding support and added a regression assertion that the AGATA shim is absent. |
| Historical data and release/migration safety | AGATA migrations, schemas, backup/restore and rollback files are historical state with separate retention requirements. Release workflow and filters are protected scope. | Left unchanged. No data/migration/backup/rollback or release workflow was removed. |

## Removed scope and measured reduction

The scoped diff removes 75 tracked files and 29,253 lines, while adding 270 lines of tests/documentation and updating 26 other files (101 changed files total before this report was added). The reduction is primarily the standalone Conversation Lab evaluator/test/pipeline/Promptfoo implementation, obsolete API client/integration/gateway, package, and synthetic AGATA API fixture. Counts are from `git diff --numstat` against baseline `075efe20980bd808a2d3d2aa21280e8de371e28c`, before adding this report.

Specific removed paths include:

- `packages/agata-client/` and its API dependency.
- `apps/api/src/agata-client.ts`, `agata-integration.ts`, and `agata-worker-gateway.ts`, plus their old integration test.
- `services/agata/conversation-lab/evaluators/`, `pipelines/lab.py`, executable Conversation Lab tests, and `tooling/promptfoo/`.
- Obsolete Conversation Lab worker integration/compatibility tests that imported removed executors.
- `services/eros-engine/local-test-api-routes.mjs` and the legacy AGATA API mock behavior in the local fixture.
- Docker dependency manifest/build-copy references for the removed AGATA client package.

## Retained safeguards and compatibility

- GPoint Messages still requires authenticated ownership and explicit age eligibility; the existing legacy conversation fence continues to prevent unsafe re-routing.
- Eros remains the sole dialogue engine path in the Messages controller; existing public route and response structures remain in place.
- The active GPoint Eros Worker endpoint remains registered and protected by its dedicated token and existing identity/allowlist validation.
- Synthetic Eros test topology remains isolated; only the obsolete AGATA shim was removed.
- GSales-shared Worker bundle, GSales lanes, queue durability mechanisms, migration history, historical data, backups, restore and rollback paths remain untouched.
- No release workflow, release script, approval rule, secret, production setting, or running service was changed.

## Validation

| Check | Result |
| --- | --- |
| API tests | 129 tests previously passed in the local API suite with loopback permission; two DB-dependent cases were skipped. A later retry encountered a fixed-port `3012` `EADDRINUSE` conflict in the integration test bootstrap, so that retry is not counted as a pass. |
| API TypeScript | `tsc -p apps/api/tsconfig.json --noEmit` passed before the final test-only cleanup. |
| Worker LAB queue | `python3.12 -m unittest services.agata.tests.test_worker_lab_queue -v` — 10 passed. |
| Docker dependency/static schema tests | `python3 -m pytest -q tests/test_docker_dependency_manifests.py tests/test_static_schema_dependency.py` — 14 passed. |
| Eros fixture | `node --check services/eros-engine/local-test-api-entrypoint.mjs` passed; 2 targeted local E2E topology/shim tests passed without invoking Docker. |
| Whitespace | `git diff --check` passed. |
| Package-manager lock validation | Not verified: available pnpm 11 rewrites this repository's pnpm v9 lock configuration and rejects the existing external `node_modules` symlink. The lockfile edit is limited to removing the deleted API dependency and deleted workspace importer; no install was run. |
| Full Docker/Eros build | Not run locally by design; use the existing GPoint Release Pipeline. Docker Desktop was not used. |

The supplied Dependabot baseline was 56 Conversation Lab advisories, 1 old AGATA Worker advisory, and 3 core GPoint dependency advisories. Dependabot API was not queried again. The current lock still resolves `effect` 3.21.0, `deepmerge-ts` 7.1.5, and `postcss-selector-parser` 6.1.4; updating these is outside this cleanup and should be handled as a separate dependency-security task with the repository's supported lockfile toolchain.

## Remaining blockers before deleting the old Worker

1. Reconcile the live GSales Mac Worker bundle and its configured hashes before removing or renaming `services/agata/worker/agata_worker_pool.py` or `lab_queue.py`. No host transition was performed here.
2. Decide and execute a separate retention/migration plan before removing historical AGATA database migrations, records, backup/restore, or rollback support.
3. Refresh the three core dependency advisories using the supported pinned pnpm version and update only after compatibility review.

These are scoped follow-up gates, not blockers to removing the API fallback, package, synthetic AGATA API, and isolated evaluator stack completed here.

## Candidate status

Base: `075efe20980bd808a2d3d2aa21280e8de371e28c`.

Status: `LOCAL_PENDING_RELEASE`. This report does not claim production runtime proof or readiness to publish.
