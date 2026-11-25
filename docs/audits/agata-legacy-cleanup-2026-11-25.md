# AGATA Legacy Cleanup — 2026-11-25

## Summary

The accepted read-only production inventory found no AGATA Engine container or AGATA database schema in the audited production deployment. Production chat is served through GPoint and Eros. Repository audit also found live compatibility obligations that prevent deleting the shared Worker bundle safely: GSales configures and hash-checks files in `services/agata/worker/`, including `agata_worker_pool.py` and `lab_queue.py`. This cleanup therefore removes the retired AGATA API fallback and the standalone Conversation Lab executable stack, while retaining those shared Worker files pending a separately authorized GSales Worker transition.

This is a local candidate only. No push, PR, Actions run, merge, or production change was performed.

## Runtime use and repository findings

Fresh read-only production evidence was captured from the authorized Mac on 2026-11-25. GitHub run [45542660307](https://github.com/akhripunov/gpoint/actions/runs/45542660307) completed `Build and publish immutable images` and `Kamal production Deploy and Verify` successfully for Core SHA `075efe20980bd808a2d3d2aa21280e8de371e28c`. Runtime container inspection confirmed that Core API, Bot, Worker and the Worker proxy use the image for this SHA. Production is component-mixed: Mini App is still on `4e5222ae9fb8417e216ad674382b425ceaf38978` (run `45327598308`), and Eros uses the official upstream revision `c7d63d77e9b52d7ae4952b8374512f571da065d5`. Kamal Proxy is running; PostgreSQL and Redis containers reported healthy. No production mutation was made.

| Area | Finding | Action |
| --- | --- | --- |
| Production AGATA Engine | The accepted read-only inventory reported no AGATA-named container and no AGATA schema/version table in the audited shared databases. Current production chat path is GPoint → Eros. The fresh container inventory is consistent with that topology. | Removed stale API fallback and old gateway. |
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
| API tests | The package's canonical API script (`NODE_ENV=test pnpm --filter @gpoint/api test`) was rerun with loopback permission: 129 TypeScript tests and 18 companion `.mjs` tests passed. The earlier port conflict came from invoking a wildcard that included the separate DB-backed `integration.test.ts`; that file imports the auto-starting API bootstrap and calls `bootstrap()` again on fixed port 3012. It is not part of the canonical suite. No other process was stopped, and the DB-backed integration test was not run because no disposable DB was verified. |
| API TypeScript | `tsc -p apps/api/tsconfig.json --noEmit` passed before the final test-only cleanup. |
| Codex MCP | `pnpm --filter @gpoint/codex-mcp test` — 7 passed after aligning the history expectation and test title with the current Eros-backed product behavior. The stable MCP server identity `gpoint-agata` remains unchanged for client compatibility. |
| GSales Python | Runtime tests are blocked by local tooling: installed pytest is under Python 3.9, while the package uses Python 3.10+ union syntax; Python 3.12 is present but has no pytest/Telethon installation. Dependencies were not installed. Source syntax can be checked separately, but this is not a substitute for the GSales runtime suite. |
| Worker LAB queue | `python3.12 -m unittest services.agata.tests.test_worker_lab_queue -v` — 10 passed. |
| Docker dependency/static schema tests | `python3 -m pytest -q tests/test_docker_dependency_manifests.py tests/test_static_schema_dependency.py` — 14 passed. |
| Eros fixture | `node --check services/eros-engine/local-test-api-entrypoint.mjs` passed; 2 targeted local E2E topology/shim tests passed without invoking Docker. |
| Whitespace | `git diff --check` passed. |
| Package-manager lock validation | pnpm 9.15.0 is available in the local Corepack cache. No dependency update was attempted: the remaining findings are transitive dependencies of the current Prisma/PostCSS toolchain and a deepmerge major-version change needs compatibility validation. |
| Full Docker/Eros build | Not run locally by design; use the existing GPoint Release Pipeline. Docker Desktop was not used. |
| Canonical pre-push | `python3 scripts/prepush.py --production-sha 075efe20980bd808a2d3d2aa21280e8de371e28c --no-fetch --main-sha 075efe20980bd808a2d3d2aa21280e8de371e28c` — FULL mode, `PREPUSH_STATUS=READY`. This validates local candidate admission only; it does not authorize publication or release. |

The current Linear ME-64 record gives a baseline of 57 AGATA-related and 3 core GPoint Dependabot alerts (60 total). The live Dependabot alert endpoint could not be read: the configured `gh` authentication is invalid, and the GitHub connector does not expose that endpoint. This means the current alert count is unverified; it does not establish a GitHub service outage.

The three known core findings map to these lockfile paths:

- `effect@3.21.0` and `deepmerge-ts@7.1.5` are transitive dependencies of `@prisma/config@6.19.3` (Prisma CLI/toolchain).
- `postcss-selector-parser@6.1.4` is brought through the existing PostCSS build tooling (`postcss-nested@6.2.0`).

The applicable advisories identify fixed versions `effect@3.22.1` ([GHSA-7589-6fvf-x4pw](https://github.com/advisories/GHSA-7589-6fvf-x4pw)), `deepmerge-ts@8.0.0` ([GHSA-ggr8-5vv4-36mx](https://github.com/advisories/GHSA-ggr8-5vv4-36mx)), and `postcss-selector-parser@6.1.5` ([GHSA-rj75-hqrm-r3gf](https://github.com/advisories/GHSA-rj75-hqrm-r3gf)). An override to `deepmerge-ts@8` would cross a major-version boundary and Prisma 6 is at end of support, so upgrading the Prisma toolchain is not a safe cleanup-only edit. No alert was marked resolved and no dependency was changed without compatibility tests. The `effect` and PostCSS patch/minor updates are candidates for a dedicated dependency change with the normal lockfile and workspace validation.

## Remaining blockers before deleting the old Worker

1. Reconcile the live GSales Mac Worker bundle and its configured hashes before removing or renaming `services/agata/worker/agata_worker_pool.py` or `lab_queue.py`. No host transition was performed here.
2. Decide and execute a separate retention/migration plan before removing historical AGATA database migrations, records, backup/restore, or rollback support.
3. Refresh the current Dependabot list through an authorized GitHub account; evaluate the two compatible patch/minor updates separately, and validate any Prisma/deepmerge major change against supported Prisma versions before updating.

These are scoped follow-up gates, not blockers to removing the API fallback, package, synthetic AGATA API, and isolated evaluator stack completed here.

## Candidate status

Base: `075efe20980bd808a2d3d2aa21280e8de371e28c`.

Status: `LOCAL_PENDING_RELEASE`. Read-only runtime and successful Deployment evidence confirm the production baseline SHA `075efe20980bd808a2d3d2aa21280e8de371e28c`; the remote `main` exact ref is the same SHA. The canonical pre-push gate passed on the final candidate in FULL mode. This is not publication authorization; no push, PR, Actions run, merge, or production change was performed.
