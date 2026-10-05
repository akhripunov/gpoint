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

Candidate worker V3 supports GPOINT and optional GSALES. Each configured target
requires a canonical `*_AGATA_TENANT_ID` trusted pin in the mode-600 local
configuration; pins must differ. The GPOINT pin is owner-verified for the
specific runtime, not compiled into source. GSALES claims must carry matching
server-authored target/tenant. GPOINT retains the existing V2 URL/token/RPC
contract and accepts legacy claims with no identity metadata; if the gateway
provides either identity field, both must match the local pin. No inference-job
payload can choose its route, credentials, or tenant. GPOINT-only configuration
remains supported when its runtime pin is present.

One AGATA supervisor owns GPOINT, GSALES, and LAB lanes. It schedules isolated
execution processes with a default pool concurrency of one; separate lifecycle,
pool, slot, and job locks guard ownership. GPOINT receives four scheduler turns
for every two GSALES turns, while each target keeps independent network/auth/job
backoff. LAB is only considered when production claims are empty and requires
its own approval immediately before inference. Increasing concurrency is blocked
until Codex provider parallel safety is explicitly verified. Each Core retains
its own pending/lease recovery.
Per-target status contains timestamps/states/error codes, never token/prompt/text.
Codex retains read-only/ephemeral/ignore-rules/isolated-temp-dir/structured-output,
environment allowlist and tool-use rejection. Production always-on guard remains.

## Configuration prepared, not installed

Keep current GPOINT variables and add separately generated GSALES credentials only
after deployment/setup is separately authorized:

```text
GPOINT_AGATA_TENANT_ID=81a4ba28-6de5-4b03-8332-0661ba4d46f4
GSALES_AGATA_WORKER_URL=https://<approved-sales-host>/agata-worker
GSALES_AGATA_WORKER_TOKEN=<distinct-worker-secret>
GSALES_AGATA_TENANT_ID=c4d67889-877e-4d1b-a8d8-597c479d638f
```

For disposable tests, set the GPOINT pin to that test Core's own tenant UUID;
never copy the production pin into a test runtime.

The Sales gateway has `GSALES_WORKER_TOKEN` with the matching worker secret and a
different `GSALES_AGATA_API_TOKEN` for its GSALES Core. Loopback HTTP is accepted
only with explicit local-test mode and a literal loopback hostname. No production
endpoint/config is changed by this task. The existing installer replaces source
and restarts the single agent: do not run prepare/install during this task.

## Real Codex rehearsal — one-shot owner approval (04.10.2026)

The earlier one-shot approval was limited to candidate SHA
`50034a22ff73b15b6093bf8edf6d3de7a810b8a5`; it cannot authorize testing a new
candidate. No test window was executed. For any future attempt, the controller
requires a full candidate SHA argument and verifies it equals the clean current
HEAD.

Read-only preflight command after the candidate is committed:

```sh
services/gsales/.venv/bin/python scripts/run-gsales-one-shot-real-codex.py \
  --candidate <exact-40-character-candidate-sha> --preflight-only
```

No bootout, credential edit, V3 install, or real inference has occurred. The
installed LaunchAgent is V2, loaded and locally idle. Its status has no remote
queue depth or TTL, so it is never used as proof that the durable queue is empty.
Synthetic E2E uses the candidate V3 source and disposable database.

A guarded controller at `scripts/run-gsales-one-shot-real-codex.py` snapshots the
original plist/source/wrapper hashes, checks the exact candidate and fresh local
idle poll, and obtains aggregate pending/running counts plus effective TTL from a
repeatable-read, read-only query inside the image-verified live AGATA API
container. It pins the verified production runtime tenant, release SHA, and
both immutable image digests. It requires an empty queue and TTL ≥420s (300s maximum full outage
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

The queue-snapshot probe verifies the production tenant against the audited
identity and scopes its read-only transaction to that tenant without substituting
or overriding the runtime GUC. It requires an active tenant row plus an active
GPOINT regional profile. The 2026-10-04 07:49:08Z snapshot is historical and predates the current
exact-candidate controller. It must not be used to authorize a worker switch or
Real Codex QA. The current controller requires `--candidate` to equal the clean
checkout's full HEAD SHA and obtains a fresh server-side snapshot. Current
preflight evidence and timestamp are recorded separately in the active G Sales
integration handoff; preflight must be repeated immediately before any approved
worker operation. A preflight pass is not itself authorization to stop the
worker or run Real Codex QA.

The controller pins full production SHA/image/tenant identities, and requires a
clean checkout plus exact candidate SHA. The shared-worker suite tests per-runtime
UUID bindings, malformed or duplicate pins, mismatched claims, separate target
credentials, independent scheduling/backoff and serial execution. V2 GPOINT
legacy claims without tenant metadata are accepted only through the exact
audited production gateway URL; identity-less claims from any other endpoint
fail before inference. GSALES claims always require server-authored target and
tenant identity.

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
