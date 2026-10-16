# Eros migration audit (synthetic database)

Measured 2026-10-16 in disposable PostgreSQL database
`eros_gpoint_shared_synth_20261016` after applying the current 18 GPoint Prisma
migrations, AGATA Alembic through `agata0011`, and Eros migrations `0..71`.
Sizes are `pg_total_relation_size` on mostly empty test tables, including their
indexes; they are not estimates of production data volume.

| `engine` table | Total size | Purpose |
| --- | ---: | --- |
| `_sqlx_migrations` | 72 kB | Eros-only SQLx migration ledger. |
| `character_insights` | 16 kB | Current extracted character-state fields. |
| `character_insights_events` | 56 kB | Append-only character insight extraction history. |
| `character_insights_snapshot` | 24 kB | Versioned character-insight snapshots. |
| `chat_images_events` | 48 kB | Image generation/edit request and result events. |
| `chat_messages` | 120 kB | User and assistant conversation messages. |
| `chat_session_members` | 16 kB | Membership for multi-participant sessions. |
| `chat_sessions` | 40 kB | Conversation session ownership and state. |
| `chat_turn_queue` | 32 kB | Durable chat-turn queue, claims, and retry state. |
| `chat_vision_events` | 48 kB | Vision input and processing events. |
| `companion_affinity` | 40 kB | Current relationship/affinity state per user and persona instance. |
| `companion_affinity_events` | 48 kB | Relationship state transition history. |
| `companion_decision_events` | 56 kB | Companion decision and orchestration event history. |
| `companion_insights_events` | 56 kB | Extracted companion insight change history. |
| `companion_insights_snapshot` | 24 kB | Current companion insight snapshot. |
| `companion_memories` | 1112 kB | Relationship/profile memories with `vector(512)` embeddings. |
| `error_handling_config` | 32 kB | Configured response to classified generation errors. |
| `human_insights` | 56 kB | Structured human/user insight state. |
| `human_insights_snapshot` | 24 kB | Versioned human insight snapshots. |
| `llm_generations` | 32 kB | LLM generation audit and usage records. |
| `persona_genomes` | 16 kB | Shared persona definitions and authored prompt data. |
| `persona_instance_items` | 32 kB | User/persona-instance-specific persona items. |
| `persona_instances` | 32 kB | User-owned active persona relationships. |
| `persona_story_events` | 24 kB | Persona story event history. |
| `persona_story_insights` | 24 kB | Current extracted persona-story insights. |
| `persona_story_memories` | 1104 kB | Story-scoped memories with vector indexes. |
| `user_insights` | 16 kB | Structured user insight state. |
| `user_insights_events` | 56 kB | User insight extraction history. |
| `user_insights_snapshot` | 24 kB | Versioned user insight snapshots. |
| `world_enrollments` | 8192 bytes | Enrollment and feature state in the world layer. |
| `world_memories` | 1104 kB | World-scoped memory fragments with vector indexes. |
| `world_post_comments` | 24 kB | Comments attached to world posts. |
| `world_posts` | 40 kB | Generated/published world posts. |
| `world_states` | 16 kB | World state and scheduler checkpoints. |
| `world_worldviews` | 16 kB | Current world/persona worldview state. |

**Count:** 35 tables in `engine`, all with RLS enabled; none used
`FORCE ROW LEVEL SECURITY` in Eros. AGATA's schema separately had 39 tables,
with 38 forced-RLS tables after its current migration chain.

`public._sqlx_migrations` is separate: 32 kB in this fixture, with exactly one
synthetic sentinel row at version `424242`, `relrowsecurity=false`, and `SELECT`
grants for the synthetic `anon` and `authenticated` roles. After Eros
migrations, the row, RLS state, and both grants were unchanged. GPoint's
`public._prisma_migrations` remained at 18 applied revisions; AGATA's
`agata.alembic_version` remained `agata0011`. The `vector` extension remained
installed in `public`. The representative public GPoint RLS fixture retained
both `relrowsecurity` and `relforcerowsecurity` and its authenticated grant.

The GPoint Eros gateway adds `engine.eros_inference_jobs` through the GPoint
Prisma migration `20261016120000_eros_worker_queue`. Its schema is owned by the
GPoint repository, not by the upstream Eros migration chain. It stores bounded,
short-lived bridge requests, lease state and responses. The migration now
enables and forces RLS with a transaction-local `app.tenant_id` policy. A
disposable PostgreSQL check using a non-superuser role verified that missing
tenant context sees no rows, tenant A sees only its own row, and tenant B cannot
see or update tenant A's row.

**Follow-up checks (2026-10-16):** a fresh Eros-only database applied all 72
Eros migrations and a repeat migration invocation succeeded. A separate
disposable database applied all 19 current Prisma migrations and all 11 AGATA
Alembic revisions; repeating both deploy commands was a no-op. That database
also retained a synthetic `public._sqlx_migrations` sentinel and pgvector
0.8.6. The fresh Eros-only run again had 35 `engine` tables; its private SQLx
ledger measured 64 kB (relation-size estimates can vary between empty fixtures).
The exact combined order with `eros_inference_jobs` present before Eros
bootstrap remains NOT_VERIFIED. The only locally available Eros binary
predates the queue-only startup guard and failed closed with
`engine tables exist without engine._sqlx_migrations`. Rebuilding the patched
binary was blocked when the pinned Rust toolchain image could not fetch its
anonymous registry token (TLS EOF). No production database was accessed.

The expected combined schema has 36 `engine` tables: the 35 Eros tables above
plus this GPoint-owned queue table. A fresh full-chain run reaching that exact
combined state is still required. The existing count/size table above reflects
the measured Eros-only fixture, not the queue table.

**Migration boundary:** this proves fresh Eros bootstrap coexists with the
current synthetic Prisma/Alembic schemas. An older Eros installation with
`engine.*` tables but only a public SQLx ledger is rejected before migration;
automatic adoption is intentionally not implemented because blindly treating
or moving a shared public ledger could corrupt another application's history.
