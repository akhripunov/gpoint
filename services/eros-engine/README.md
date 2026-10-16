# Eros Engine V1 local integration

Upstream source is pinned to `etherfunlab/eros-engine` v2.2.1 at
`d1a140083321687212fb7b17c09cc0c3c9639f5e`. Keep downstream changes as ordered,
reproducible patches; do not edit a mutable image or apply migrations to the
GPoint database. The upstream source is AGPL-3.0-only; retain its license and
review AGPL §13 obligations before operating a modified network service.

`patches/0001-provider-and-startup-gates.patch` removes the
unrelated startup dependency on `OPENROUTER_API_KEY` for fully custom-only
configs, while requiring that key at startup for unqualified or explicit
OpenRouter slugs and whenever an uncovered known task would use Eros’s compiled-in
OpenRouter fallback. Custom providers still require their
own `${NAME}_API_KEY`; active `chat_vision` is included in the implicit-fallback
gate even though it is not a `ChatRequest` task. No placeholder provider keys
are used.

`patches/0002-isolate-sqlx-ledger-in-engine-schema.patch` pins the Eros
connection search path to `engine,public`, creates `engine` before SQLx
migrations initialize, and keeps the Eros migration ledger at
`engine._sqlx_migrations`. Its migration lockdown targets that ledger when it
exists and leaves SQLx's default public test ledger untouched when the private
ledger is absent. Startup refuses to rerun migrations if `engine.*` tables
exist without the private ledger. This prevents migration version collisions
and permission changes to a separate application's `public._sqlx_migrations`
table. This bootstrap supports a fresh Eros `engine` schema; adopting a prior
Eros install whose history is only in `public._sqlx_migrations` is deliberately
not automatic and currently fails closed.

`patches/0003-gpoint-worker-bridge-identity.patch` adds an opaque bridge
identity to Eros chat requests. On the GPoint custom provider path only, the
bridge identity replaces Eros's caller-controlled OpenAI `user` audit field.
The Eros server signs a 60-second HS256 JWT from trusted tenant, user, session,
persona-instance and triggering user-message IDs. The GPoint gateway validates
issuer, audience, signature, lifetime and UUID claims, then checks the signed
tenant against its server-configured Eros tenant. Other custom providers and
built-in OpenRouter keep their previous audit behavior.

Migration coexistence was tested in a disposable PostgreSQL database after all
18 current GPoint Prisma migrations and all 11 AGATA Alembic revisions. Eros
then recorded 72 revisions (`0..71`) in `engine._sqlx_migrations`; the public
sentinel ledger, Prisma ledger, Alembic head, `agata` schema, and public pgvector
installation remained intact. See [MIGRATION_AUDIT.md](MIGRATION_AUDIT.md) for
the full engine table inventory and measured sizes.

Apply from a clean, exact v2.2.1 checkout:

```sh
services/eros-engine/apply-patches.sh /path/to/eros-engine-v2.2.1
```

The wrapper verifies the exact upstream revision, a clean source tree, and all
patches before applying any of them.

After applying, run the upstream Rust tests for `eros-engine-server` and
`eros-engine-llm`. The patch is intentionally separate from the upstream
checkout so rebasing Eros is explicit and reviewable.

## Current local boundary

The GPoint API has a distinct Eros-only OpenAI-compatible bridge and
`engine.eros_inference_jobs` queue. It uses separate credentials, signed
identity claims, tenant-scoped queue queries, a bounded queue, one-attempt
leases, deadlines, cancellation checks and no prompt logging. The Mac Worker
uses a distinct `EROS` target and `slot-04`; local test mode requires the exact
target set `{EROS}` and cannot poll GPOINT, GSALES or LAB. The shared supervisor
still enforces one global inference slot at `N=1`. An EROS poll failure marks
only its own lane degraded; it does not degrade GPOINT/GSales readiness.
Production installation/configuration is unchanged and not authorized here.

The signed `creator_id` is read from the active `(instance_id, owner_uid)` row
in Eros PostgreSQL, then carried through the short-lived bridge identity and
persisted on the EROS queue row. A client cannot choose it. EROS request IDs are
global idempotency keys: replayed requests attach to one inference, and a
disconnect cancels that logical request for all replays. The cancellation
check stops the Worker before it submits a result; deadlines remain the bounded
fallback if the Worker is unreachable.

Local chat configuration is in `test-model-config.toml`. Eros's provider key
`GPOINT_API_KEY`, the API's `EROS_BRIDGE_API_KEY`, and the Mac Worker's
`EROS_WORKER_TOKEN` are supplied through private local environment files.
The same secret bytes must be configured under `GPOINT_WORKER_BRIDGE_SIGNING_SECRET`
for Eros and `EROS_WORKER_SIGNING_SECRET` for the GPoint API. The names differ
because each process owns its own configuration namespace. The secret must be at
least 32 bytes. Never put it in logs or handoff records.
`EROS_WORKER_TENANT_ID` must match the server-owned tenant. Do not put secrets
in the committed test configuration.

The local embedding candidate is Qwen3-Embedding-0.6B through Ollama's
`/api/embed` endpoint. The official model supports 100+
languages and configurable output dimensions through 1024; Ollama documents a
`dimensions` request field, and Eros sends `dimensions: 512`. The candidate
model ref is `qwen3-embedding:0.6b`; see [the model card](https://ollama.com/library/qwen3-embedding)
and [Ollama's embedding endpoint](https://docs.ollama.com/api/embed). The test
config routes chat to the Worker bridge and embeddings to its Ollama-compatible
endpoint; it contains no hosted provider route. Runtime/model verification is
NOT_VERIFIED: two bounded pulls of the official `ollama/ollama:0.40.5` image
failed with TLS EOF at the registry/auth endpoint. TLS verification was not
disabled; no model weights or fabricated embeddings are committed.

One synthetic chat request was verified through GPoint bridge -> isolated
EROS-only test Worker -> authenticated Codex CLI -> OpenAI-compatible response.
It completed one real Codex generation, returned a Russian response, and wrote
a completed row to the disposable PostgreSQL queue. A preceding test claim was
cancelled before inference after the temporary runtime slot pointer was
missing. No AGATA or GSales job was created or claimed.

Full Eros Engine chat/session/memory/relationship E2E remains NOT_VERIFIED.
The successful smoke called the GPoint local gateway directly; the Eros server
has not been rebuilt from the third downstream patch, and no embedding provider
could be started. The prior 72-migration synthetic audit remains valid for its
recorded fixture. The disposable gateway-test database is not a clean Eros
migration fixture and was not used to rerun the Eros migrations. The audit's
35 Eros tables are distinct from the GPoint queue table; both migration sets
would yield 36 tables in the combined local `engine` schema.
