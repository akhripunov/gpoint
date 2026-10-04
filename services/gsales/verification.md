# G Sales V1 — independent review and repeat verification

Verification date: 2026-10-04. Local development evidence only. No production
database/runtime mutation, workflow dispatch, live Telegram login/contact, paid
LLM call, MAXIBOT change or Conversation Lab change occurred.

## Independent review

The independent security reviewer inspected the implementation, then performed
focused rechecks of the corrections. Final result: no unresolved P1/P2 within
the reviewed scope. This is code review evidence, not live owner acceptance.

| Finding | Correction and evidence |
| --- | --- |
| P1: restrictive RLS with incomplete role bindings could lock out GPoint | Explicit nonempty bindings include GPoint and every actual tenant; trusted bypass migration administrator enumerates the complete inventory before DDL. Restricted migrators are rejected. PostgreSQL regression verifies missing foreign binding rolls back without identity schema/partial migration. |
| P1: pending Core replies lost on restart | Persistent inbound ID, scoped completion polling and restart drain. Additional recovery selects only approved, unclaimed reply drafts; final send guards remain authoritative. Process death after approval commit but before claim is tested with a fresh router. Sending/unknown are never replayed. |
| P2: seller auto reply could not be enabled through API | Owner-authenticated seller resume updates the explicit permission after security checks. CRM PostgreSQL test covers enabling. |
| P2: paused seller could not receive consent | Explicit consent is handled before send/pause conditions and calls canonical age self-attestation. It does not enable sending. PostgreSQL consent-while-paused test passes. |
| P2: undelivered assistant output contaminated subsequent history | Core assistants start as draft; response context, summaries and history exclude unconfirmed delivery. CRM sync selects sent attempts only; Core validates exact scoped text and provider ID. HTTP E2E verifies the undelivered draft is absent from the next worker context. |

## Repeat test results

Executed using Python 3.12 in the local development venv and a uniquely named
disposable PostgreSQL 16/pgvector Docker container. The harness removes its
container in finally and strips production AGATA/GSALES environment variables.

```sh
PYTHONPATH=services/gsales services/gsales/.venv/bin/python services/gsales/tests/run_postgres.py
```

- G Sales suite: **29 passed**; includes actual PostgreSQL and cross-service HTTP E2E.
- Dedicated Core Sales integration: **1 passed**.
- Core baseline/legacy migration regression: **1 passed**.
- Full Core regression: **105 passed, 0 skipped, 0 failed**.
  This covers the previous 101-test set plus added production guards/integration.
- Previous single SKIP: `test_sales_postgres.py` lacked
  `AGATA_GSALES_TEST_DATABASE_URL` after its separate invocation. The harness now
  retains the isolated Sales role DSN for the full suite; the test executes there.
- Release tooling regression: **109 passed, 0 skipped, 0 failed**.
- Workspace TypeScript typecheck: PASS. G Sales mypy: PASS, 17 source files.
- Ruff and whitespace checks: PASS. Independent local Docker build/smoke: PASS.
- One dependency deprecation warning from Starlette/AnyIO; no test failure.

The pre-fix PostgreSQL probe reproduced foreign-tenant access through mutable
`app.tenant_id`; post-migration actual runtime-role tests refuse that access and
privileged-role/membership/schema-ownership hazards in both product directions.

## Required E2E scenarios

| Scenario | Result and boundary |
| --- | --- |
| Generate → persistent pending → send → confirm | PASS: real Core HTTP process, PostgreSQL state, durable worker RPC claim/completion, protected draft, mocked Telegram provider ID and canonical confirmed receipt. |
| Worker restart between generation and sending | PASS: separate worker processes claim/complete the persisted lease; fresh EventRouter drains. Additional process-death simulation covers approval commit before first dispatch claim. |
| Undelivered messages absent from next AGATA context | PASS: generation followed by pause prevents delivery; subsequent captured worker context excludes that draft and contains the prior confirmed reply. |
| No duplicate send after timeout | PASS: transport records the dispatch then raises timeout; unknown persists. Fresh drain/direct send cannot invoke transport a second time. |
| Two sellers isolated | PASS: scoped Core drafts/history/ownership tests and composite seller/lead approval checks refuse cross-seller reads/use. |
| First contact impossible without Owner Approval | PASS: owner API send returns 409 with zero transport calls until the exact content/recipient/seller/account binding is approved. Concurrent approved calls dispatch once. |

Telegram transport is mocked; worker completion is synthetic through the real RPC
contract. These results do not claim real Telegram delivery or actual LLM quality.

## Mac worker and remaining acceptance

Mac worker is the current existing development/QA inference provider. G Sales
depends on AGATA HTTP, not Mac hardware. Production must have an owner-approved
always-on server provider. Core production mode rejects Mac/local fake/disabled
providers, local Codex execution, missing explicit approval and missing credentials;
Sales production mode requires Core's production readiness fact. The production
provider is not yet selected/approved, so current local mode is not production ready.

Additional external prerequisites: owner-selected bot and MTProto test account for
approved live acceptance, authenticated Sales→Creator registration contract, actual
production role inventory, restored-backup rehearsal and reviewed guarded
agata0010→0011 release transition. All send flags default OFF. No deployment is
authorized by this verification report.

## V1.1 checkpoint — canonical path and shared worker

Canonical application path is now `services/gsales/`; brand, PostgreSQL schema,
GSALES tenant and source_system are unchanged. Owner's rename was verified as
identical content before a dedicated commit and clean-start PASS. Active path
references are updated; old service directory is absent.

Prepared worker v3 uses one serial execution slot/common installed lock and two
fixed targets with distinct endpoint/worker tokens, independent bounded backoff
and fair rotation. GPOINT-only configuration remains compatible. Sales gateway
authenticates separately and proves the bound Core identity; no worker DB access.
Core completion retains its receipt lease and rejects wrong-lease replay.
See `worker-integration.md` for installed v2 audit and the safe switching plan.

Repeat synthetic verification: Sales **32 PASS**, dedicated Core **1 PASS**,
baseline migration **1 PASS**, full Core **109 PASS, 0 SKIP** (previous105 plus
four target/limit tests), focused worker suite **21 PASS**, release tooling
**109 PASS**, Ruff/mypy (**18 files**)/workspace typecheck/Docker build+smoke PASS.
One Starlette/AnyIO deprecation warning remains. Cross-service integration creates
actual synthetic Sales seller/lead, persists encrypted cold pending without
invented recipient messages/consent, generates Sales first then GPOINT via the
same worker, verifies confirmed GPOINT response and Sales pending owner text.
Foreign job/wrong lease rejected; matching duplicate receipt accepted. Sending
flags OFF; no Telegram transport. Codex output in this regression is mocked.

Independent V1.1 review found one P2: Sales output limit4000 vs worker4096.
Sales-specific schema and pre-completion validation now cap4000, reporting invalid
output through failure RPC. An oversized-output regression verifies no completion. Focused independent
recheck: no unresolved P1/P2; four target regressions PASS.

**Real Codex QA remains BLOCKED**, not PASS/SKIP: the installed watch process
owns the shared lock. Prepared `--real-codex` command refuses before container
creation/inference. Separate owner approval is required for temporary idle-only
stop, two serial synthetic generations and finally restore of unchanged original
LaunchAgent. No installed source/config/LaunchAgent/process was changed.
Production always-on guard remains enforced; no production deployment is claimed.


One-shot owner approval was received for a controlled real Codex rehearsal. The
installed LaunchAgent was not stopped: three health polls returned sanitized
`worker_gateway_network_error`, DNS resolution for the GPoint host failed, and
the deployed gateway provides no read-only pending/running queue count. An idle
local poll does not prove its remote queue is empty. A tested controller now refuses
bootout unless both queue counts are explicitly zero; it snapshots original hashes,
requires a read-only zero queue snapshot and effective TTL≥221s, uses a 220-second watchdog (below default 420-second inference TTL/300-second lease), and restores
the unchanged plist/source/wrapper in finally. Controller safeguard tests: 2 PASS.
Real Codex remains BLOCKED on network recovery and read-only queue observability.
