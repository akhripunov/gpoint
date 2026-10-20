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

- Stock server startup currently requires `OPENROUTER_API_KEY` before loading
  its model config, even if the configured route is custom-only. Never use a
  production value for local tests. The local Compose harness supplies only a
  synthetic startup sentinel, uses a Docker-internal network with no egress,
  and configures no hosted provider route. The sentinel is not a credential and
  must never be copied to a deployed service.
- Eros SQLx migrations use the default `public._sqlx_migrations` ledger. Keep
  Eros in a dedicated database; do not share the GPoint or AGATA database.
- Stock Eros's relationship archive endpoint soft-deletes sessions and removes
  some relationship memory, but deliberately retains transcript history.
  GPoint currently has no Eros user-data erasure path. A release pilot is
  blocked until a tested deletion adapter covers transcript, memory, summaries,
  embeddings and queued/background writes without changing Eros source.
- The stock background dream/classification flow has a race with relationship
  archive that the archived patch `0004` addressed. Treat this as an unresolved
  deletion-safety risk until reproduced against stock Eros and either resolved
  by an upstream-compatible external lifecycle mechanism or accepted by OWNER.
- The test allowlist remains synthetic and local. No broad production routing
  or AutoWrite/memory-policy change is made here.

## Local verification boundary

`run-local-e2e.sh` uses two disposable PostgreSQL databases, internal-only
Docker networking, synthetic identities/data, and a deterministic local Worker
fixture. The run passed a 15-turn dialogue, server age/consent gates, GPoint API
history, stock Eros extraction and durable recall, a 14-message gap, provider
context, existing GPOINT queue routing, and persistence of history and durable
memory rows after Eros restart. The recall response and provider-context checks
ran before restart; recall after restart was not tested.
This is a stock-engine integration E2E with a fixture provider, not real Codex
inference. It does not prove Mac Product Worker availability, VPS resource
needs, or production performance.

The pinned stock source compiled with the isolated `rust:1.98` build image; its
offline `print-openapi` command and full local Compose E2E passed. Evidence:
`LOCAL_E2E_STOCK_EROS_IMAGE=PASS`, `LOCAL_E2E_15_TURN_DIALOGUE=PASS`,
`LOCAL_E2E_MEMORY_EXTRACTION=PASS`, `LOCAL_E2E_DURABLE_MEMORY=PASS`,
`LOCAL_E2E_MEMORY_RECALL_RESPONSE=PASS`,
`LOCAL_E2E_PROVIDER_CONTEXT=PASS`, `LOCAL_E2E_RESTART_HISTORY=PASS`, and
`LOCAL_E2E_CLEANUP=PASS`. Real inference was not run; no paid API calls were
made. RLS tenant-isolation integration was not tested: the disposable Eros
fixture grants `BYPASSRLS` to its API/runtime roles to exercise the current
direct database adapter. That fixture is not evidence of acceptable production
privileges.

The current GPoint adapter performs direct `INSERT`/`UPDATE`/`SELECT` operations
against Eros `persona_genomes` and `persona_instances`. This is an integration
boundary that remains to be replaced by, or explicitly justified against,
stock Eros APIs and a least-privilege role. No such production role or grants
have been approved or tested.

Before any pilot, independently verify stock image SHA/provenance, dedicated
database ownership and backups, Eros runtime privileges, exact provider route,
GPOINT Worker compatibility, deletion behavior, resource use on the target
VPS, and AGPL-3.0 obligations for the network service. Mac resource measurements
cannot be represented as VPS capacity evidence.
