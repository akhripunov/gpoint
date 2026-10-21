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
  GPoint currently has no Eros user-data erasure path. The new GPoint
  relationship mapping uses restrictive user/creator foreign keys so deleting
  either parent cannot silently discard the only Eros instance pointer. This
  intentionally blocks GPoint deletion while a mapping exists; it does not
  erase Eros data. A release pilot is blocked until a tested deletion adapter
  covers transcript, memory, summaries, embeddings and queued/background writes
  without changing Eros source.
- Stock `classify_session` performs model/extraction and embedding before an
  unconditional memory upsert; archive deletes relationship memory. A delayed
  write crossing archive is a plausible data-resurrection risk, not a proven
  failure. It needs a synthetic reproduction and an upstream-compatible
  lifecycle solution before data deletion can be relied on. Transcript
  retention after archive itself is expected behavior.
- The test allowlist remains synthetic and local. No broad production routing
  or AutoWrite/memory-policy change is made here.

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
not with Eros database RLS. The E2E uses an API fixture role with `BYPASSRLS`
for the remaining cross-database receipt SQL; that grant does not prove
production-safe access and must not be carried to a pilot. The post-restart
recall check passed.

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

One direct Eros database dependency remains for selected assistant-message
read receipts and unread counts. Stock `POST /comp/chat/{session_id}/read`
marks the whole session; there is no public API operation for selected-message
receipts or unread counts. Removing this dependency without changing behavior
requires either an upstream API or a GPoint-owned receipt store and
history-based counting. The local fixture still grants its adapter role
`BYPASSRLS` to exercise this legacy path. That is test-only, is not evidence of
production-safe privileges, and must not be used for a VPS pilot.

Stock Eros has no per-user database RLS policies. Its public HTTP handlers
scope session and message operations to the authenticated Eros user, which the
adapter maps deterministically from the GPoint account. The local API-isolation
test verifies that HTTP boundary. Direct cross-service SQL does not inherit it,
so receipt SQL and the test-only `BYPASSRLS` grant remain blockers for a
shared-database pilot.

Before any pilot, independently verify stock image SHA/provenance, dedicated
database ownership and backups, Eros runtime privileges, exact provider route,
GPOINT Worker compatibility, deletion behavior, resource use on the target
VPS, and AGPL-3.0 obligations for the network service. Mac resource measurements
cannot be represented as VPS capacity evidence.
