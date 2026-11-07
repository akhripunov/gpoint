# ME-36 — stock Eros integration

## Owner decision

Use the unmodified upstream `etherfunlab/eros-engine` source at
`d1a140083321687212fb7b17c09cc0c3c9639f5e` (v2.2.1). Do not apply Rust
patches, maintain a fork, replace the installed GPOINT Mac Product Worker, or
add an EROS Worker/lane. The files in `patches/` are retained as historical
audit evidence only. `apply-patches.sh` now fails closed without changing its
input.

Build a local stock image from a clean exact upstream checkout:

```sh
services/eros-engine/build-stock-image.sh \
  /path/to/clean-eros-engine-checkout \
  gpoint-eros:stock-d1a140083321687212fb7b17c09cc0c3c9639f5e
```

The script verifies the upstream commit and clean Git tree before invoking the
upstream Dockerfile. It labels the local image with source, revision and
`patch-set=none`; the local E2E runner rejects unverified images. This is a
local build only and does not publish an image.

## Existing Worker adapter

Stock Eros supports OpenAI-compatible provider endpoints in its model config.
GPoint supplies a provider endpoint outside Eros. The adapter places a
short-lived, signed GPoint identity in the stock `audit.user` field. The GPoint
gateway validates tenant, user, persona and relationship claims before queueing
work on the existing GPOINT lane. A stable request ID is derived for each
distinct provider request so stock Eros calls made during one user turn do not
collide in GPoint idempotency. GPOINT AGATA work keeps priority, so Eros can
wait if that lane stays busy. The configured Codex Local/Product Worker route
must be confirmed in a permitted real integration run; this change does not
alter its installed configuration.

## Upstream constraints and safety gates

- Stock server startup currently requires the `OPENROUTER_API_KEY` environment
  variable to exist before loading its model config, even for a custom-only
  route. The local Compose E2E sets it to an empty value; every configured task
  routes to the internal GPOINT endpoint and the Docker network has no egress.
  No credential or hosted provider is used. Any deployed configuration still
  needs an OWNER-approved supported provider setup.
- Eros SQLx migrations use the default `public._sqlx_migrations` ledger. Keep
  Eros in a dedicated database; do not share the GPoint or AGATA database.
- Historical patch audit: `0001` changed provider startup/config behavior;
  stock Eros boots for this synthetic E2E with the required variable empty and
  all model tasks routed to GPOINT. `0002` moved the SQLx ledger; that is
  unnecessary when Eros owns a dedicated database. `0003` implemented the
  GPOINT bridge inside Eros; it is replaced by the external GPoint provider
  adapter. `0004` attempted to fence a delayed memory write after archive; it
  remains an unverified stock-engine race, not an applied patch. None of these
  patches is used in the pinned stock image.
- Stock Eros's relationship archive endpoint soft-deletes sessions and removes
  some relationship memory, but deliberately retains transcript history.
  OWNER withdrew a stock full-erasure API as a separate ME-36 Pilot and version
  selection prerequisite. Do not fork or patch Eros to add it. Applicable GPoint
  consent, age assurance, disclosure, data handling/retention/deletion,
  access-isolation, backup and recovery requirements remain active; this decision
  does not claim that stock Eros provides a full-erasure endpoint. The initial
  pilot remains synthetic-only in a dedicated Eros database, with no real user
  data. Eros embeddings use the pinned Qwen model on Layer7 through its private
  relay; this removes the Mac dependency for embeddings. The existing Mac
  Product Worker remains the chat-inference route and is still required for
  real Eros dialogue until that existing Worker is available to the request.
- Stock `classify_session` performs model/extraction and embedding before an
  unconditional memory upsert; archive deletes relationship memory. A delayed
  write crossing archive is a plausible data-resurrection risk, not a proven
  failure. It needs a synthetic reproduction and an upstream-compatible
  lifecycle solution before data deletion can be relied on. Transcript
  retention after archive itself is expected behavior.
- The pilot allowlist is synthetic and contains exactly one test user and one
  test creator. No normal user is routed to Eros, and no AutoWrite/memory-policy
  change is made here. The ordinary Native First V3 + Kamal 2 release owns
  component selection and steady-state Eros upgrades. Compose is used only by
  the disposable local test harness, not as the production deploy path.

## Local verification boundary

`run-local-e2e.sh` uses two disposable PostgreSQL databases, internal-only
Docker networking, synthetic identities/data, and a deterministic local Worker
fixture. The run passed a 15-turn dialogue, server age/consent gates, GPoint API
history, stock Eros extraction and durable recall, a 14-message gap, provider
context, existing GPOINT queue routing, and persistence of history and durable
memory rows after Eros restart. It also verified that a second JWT subject sees
no first-user sessions and receives HTTP 403 for a foreign session. The
GPoint inference queue's tenant RLS integration test also passes using the
`NOBYPASSRLS` `gpoint_worker_test` role. Eros itself has no per-user database
RLS policies, so its user isolation is verified at the stock HTTP API boundary,
not with Eros database RLS. The GPoint API has no connection or SQL grants to
the Eros database; product read receipts live in the GPoint database and store
only user/creator/session/message IDs plus `readAt`, never message bodies. The
post-restart recall check passed.

A separate disposable-catalog probe against the pinned stock image found all 34
ordinary and partitioned `engine` tables with RLS enabled, none with `FORCE ROW
LEVEL SECURITY`, and no policies. The actual `eros_runtime` role has
`BYPASSRLS` and can see the seeded persona; a role with equivalent table grants
but `NOBYPASSRLS` sees zero seeded persona rows, while `/healthz` still returns
`ok`. This is not per-user isolation: it is an RLS configuration that requires
the runtime role to bypass policy enforcement. The runner now checks every
ordinary and partitioned table, confirms the runtime-visible row count, and
reports this result explicitly. It remains a hard VPS-pilot security blocker.
This is a stock-engine integration E2E with a fixture provider, not real Codex
inference. It does not prove Mac Product Worker availability, VPS resource
needs, or production performance.

The pinned stock source compiled with the isolated `rust:1.98` build image; its
offline `print-openapi` command and full local Compose E2E passed. Evidence:
`LOCAL_E2E_STOCK_EROS_IMAGE=PASS`, `LOCAL_E2E_15_TURN_DIALOGUE=PASS`,
`LOCAL_E2E_MEMORY_EXTRACTION=PASS`, `LOCAL_E2E_DURABLE_MEMORY=PASS`,
`LOCAL_E2E_MEMORY_RECALL_RESPONSE=PASS`,
`LOCAL_E2E_PROVIDER_CONTEXT=PASS`, `LOCAL_E2E_RESTART_HISTORY=PASS`,
`LOCAL_E2E_STOCK_API_USER_ISOLATION=PASS`,
`LOCAL_E2E_GPOINT_QUEUE_RLS_TENANT_ISOLATION=PASS`,
`LOCAL_E2E_MEMORY_RECALL=PASS`, and `LOCAL_E2E_CLEANUP=PASS`.
`OPENROUTER_API_KEY` was empty, with no credential; the inference test network
had no egress. Real inference was not run. The GPoint queue tenant RLS test
passed with a non-bypass worker role; Eros has no per-user RLS, and the API
fixture's `BYPASSRLS` receipt access is not a production isolation check.

The deterministic fixture run is not a real Codex Local E2E. A later bounded
attempt made one Codex CLI invocation and stopped on its first network error;
no model response was received and no follow-up inference was sent. On
2026-10-23, read-only host diagnostics found `chatgpt.com` resolving to
`240.0.0.48` and `api.openai.com` to `240.0.2.128`, both in the non-public
240.0.0.0/4 range. The configured resolver was classified as private and the
route used a `utun` interface. An unauthenticated HEAD to `chatgpt.com` returned
403; the request to `api.openai.com` timed out during connection/TLS. This
identifies a current network-path blocker but not the specific resolver,
tunnel, or policy responsible. No DNS, VPN, route, or Worker settings were
changed. A new limited OWNER grant is required after the network path is
confirmed healthy.

The adapter provisions the persona with stock `seed-personas` and uses stock
`POST /comp/chat/start` to create or resume a user instance. GPoint stores the
creator-to-genome and user/creator-to-instance mappings in its own database;
runtime persona and relationship operations no longer query Eros tables. The
synthetic E2E follows the same path. A stored instance mapping is passed back
as `instance_id`; upstream requires that instance to still be active and does
not reactivate it. A missing mapping uses `genome_id`, which can create or
reactivate an archived instance. Since stock session listing omits archived
sessions and genome IDs, an initial rollout must use a fresh, dedicated Eros
database with no historical instances. Existing Eros history cannot be safely
mapped through the public API alone. Each enabled creator profile also needs
its `erosGenomeId` provisioned by `seed-personas` before routing is enabled;
there is no stock genome-provisioning HTTP endpoint or safe runtime fallback.

The stock API has no operation for selected-message read receipts or unread
counts, and `POST /comp/chat/{session_id}/read` marks the whole session. The
GPoint adapter therefore indexes assistant message IDs returned by the stock
history API and stores UI read timestamps in GPoint-owned metadata. The send
route records each returned assistant ID; history and inbox reads reconcile
IDs from the latest history page. Read acknowledgement checks that every
requested ID was previously observed for the authenticated GPoint user,
creator, and Eros session. This preserves visible-message-level semantics
without direct Eros SQL or a broad Eros DB role. Reconciliation is bounded to
the pages the UI/inbox reads; historical sessions or messages outside those
pages are not backfilled and are not claimed to have exact unread counts.

Stock Eros has no per-user database RLS policies. Its public HTTP handlers
scope session and message operations to the authenticated Eros user, which the
adapter maps deterministically from the GPoint account. The local API-isolation
test verifies that HTTP boundary. Removing receipt SQL closes the direct
cross-service database bypass, but it does not fix Eros runtime `BYPASSRLS` or
provide per-user database isolation. This is accepted only for the isolated
single-synthetic-user pilot; it remains a hard gate before any real-user
access.

Before the protected production Apply, verify the stock image SHA and
provenance, dedicated database ownership, SQLx migration compatibility, ACL,
backup and restore, network boundaries, provider route, existing GPOINT Worker
compatibility, VPS resource headroom, and AGPL-3.0 distribution/source
obligations. The Apply includes a short synthetic runtime check of API,
dialogue, durable memory recall, and the existing Mac Product Worker before
Messages routing changes. Any later real-user access must meet the applicable GPoint
consent, privacy, retention and deletion obligations; the withdrawn full-erasure
API prerequisite must not be reintroduced under another name. Mac resource
measurements are directional evidence and cannot replace a VPS load measurement.

## Release and pilot boundary

The initial AGATA-to-Eros transition uses the existing protected Native First V3 Deploy and Kamal 2.
It starts Eros while AGATA remains available,
checks API, dialogue, durable memory recall, and the existing GPOINT Mac Product
Worker with synthetic identities, then changes Messages routing and stops the
AGATA API and Memory Worker only after those checks pass. Failure restores the
previous route and AGATA services; it never downgrades either database. Keep the
AGATA database and its existing 102 messages intact for recovery. Do not copy
conversation history or memory rows into Eros and do not drop old data or
schemas during cutover. The production Apply plan and data impact must be shown
to OWNER before the first production mutation; the protected Apply requires a
separate current authorization.

GPoint Messages uses one global policy: users must be 18 or older, accept the
current GPoint Terms of Use at `https://gpoint.one/terms` and Privacy Policy at
`https://gpoint.one/privacy`, and acknowledge the current AI disclosure. The
document versions and each confirmation are stored for audit. Legacy AGATA
regional policy rows remain untouched and never select GPoint runtime rules.
Previously active consent rows are retained in the GPoint audit history, with
only the newest confirmation left active; age assurance is selected by its
latest version without a regional filter.

After accepted cutover, Eros upgrades use the existing GPoint Native First V3
workflow and Kamal 2. Eros, Core, AGATA, and Mini App retain independent version
and deployment scopes. Core-only or Mini-App-only releases leave Eros untouched;
Eros-only releases leave the other components untouched unless a verified
compatibility dependency is explicitly included in scope. Stock SQLx migrations
run only when the selected Eros version requires a schema change and only after
the existing Deploy gates verify the exact migration set, compatibility,
backup/restore and recovery plan. Database rollback is never automatic.

The local Compose harness is only a disposable test environment. It does not
define production topology or deploy procedures. Historical V5 Prepare/Deploy,
Compose production Apply, and separate release-controller instructions are not
part of the current GPoint Eros release contract.

## Local database recovery rehearsal

A disposable stock-image rehearsal applied SQLx migrations, created a synthetic
marker, produced a custom-format PostgreSQL snapshot, restored it into an
isolated database, verified the marker and migration ledger, and checked the
migrator ownership. The rehearsal validates the local snapshot/restore mechanics
only; it is not production evidence and does not authorize a database swap.

The protected production Apply requires a fresh, exact-SHA recovery plan and
backup/restore verification for its dedicated Eros database. Steady-state
version rollback may restore a compatible stock image but must not downgrade the
SQLx database. If a migration is incompatible with the prior image, block image
rollback and use the approved forward-recovery plan.
