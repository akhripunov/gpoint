# AGATA Core for GPoint

This is the channel-neutral Python dialogue, character/personality and memory core. It is a separate process from the TypeScript GPoint API and connects to the existing GPoint PostgreSQL database using only the `agata` schema. The old AGATA database and its data are not migrated.

## Local configuration

Use the `agata` profile in `infra/docker/docker-compose.yml`. Provide the `AGATA_*` settings in a local, untracked `.env`; generate independent random keys for message encryption and each HMAC domain. Do not copy the old AGATA `.env` or reuse its credentials. The development API is bound to `127.0.0.1:8001`; it is not bound to a public interface.

Required settings are `AGATA_DATABASE_URL`, `AGATA_TENANT_ID`, `AGATA_INTERNAL_API_TOKEN`, `AGATA_MESSAGE_AEAD_KEY`, `AGATA_REQUEST_FINGERPRINT_HMAC_KEY`, `AGATA_RETRIEVAL_QUERY_HMAC_KEY`, `AGATA_RESPONSE_FINGERPRINT_HMAC_KEY`, `AGATA_SHADOW_CANDIDATE_HMAC_KEY`, and provider keys. Key values are base64-encoded; the AEAD key must decode to 32 bytes and HMAC keys to at least 32 bytes.

Apply only this service's Alembic baseline explicitly with `alembic upgrade head`; GPoint Prisma migrations do not own this schema. The baseline enables forced tenant RLS for AGATA tables, installs `vector`/`pgcrypto` only if absent, and seeds the configured tenant and conservative global policy. It does not create or alter GPoint tables. Never run it against production as part of this task.

Internal routes are authenticated with a service bearer token. `PUT /internal/v1/characters/{gpoint_character_id}` synchronizes immutable persona versions, `PUT /internal/v1/users/{gpoint_user_id}/consent` synchronizes a consent/age/disclosure snapshot, and `POST /internal/v1/respond` accepts one message and loads all history, persona, and memory within AGATA. The worker runs as `python -m app.memory.worker` and owns summary, extraction, promotion, and embedding outbox jobs.

Production Compose entries are behind the explicit `agata` profile and are not started by the existing GPoint release flow. They require a separately reviewed release/migration enablement step and provisioned `AGATA_*` secrets. This task does not deploy or migrate any database.
