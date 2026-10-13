# GPoint Codex direct chat with AGATA Implementation Plan

> **For agentic workers:** Execute inline in the current session, task by task. Steps use checkbox syntax.

**Goal:** Connect local Codex to one allowlisted OWNER's existing GPoint conversation through the production messages API and AGATA pipeline.

**Architecture:** Add short device authorization in the GPoint API/Mini App. Persist only hashed, conversation-scoped credentials in the existing GPoint database. A local MCP server stores credentials in macOS Keychain and calls only the existing history/send routes.

**Tech Stack:** NestJS/Fastify, Prisma/PostgreSQL, React Mini App, TypeScript, MCP SDK, macOS Keychain.

**Spec:** docs/superpowers/specs/2026-10-13-gpoint-codex-agata-direct-chat-design.md

## Global Constraints

- No second AGATA, relationship, chat ledger, memory store or generation pipeline.
- No changes to Persona, Naturalness V1, memory semantics, provider or Mac Worker.
- Codex credential is bound to exactly one user and creator; deny all API routes except that creator's message history/send.
- Require GPOINT_CODEX_OWNER_USER_IDS; absent config fails closed. ADMIN and creator-owner status are not substitutes.
- No secrets, codes or dialogue bodies in logs, environment variables or chat output.
- No production deploy or live inference.

## Review Focus

- Device-code brute force, expiry and replay — auth tests.
- Route/creator scope escape — API scope tests.
- Refresh/revocation race — credential transaction tests.
- Duplicate/reordered sends — MCP flow tests.
- Secret leakage — credential-store and MCP output/log tests.

---

### Task 1: Scoped authorization and credential persistence

**Files:** apps/api/src/codex-chat-auth.ts; apps/api/src/session.ts; apps/api/src/main.ts; apps/api/src/messages.ts; packages/db/prisma/schema.prisma; new Prisma migration; focused apps/api tests.

**Interfaces:** resolveCodexMessageSession(bearer, creatorId, action) returns userId/connectionId only for active scoped read/send. assertCodexOwner(userId) uses only the configured allowlist. Persist userId, creatorId, hashed access/refresh credentials, expiry and revocation metadata; never dialogue content.

- [x] Add failing tests for allowlist, approval, creator/history mismatch, code expiry/replay and route scope.
- [x] Run focused tests and verify expected failures.
- [x] Implement device start/approve/exchange, credential refresh/revocation, scoped auth and migration without changing Telegram sessions.
- [x] Add cross-user, cross-creator, non-message-route, expired and revoked scope tests; refresh replay is guarded by atomic one-use rotation.
- [x] Run API tests/typecheck and Prisma schema validation. Live-database migration execution awaits normal release preparation.

### Task 2: OWNER-only Mini App controls

**Files:** apps/api/src/main.ts; apps/api/src/codex-chat-auth.ts; apps/miniapp/src/api/client.ts; apps/miniapp/src/messages-ui.tsx; apps/miniapp/src/style.css; focused Mini App tests.

- [x] Add status/approve/revoke endpoints using ordinary authenticated sessions and verify approved creator plus existing history.
- [x] Add a compact control to the current chat header; require code entry and display connected/revoked state.
- [x] Run Mini App tests, typecheck/build, and targeted 390 px Playwright control flow for hidden, approve and revoke states with mocked API only.

### Task 3: Local Codex MCP bridge

**Files:** packages/gpoint-codex-mcp/package.json; src/server.ts; src/keychain.ts; test/server.test.ts; pnpm-lock.yaml; .codex/config.toml; .agents/skills/gpoint-agata-direct-chat/SKILL.md; AGENTS.md.

- [x] Add failing tests for device lifecycle, exact response passthrough, history reads, ordered sends, pending/no-duplicate behavior and TLS origin constraints.
- [x] Verify expected failures using a fake API and Keychain adapter.
- [x] Implement MCP SDK stdio tools for connect, finish, history, send and revoke. Keep credentials in Keychain, never repo/config.
- [x] Configure project MCP startup and add exact-response guidance to the tool description.
- [x] Run package tests/typecheck, local stdio MCP handshake and dummy-only macOS Keychain roundtrip; no production credentials used.

### Task 4: Security review and local handoff

**Files:** all Task 1–3 paths.

- [x] Independently review auth boundaries, device flow, query scope, rotation, revocation, injection and logging.
- [x] Fix pending-reply attribution, bounded device-record cleanup, cross-instance rate limiting and reverse-proxy rate-key findings; obtain independent follow-up review with no findings.
- [ ] Run final scoped tests/typechecks/build, git diff checks, Prisma validation and pnpm repo:clean:end after commit.
- [ ] Record pipeline equivalence, review, test evidence, no-live-inference status, migration/security risks and exact local commit in the GPoint handoff. Mark LOCAL_PENDING_RELEASE unless canonical handoff gate passes.
- [ ] Do not publish, dispatch hosted Actions, deploy or run live inference.

### Post-release E2E gate (must pass before VERIFIED)

- [ ] After normal release and separate permission for live inference, prove Codex uses the same production `POST /messages/creator/:creatorId` and AGATA Worker/provider configuration as Telegram.
- [ ] Verify the same user, creator, conversation_id, relationship, Persona/profile and long-term memory context in both channels without reading another user's data.
- [ ] Send from Codex and find it in Telegram; send from Telegram and find it in Codex.
- [ ] Send concurrently through both channels and verify request-to-response correlation. If AGATA returns pending without a correlation identifier, report pending; never attribute an adjacent reply.
- [ ] Record evidence and mark VERIFIED only after every check passes; otherwise retain LOCAL_PENDING_RELEASE/BLOCKED with precise missing gates.
