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

- [ ] Add failing tests for allowlist, approval, creator/history mismatch, code expiry/replay and route scope.
- [ ] Run focused tests and verify expected failures.
- [ ] Implement device start/approve/exchange, credential refresh/revocation, scoped auth and migration without changing Telegram sessions.
- [ ] Add cross-user, cross-creator, non-message-route, expired, revoked and refresh-replay tests.
- [ ] Run API tests/typecheck, Prisma validation and migration checks.

### Task 2: OWNER-only Mini App controls

**Files:** apps/api/src/main.ts; apps/api/src/codex-chat-auth.ts; apps/miniapp/src/api/client.ts; apps/miniapp/src/messages-ui.tsx; apps/miniapp/src/style.css; focused Mini App tests.

- [ ] Add failing tests for allowlist-gated visibility, selected-chat binding, approval and revocation states.
- [ ] Verify expected failures.
- [ ] Add status/approve/revoke endpoints using ordinary authenticated sessions and verify approved creator plus existing history.
- [ ] Add a compact control to the current chat header; require code entry and display connected/disconnected state.
- [ ] Run Mini App tests, typecheck/build, and responsive header checks at 360–430 px.

### Task 3: Local Codex MCP bridge

**Files:** packages/gpoint-codex-mcp/package.json; src/server.ts; src/keychain.ts; test/server.test.ts; pnpm-lock.yaml; .codex/config.toml; .agents/skills/gpoint-agata-direct-chat/SKILL.md; AGENTS.md.

- [ ] Add failing tests for device lifecycle, exact response passthrough, history refresh, ordered sends, pending/no-duplicate behavior, revocation and secret-free output/logs.
- [ ] Verify expected failures using a fake API and Keychain adapter.
- [ ] Implement MCP SDK stdio tools: start_connection, finish_connection, read_history, send_message. Keep credentials in Keychain, never repo/config.
- [ ] Configure project MCP startup and instruct Codex to send the user's exact text and present AGATA response text unchanged.
- [ ] Run package tests, typecheck/build and MCP handshake with no production credentials.

### Task 4: Security review and local handoff

**Files:** all Task 1–3 paths.

- [ ] Independently review auth boundaries, device flow, query scope, rotation, revocation, injection and logging.
- [ ] Fix material findings and rerun affected tests.
- [ ] Run applicable API/Mini App tests, typechecks, builds, git diff checks, migration checks and pnpm repo:clean:end.
- [ ] Record pipeline equivalence, review, test evidence, no-live-inference status, migration/security risks and exact local commit in the GPoint handoff. Mark LOCAL_PENDING_RELEASE unless canonical handoff gate passes.
- [ ] Do not publish, dispatch hosted Actions, deploy or run live inference.
