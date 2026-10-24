# ME-36 Permanent Eros Service Implementation Plan

> **For agentic workers:** Execute this plan task-by-task. Each task must keep GPoint as the only user-facing API and preserve synthetic-only routing until cutover approval.

**Goal:** Install stock Eros as a persistent V5-managed GPoint service, move AGATA-owned policy/history functions to GPoint without data loss, and retain safe rollback on the current VPS.

**Architecture:** The existing GPoint API remains the sole client entry point and owns consent, age assurance, history, authorization, and routing. Stock Eros runs as a persistent internal service with its own logical database and least-privilege roles on the existing PostgreSQL cluster only after exact migration, isolation, backup, restore, and RAM tests pass; otherwise V5 uses a dedicated PostgreSQL container. Qwen3 stays on Mac behind an automatically recovered SSH reverse tunnel. V5 owns the immutable Eros image, runtime configuration, migration, backup, rollback, and health evidence.

**Tech Stack:** Existing GPoint TypeScript/NestJS API, Prisma, Python/FastAPI AGATA service, stock Eros Docker image, Docker Compose, PostgreSQL, existing V5 GitHub Actions and GPoint Mac runner.

**Spec:** Owner-approved ME-36 architecture in this conversation (2026-10-24).

## Global Constraints

- Do not modify stock Eros source or introduce a second always-on GPoint API.
- Do not change production traffic routing beyond the explicit synthetic allowlist during initial operation.
- Do not disable AGATA until GPoint owns and verifies replacement behavior and data.
- Do not delete AGATA database, volumes, backups, secrets, or images before restore verification and owner acceptance.
- Do not use swap as required capacity; recheck current RAM and disk immediately before V5 Apply.
- Keep Ollama/Qwen3 and Codex Local on Mac; never expose Ollama publicly.
- Preserve exact SHA, immutable GHCR receipt/digest, security gates, OWNER approvals, backup/rollback, and V5 Deploy.
- Store secrets only through the established protected Mac/VPS mechanism; never write secrets to Git, logs, chat, or Linear.

## Review Focus

- A stale or absent consent/disclosure/age-assurance state must fail closed before a chat request.
- AGATA export/import interruption or duplicate retry must preserve every source row and be idempotent.
- Cross-user and cross-tenant access must fail at both the GPoint API and Eros database boundary.
- Eros startup or migration failure must leave the existing GPoint API and database untouched and restore the prior Eros state.
- SSH tunnel interruption must recover without exposing Ollama outside the private path.
- Low RAM or disk must fail preflight before any service or database mutation.

---

### Task 1: Establish persistent Eros/PostgreSQL topology

**Files:**
- Modify `infra/production/docker-compose.eros-pilot.yml` into the permanent Eros overlay or replace it with a permanent overlay selected by V5.
- Modify `infra/production/eros-pilot-init.sh` only if required for isolated logical-database roles.
- Modify `services/eros-engine/local-test-init.sql` and `services/eros-engine/run-local-e2e.sh` for production-equivalent migration ordering tests.
- Test `scripts/eros-pilot-deployment.test.mjs`, Eros migration tests, and disposable PostgreSQL integration tests.

**Acceptance:** One GPoint API serves all users; only Eros is a new persistent app service. Stock migrations succeed in a dedicated database on the existing PostgreSQL server, with runtime credentials unable to connect to or read GPoint/AGATA databases. If the host cannot provide this isolation, the permanent V5 target uses a dedicated Eros PostgreSQL container and records the measured RAM cost.

### Task 2: Move AGATA policy and history ownership to GPoint

**Files:**
- Add GPoint Prisma models/migrations under `packages/db/prisma/` for versioned disclosure receipts, consent, age-assurance state, and archived legacy conversations/messages.
- Modify `apps/api/src/messages.ts`, `apps/api/src/agata-client.ts`, and focused API services so GPoint reads and writes those records and routes only exact synthetic identities to Eros.
- Add an AGATA export operation under `services/agata/app/api/internal.py` and migration support under `services/agata/migrations/` if its current API cannot produce a complete consistent export.
- Add migration/import and reconciliation tooling under `scripts/` with tenant scoping, row counts, stable identifiers, idempotency, and secret-free output.
- Add tests in `apps/api/test/`, `services/agata/tests/`, and `tests/` for fail-closed policy, complete data parity, idempotent retries, and user isolation.

**Acceptance:** Existing history, consent, disclosure receipts, memory choices, age-assurance evidence, and timestamps are copied and read back with source-to-target counts/checksums by tenant. GPoint preserves source AGATA data until owner accepts restore evidence. Old conversations remain readable; new synthetic conversations use Eros. No chat bypasses consent or age checks.

### Task 3: Integrate Eros into the existing GPoint V5 release

**Files:**
- Modify `.github/workflows/gpoint-deploy.yml`, `scripts/automated-eros-pilot-release.mjs`, `scripts/eros-pilot-deployment.mjs`, and release tests to make Eros a permanent service in the normal GPoint V5 candidate.
- Remove the second `eros-pilot-api` service and all `EROS_PILOT_ENV_B64`-only startup assumptions.
- Add permanent secret/config provisioning using the established protected Mac/VPS release mechanism.
- Add Eros-specific backup, migration, restore, rollback, health, image digest/provenance, and restart-after-reboot checks while preserving all existing GPoint gates.
- Keep the Mac Ollama reverse tunnel under an automatically restarting, health-checked mechanism and verify recovery after a bounded interruption.

**Acceptance:** Prepare and Deploy bind every Eros image and migration to the exact SHA and immutable receipt. Failed Eros apply rolls back only Eros resources; it cannot recreate, migrate, or stop GPoint API/Worker/Bot. Secrets remain outside CI logs/Git and are available for restore.

### Task 4: Prove isolation, resource capacity, and recovery

**Files:**
- Extend `services/eros-engine/run-local-e2e.sh`, `apps/api/test/eros-adapter.test.ts`, and `scripts/eros-pilot-deployment.test.mjs`.
- Add an operator-readable Eros deployment/restore checklist to `services/eros-engine/README.md` and V5 automation documentation.

**Acceptance:** Two synthetic users cannot read, mutate, or infer one another's sessions, memories, embeddings, or queue jobs; direct DB roles cannot access other databases; API/Worker credentials and runtime network boundaries match the design. Backup→restore→rollback pass on disposable state. Current VPS RAM/disk has the configured safe reserve with GPoint/Worker running and GSales stopped; no swap dependency.

### Task 5: Deploy permanent service and retire AGATA safely

**Files:** No source changes beyond Tasks 1–4; use existing V5 workflows and Linear ME-36.

**Acceptance:** Publish only after exact SHA approval and Prepare authorization. Deploy the permanent Eros service through V5 with synthetic-only allowlists. Verify health, Mac Qwen3 embedding, synthetic memory write/recall, restart persistence, archive access, and rollback. Disable AGATA only after functional and data restore evidence is accepted; preserve its data and recovery path. Do not enable real-user routing without separate explicit approval.
