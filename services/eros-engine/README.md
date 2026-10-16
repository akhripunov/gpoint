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

After applying, run the upstream Rust tests for `eros-engine-server` and
`eros-engine-llm`. The patch is intentionally separate from the upstream
checkout so rebasing Eros is explicit and reviewable.

## Current boundary

The existing Mac Worker Gateway is AGATA's tenant-scoped encrypted lease queue,
not an OpenAI-compatible endpoint. It accepts only jobs already created by the
AGATA response pipeline. No generic Eros prompt/response route exists. A safe
Eros-to-Worker bridge therefore requires a separately authorized AGATA queue
extension or a dedicated local provider runtime; neither is implemented here,
and no AGATA files or production jobs were changed.

The local embedding candidate is Qwen3-Embedding-0.6B through Ollama's
OpenAI-compatible `/v1/embeddings` endpoint. The official model supports 100+
languages and configurable output dimensions through 1024; Ollama documents a
`dimensions` request field, and Eros sends `dimensions: 512`. The candidate
model ref is `qwen3-embedding:0.6b`; see [the model card](https://ollama.com/library/qwen3-embedding)
and [Ollama's embedding endpoint](https://docs.ollama.com/api/embed). The test
config sets a custom local default for all chat tasks and points embeddings to
local Ollama; it contains no hosted provider route. Runtime/model verification remains NOT_VERIFIED because
the local Docker registry request for `ollama/ollama:0.40.5` ended in EOF. No
model weights are committed; retry after registry access is restored and pin
the resolved image/model identities before relying on this provider.

The complete chat E2E remains blocked. GPoint's Mac Worker Gateway accepts
only AGATA-created tenant-scoped encrypted leases; it has no generic prompt
submission contract. Do not enqueue synthetic AGATA jobs or mutate AGATA. A
separately authorized AGATA gateway contract is required to use that Worker.
The current local Ollama runtime is also unavailable because its image pull
failed; after registry access returns it can serve a local quality-only E2E,
but it does not satisfy the Mac Worker bridge requirement. Two independent
synthetic users successfully created isolated sessions and received HTTP 403
when crossing session ownership. No messages or provider inference were
generated in this run.
