# GPoint Codex direct chat with AGATA Implementation Plan

> **For agentic workers:** Execute inline in the current session, task by task. Steps use checkbox syntax for tracking.

**Goal:** Connect local Codex to one existing allowlisted OWNER GPoint conversation using Telegram identity proof and the production AGATA message route.

**Architecture:** Reuse the existing Telegram bot's private `/start` command and authenticated bot→API internal channel to redeem a short-lived challenge. GPoint binds the verified OWNER to one existing approved creator conversation and issues existing scoped credentials; local MCP uses macOS Keychain and only existing message history/send routes.

**Tech Stack:** grammY, NestJS/Fastify, Prisma/PostgreSQL, TypeScript, MCP SDK, macOS Keychain.

**Spec:** `docs/superpowers/specs/2026-10-13-gpoint-codex-agata-direct-chat-design.md`

## Global Constraints

- Reuse the current GPoint Telegram bot; create no new bot, AGATA, relationship, conversation, memory store, test database, or hidden admin route.
- No Codex-specific control, status, approval, or revoke UI remains in GPoint Mini App.
- `GPOINT_CODEX_OWNER_USER_IDS` is an exact fail-closed allowlist; API identity comes only from Telegram's private-chat update forwarded over `TELEGRAM_WEBHOOK_SECRET`.
- Telegram/device challenge expires after 5 minutes, stores only hashes, and grants one user and one creator after one successful redemption.
- Codex credentials can read/send only the exact bound creator's existing message routes; revocation is available to that scoped Codex credential.
- Do not alter Persona, Identity Core, Naturalness V1, memory semantics, relationship state, provider, Mac Worker or Telegram login.
- No credentials, challenge links/tokens, message bodies, prompts or provider results in persistent logs or configuration.
- Tests use fake APIs and never invoke live inference. No production deploy, migration apply, or live inference.
- Keep ME-27 `LOCAL_PENDING_RELEASE` until the existing handoff gate and canonical release flow establish otherwise; never call integration `VERIFIED` without the authorized post-release E2E.

## Review Focus

- Telegram link opened outside a private chat or by a sender whose chat ID differs — API/bot tests must deny and leave authorization pending.
- Copied, replayed, expired, malformed, or brute-forced challenge — API tests must prove no grant, one-time redemption and bounded rate limits.
- Telegram ID missing from OWNER allowlist, creator slug unapproved, selected user missing the `CreatorOwner` relation, or no existing AGATA history for that same user/creator — callback must fail generically without granting or enumerating account data. All predicates are required together.
- Codex credential targets another creator, unsupported API route, revoked scope, or refresh replay — auth integration tests must deny.
- Pending or concurrently arriving AGATA replies — MCP returns exact completed text or explicit pending and never claims an adjacent reply.

---

### Task 1: Telegram bot identity-proof forwarding

**Files:**
- Create: `apps/bot/src/codex-start.ts`
- Modify: `apps/bot/src/main.ts`
- Test: `apps/bot/test/codex-start.test.mjs` (new)

**Interfaces:**
- Consumes: Telegram grammY `/start <payload>` updates; `GPOINT_INGEST_URL`; existing `TELEGRAM_WEBHOOK_SECRET`.
- Produces: `forwardCodexStart(payload, chat, from, fetchImpl)` sends one JSON request to the approval callback only for `chat.type === 'private'`, numeric sender ID, and `chat.id === from.id`; production requires the external Caddy-routed `/api/internal/telegram/update` base and keeps `/api` on derived paths, while local direct API development uses unprefixed paths. It returns a generic outcome and never logs the payload.

- [ ] **Step 1: Add failing bot tests** for valid private owner-shaped input forwarding; group chat denial; sender/chat mismatch denial; absent secret; API failure; redirect refusal when the shared webhook secret is attached; no token in captured logs or reply.
- [ ] **Step 2: Run `pnpm --filter @gpoint/bot test`** and verify the new behavior tests fail before implementation.
- [ ] **Step 3: Implement payload parsing and callback forwarding** in the existing `/start` handler; keep generic GPoint welcome behavior for normal `/start` and other bot commands unchanged. Forward only the payload and Telegram's actual `chat`/`from` fields with existing internal secret.
- [ ] **Step 4: Run `pnpm --filter @gpoint/bot test && pnpm --filter @gpoint/bot typecheck`**; all bot tests and typecheck must pass.

### Task 2: Telegram challenge and scoped API approval

**Files:**
- Modify: `apps/api/src/codex-chat-auth.ts`, `apps/api/src/codex-chat.ts`, `apps/api/src/main.ts`, `apps/api/src/messages.ts`, `apps/api/package.json`, `packages/db/prisma/schema.prisma`, `packages/db/prisma/migrations/20261013120000_codex_chat_connection/migration.sql`
- Test: `apps/api/test/codex-chat-auth.test.ts`, `apps/api/test/codex-chat.test.ts` (new), `apps/api/test/integration.test.ts`

**Interfaces:**
- Consumes: `POST /auth/codex/device/start { creatorSlug }`; bot callback `{ payload, chat:{id,type}, from:{id} }` over HTTPS; existing `TELEGRAM_WEBHOOK_SECRET`, OWNER allowlist, source rate limiter and AGATA integration.
- Produces: device start `{ deviceCode, telegramLink, expiresIn }`; exchange `{ status:'pending'|'expired'|'connected', ...scopedTokens }`; callback `{ approved:true }` or generic denial. The additive migration renames the challenge digest column; raw challenge is never stored.

- [ ] **Step 1: Add failing API tests** for private-chat proof, exact `chat.id == from.id`, Telegram→GPoint identity mapping, OWNER allowlist, APPROVED creator slug, `CreatorOwner` relation, existing history for the same OWNER, missing bot settings, expiry, replay/atomic one-use, source rate limiting, generic failure/no enumeration, and one-time exchange.
- [ ] **Step 2: Run the focused API tests** and confirm they fail for the missing Telegram approval path and current Mini App user-code approval contract.
- [ ] **Step 3: Implement `POST /auth/codex/device/start`** accepting a bounded creator slug, mint 256-bit Telegram challenge and device secret, store only hashes with five-minute expiry, preserve the cross-instance start rate limit, and return a `t.me` deep link for the configured existing bot username. Fail closed when required bot/OWNER/proxy configuration is absent.
- [ ] **Step 4: Implement `POST /auth/codex/telegram/codex-approve`** protected by the existing webhook secret and reached through the bot's HTTPS GPoint API URL; validate the payload, private chat and matching numeric sender/chat IDs, map the real Telegram ID to `User`, check exact OWNER allowlist, APPROVED creator, that OWNER's `CreatorOwner` relation, and `agata.listMessages(userId, creatorId, {limit:1})` for the same OWNER; then atomically grant the pending authorization once. Use generic denial responses. Do not weaken or bypass any predicate.
- [ ] **Step 5: Remove Mini App approval/status API endpoints** and retain no GPoint user-session approval bypass. Add scoped `POST /auth/codex/connection/:creatorId/revoke` authorization that accepts the current scoped bearer only for its bound creator, verifies allowlist/expiry/revocation state, then revokes it. Existing `/messages/creator/:creatorId` scope remains unchanged.
- [ ] **Step 6: Register the new API test file in the API test script, then run `pnpm --filter @gpoint/api exec node --import tsx --test test/codex-chat-auth.test.ts test/codex-chat.test.ts`, `pnpm --filter @gpoint/api typecheck`, and `pnpm --filter @gpoint/db exec prisma validate`**; all must pass. Run the package's regular API suite as final verification. Do not apply migrations to any database.

### Task 3: Remove all Mini App Codex UI

**Files:**
- Modify: `apps/miniapp/src/api/client.ts`, `apps/miniapp/src/messages-ui.tsx`, `apps/miniapp/src/style.css`, `tests/browser-smoke/miniapp-mocks.ts`, `tests/browser-smoke/messages.spec.ts`

**Interfaces:**
- Consumes: existing normal GPoint conversation UI and message APIs.
- Produces: no Codex-specific imports, state, query, endpoint calls, controls, copy, or CSS in Mini App. Normal conversation behavior remains unchanged.

- [ ] **Step 1: Add/adjust failing browser-smoke assertions** that owner and non-owner chats render no Codex control/panel or authorization request, while normal history/send behavior remains.
- [ ] **Step 2: Run the focused browser-smoke test** and verify the regression fails against the current control.
- [ ] **Step 3: Remove Codex status/approval/revoke imports, query, state, mutations, markup, and CSS**, plus mock routes no longer used. Do not alter the ordinary chat UI.
- [ ] **Step 4: Run Mini App tests, typecheck/build and targeted Playwright at 390px**; normal conversation flow passes and no Codex-specific UI/API activity appears.

### Task 4: Codex MCP Telegram connection flow

**Files:**
- Modify: `packages/gpoint-codex-mcp/src/index.ts`, `packages/gpoint-codex-mcp/src/main.ts`, `packages/gpoint-codex-mcp/README.md`, `packages/gpoint-codex-mcp/test/tools.test.ts`

**Interfaces:**
- Consumes: `connect_gpoint({ creatorSlug })`, `finish_gpoint_connection`, `read_gpoint_history`, `send_gpoint_message`, `disconnect_gpoint`; existing GPoint API routes and Keychain adapter.
- Produces: `connect_gpoint(creatorSlug: string)` saves `deviceCode` in Keychain and returns only the Telegram URL/expiry; finish exchanges after bot approval and persists scoped tokens; disconnect revokes using the scoped token before deleting Keychain items.

- [ ] **Step 1: Add failing MCP tests** for exact creator slug in start body, valid Telegram deep-link response without raw device secret, pending then connected finish, Keychain persistence across tool-instance recreation, expired/denied flow, exact response passthrough, history, ordered sends, pending response, and scoped revoke.
- [ ] **Step 2: Run `pnpm --filter @gpoint/gpoint-codex-mcp test`** and verify the tests fail against user-code instructions/current ephemeral module storage.
- [ ] **Step 3: Implement the revised start/finish flow**; persist pending secret and all credentials only in macOS Keychain, validate returned Telegram URL origin/shape, keep API HTTPS-only outside loopback, and make the MCP schema require the creator slug.
- [ ] **Step 4: Keep `send_gpoint_message` on existing `POST /messages/creator/:creatorId`** and return completed AGATA response text byte-for-byte; on pending, return pending and do not poll/guess/retry.
- [ ] **Step 5: Implement `disconnect_gpoint` through scoped bearer revocation** and delete local Keychain secrets only after server confirms revoke.
- [ ] **Step 6: Run MCP tests/typecheck and a local stdio handshake plus dummy-only Keychain roundtrip**; no production credentials or live inference.

### Task 5: End-to-end offline verification, independent security review and handoff

**Files:** all changes in Tasks 1–4 plus this spec/plan and the existing ME-27 Linear handoff/Release Queue.

**Interfaces:** the reviewer receives exact local commit/diff, test evidence and architecture only; no credentials, personal payloads, production access or live inference.

- [ ] **Step 1: Run scoped API/bot/MCP/Mini App tests, typechecks, build, Playwright UI smoke, Prisma validation and `git diff --check`.**
- [ ] **Step 2: Request an independent security review** of one-time Telegram proof, bot→API trust, OWNER/creator scope, rate limits, hash-only storage, replay, revocation, keychain, output/log leakage and races. Fix findings and request follow-up review on the new exact diff.
- [ ] **Step 3: Verify static pipeline parity** by tracing both Mini App and MCP through the same existing message API to AGATA; compare source-level Persona/profile, Naturalness, memory and provider call points. Label provider/runtime/E2E claims that need deployment as unverified.
- [ ] **Step 4: Update the existing ME-27 issue and Release Queue** with changed paths, exact base/commit, checks/review, migration and bot config risks, no live inference/deploy, `LOCAL_PENDING_RELEASE`, and the gated post-release E2E. Do not create another issue or publish code/hosted Actions.
- [ ] **Step 5: Create one clean scoped local commit; run `python3 ~/.codex/bin/gpoint-check-handoff.py --repo <clean_checkout> --handoff <json_evidence_outside_checkout>` and `pnpm repo:clean:end`.** Record exact gate evidence. If any gate fails, report `LOCAL_PENDING_RELEASE` or `BLOCKED`, never `READY_FOR_RELEASE`.

### Post-release E2E gate (must pass before VERIFIED)

- [ ] After standard authorized release and separate OWNER permission for live inference, prove Codex and Telegram use the same deployed `POST /messages/creator/:creatorId` and production AGATA Worker/provider configuration.
- [ ] Verify same OWNER, creator, `conversation_id`, relationship, Persona/profile, Naturalness V1 and memory context without accessing any other user's data.
- [ ] Send through Codex and observe the message/reply in Telegram; send through Telegram and observe the message/reply in Codex.
- [ ] Send through both interfaces concurrently and verify each response correlates to its request; if the production API cannot prove correlation, report pending and do not claim success.
- [ ] Verify a test fact shared in one interface is recalled through the other after asynchronous memory processing.
- [ ] Record evidence and mark `VERIFIED` only after every acceptance step passes. Production Deploy and live inference remain unauthorized by this plan.
