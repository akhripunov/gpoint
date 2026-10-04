# Shared Mac worker — V1.1 audit and prepared integration

Audit 2026-10-04: installed `one.gpoint.agata-codex-worker` LaunchAgent is loaded,
one watch process, KeepAlive. Installed Python source is v2 and matches the V1
repository source SHA256 `606490fcb4f5e3bd119bff5759ee994d25543a8fa7e121db41014fd5617c5f60`.
Its config `~/.config/gpoint/agata-worker.env` is mode 0600 and configures only
GPOINT URL/token plus Codex model/reasoning/timeout. No credential is reproduced.
Installed source, config, LaunchAgent and running process were not changed.

## Actual boundaries

Existing Nest GPOINT gateway `/api/agata-worker` authenticates a dedicated worker
Bearer token with constant-time comparison, and forwards health/claim/complete/fail
using the server's GPOINT Core token. It does not route GSALES jobs.

Prepared GSALES gateway in independent Sales API exposes `/agata-worker` and the
same four POST actions. It authenticates `GSALES_WORKER_TOKEN`, distinct from
Sales Core/owner/bot tokens. Before forwarding it proves fixed GSALES Core
source/tenant/security and production guard. Worker never receives Core credentials
or PostgreSQL access. Completion/failure schemas forbid extra target/tenant fields.

Both instances run the same Core code. Each owns tenant-scoped encrypted durable
outbox jobs under its non-bypass login. Core decrypts only at authenticated claim;
worker sees only the prepared inference request. Results return via the same
target endpoint/token/lease. Core encrypts the result and rejects foreign IDs,
expired/wrong leases. Repeated completion proves the retained receipt lease.
No PostgreSQL schema/tenant rename or new queue is introduced.

Candidate worker v3 uses two fixed targets, GPOINT and optional GSALES. Tenant
UUIDs are source constants. Sales claims must carry matching server-authored
target/tenant. GPOINT supports the legacy gateway without metadata; if present,
identity must match. Configuration refuses reused endpoint/token. No payload
can select credentials, endpoint or tenant. Default GPOINT-only config remains valid.

One unchanged process `worker.lock` serializes the watch lifecycle and Codex
subprocesses. Round-robin gives each healthy target a turn; target network/auth/job
failures have independent bounded backoff, leaving the other target running.
No concurrent Codex CLI is added. Each Core retains its own pending/lease recovery.
Per-target status contains timestamps/states/error codes, never token/prompt/text.
Codex retains read-only/ephemeral/ignore-rules/isolated-temp-dir/structured-output,
environment allowlist and tool-use rejection. Production always-on guard remains.

## Configuration prepared, not installed

Keep current GPOINT variables and add separately generated GSALES credentials only
after deployment/setup is separately authorized:

```text
GSALES_AGATA_WORKER_URL=https://<approved-sales-host>/agata-worker
GSALES_AGATA_WORKER_TOKEN=<distinct-worker-secret>
```

The Sales gateway has `GSALES_WORKER_TOKEN` with the matching worker secret and a
different `GSALES_AGATA_API_TOKEN` for its GSALES Core. Loopback HTTP is accepted
only with explicit local-test mode and a literal loopback hostname. No production
endpoint/config is changed by this task. The existing installer replaces source
and restarts the single agent: do not run prepare/install during this task.

## Real Codex rehearsal — one-shot owner approval (04.10.2026)

Prepared command from the repository root:

```sh
PYTHONPATH=services/gsales services/gsales/.venv/bin/python services/gsales/tests/run_postgres.py --real-codex
```

Owner granted one test window; no stop occurred. The installed LaunchAgent is
loaded and its fresh local status reports IDLE, no current job and zero network
failures. Its status contract has no read-only pending/running queue depth or
effective inference TTL, so local IDLE cannot prove the remote durable queue is
empty. The controller reads the local status file without network calls and
fails closed before bootout unless both remote queue counts and effective TTL
are explicitly provided. The freshness limit is 15 seconds, above the worker's
10-second idle poll interval.
Synthetic rehearsal already uses the same worker/API paths with mock output.

A guarded controller at `scripts/run-gsales-one-shot-real-codex.py` snapshots the
original plist/source/wrapper hashes, checks the exact candidate and fresh local
idle poll, and obtains aggregate pending/running counts plus effective TTL from a
repeatable-read, read-only query inside the image-verified live AGATA API
container. It requires an empty queue and TTL ≥420s (300s maximum full outage
plus a 120s margin). One monotonic outage deadline starts before bootout and ends
only after the original LaunchAgent resumes polling; the test budget reserves up
to 10s for termination and 60s for bootstrap/poll confirmation. The query is
refreshed directly before switching, then local activity is checked again.

The 2026-10-04 identity reconciliation corrected the earlier **NO-GO** diagnosis.
The live runtime is configured with tenant
`81a4ba28-6de5-4b03-8332-0661ba4d46f4`; the same active tenant is visible in its
ordinary tenant context, has an active GPOINT regional policy profile, and
contains the GPOINT user, character, and conversation records. Release
provisioning creates a UUID4 in `production.env` only when `AGATA_TENANT_ID` is
absent; the AGATA baseline migration registers that configured UUID and creates
the GPOINT policy profile. The observed registry creation timestamp
`2026-09-28T06:14:55.151685Z` is consistent with the nearby initial release
bootstrap, though the audit does not claim a specific run wrote the row. The
fixed `7508f36f-774a-48aa-8c6b-42f6f160415d` value comes from candidate/test
fixtures and a draft worker target map, not the running production
configuration. Ordinary AGATA requests use the runtime-configured tenant. No
tenant GUC override or production configuration change was made.

The live API/Core release identity is
`68bf0212d7aa5125394b0b39ffba9226eb106c0d`. The apparent ledger value
`8c30d676809f8c0f6d3254891bf096b0b8740bd4` came from the historical
`docs/release/state/production.json` snapshot on application `main`. The
authoritative `gpoint-release-ledger` branch, exact Deploy run, production ref,
immutable evidence and current Linear release journal all record 68bf. Deploy
run `37140588175` completed production apply; immutable bundle and evidence
receipts are attached to that SHA. The production ref is
`gpoint-production-20261003T172830088Z`; API and AGATA image digests were
verified against the running containers. GitHub confirms 68bf is 35 commits
ahead of 8c30. There is no live release identity drift to reconcile.

The queue-snapshot probe now scopes its read-only transaction to the tenant
selected by live AGATA settings and requires an active tenant row plus an active
GPOINT regional profile. It never substitutes the candidate/test UUID. The
verified live snapshot on 2026-10-04 07:49:08Z had zero pending/running inference
jobs and effective TTL 420 seconds; API/Core artifacts were healthy at the same
SHA. The updated controller completed `--preflight-only` with **PASS**. This is
GO for the Queue Preflight gate only, not authorization to stop the worker or run
Real Codex QA. No bootout, deploy, migration, tenant mutation, or Real Codex
inference occurred.

The focused controller suite covers stale/untrusted/wrong-runtime snapshots,
empty/non-empty queue, the TTL margin, a shared outage deadline, and the
queue-then-local recheck order. It passes 7 tests. Syntax/AST checks and
`git diff --check` pass. Ruff was not rerun in this reconciliation.

The P0 worker-token exposure is recorded without its value or any derived
fingerprint in `docs/security/gpoint-agata-worker-token-incident-2026-10-04.md`.
An exact-release rotation handoff is recorded in
`docs/security/gpoint-agata-worker-token-rotation-handoff-2026-10-04.md`.
Rotation is not performed; it requires separate OWNER approval and a coordinated
Mac/VPS maintenance window.

Concrete plan after the owner-approved one-shot window can satisfy queue/network preflight:

1. Recheck installed worker idle/current_job_present=false. If busy, wait for its
   existing job to finish; never interrupt generation. Preserve original plist,
   source/config hashes and loaded state. Record no token contents.
2. Temporarily bootout only `one.gpoint.agata-codex-worker`. Do not modify its
   source/config/LaunchAgent or install candidate worker. GPoint generation will
   briefly wait in its existing durable queue during this maintenance window.
3. Run the command above: unique disposable DB, separate GPOINT/GSALES Core
   instances, synthetic seller/lead/product facts, existing Codex Local executable,
   same global lock. Generate one Sales cold draft and one GPOINT response serially;
   no recipient consent/incoming message fabricated for the Sales cold draft.
   Cold draft remains pending owner review; sending flags OFF/no Telegram transport.
4. In finally bootstrap the exact original plist whether testing passes/fails;
   verify process loaded, original hashes, resumed polling/error status. If restore
   fails, report immediately and repair original agent before other work.
5. Save exact outputs under ignored `test-evidence/real-codex.json`; publish tested
   content/status in the current chat and canonical Linear. Distinguish synthetic
   fixture routing from real Codex inference and from production acceptance.

No second LaunchAgent, parallel CLI, production migration/deploy or paid API is
needed. Permanent installation/enabling a Sales target requires a later separate
owner-controlled switch from the exact reviewed GitHub candidate.
