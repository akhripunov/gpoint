# Unread Messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist unread incoming message state and show correct aggregate and per-dialog badges in the existing GPoint Mini App.

**Architecture:** Extend AGATA Message with incoming_read_at; derive counts from eligible persisted assistant messages. Add bounded, authorized per-message acknowledgements and a lightweight aggregate query through the existing GPoint API. Use existing React Query polling and viewport observation without replacing the chat.

**Tech Stack:** PostgreSQL, SQLAlchemy, Alembic, FastAPI, NestJS, TypeScript, React, React Query, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-03-unread-messages-design.md`, owner-approved source commit `554c6cac0ad23052522de5aa4d5da82d93d67124`.

**Status:** OWNER_PLAN_REVIEW_REQUIRED. Recommended execution: Native (`executing-plans`), phased in this session with one independent whole-change review before completion. Adjacent tasks share API contracts, so sequential implementation minimizes coordination overhead.

## Global Constraints

- No production Deploy, production migration, runtime mutation, MAXIBOT changes, Telegram push notifications, message transport replacement, or unrelated refactoring is authorized.
- Do not copy message bodies or maintain a second message store in Prisma.
- Each read acknowledgement supplies specific message IDs, at most 100 per request, belonging to a single owned GPoint conversation.
- New `assistant` messages start unread; `user` and `system` messages never contribute to the user's incoming count.
- Fetching history remains read-only. Only visible persisted incoming messages are acknowledged.
- Primary mobile verification: 390x844. Visual acceptance remains OWNER_REVIEW_REQUIRED.
- Use existing dependencies and GPoint tokens; do not introduce a new UI library, push transport, shared infrastructure or cross-project dependency.
- Observe RED before GREEN for each task; run existing suites and record skipped tests explicitly.
- Leave the repository clean with scoped, verified changes committed and pushed. Production authorization remains separate.

## Review Focus

- Long history and off-screen bubbles: loaded messages outside the chat viewport remain unread (Task 4).
- Identity change while a count request is pending: old account data cannot appear in the next account (Task 4).
- Mixed batches containing one foreign ID: reject the whole mutation with no partial acknowledgement (Task 2).
- A late count response after a read acknowledgement: cancel obsolete queries and reconcile; do not permanently restore cleared counts (Task 4).
- Many users polling simultaneously or a failed service: jitter and bounded backoff avoid synchronized retry storms; query cost follows the unread set, not full encrypted history (Tasks 2, 4, 5).

## Polling efficiency decisions

- One shared unread query per authenticated application; navigation and Inbox consume the same result. No second timer for each consumer.
- Normal interval is 3000 ms. Randomize the initial polling phase within 0–3000 ms, resume with one refresh, and preserve normal polling thereafter. Stop background polling.
- On failures, back off exponentially from 3 seconds to a 60-second ceiling, retaining last known data; successful requests restore the normal interval. Avoid overlapping requests and automatic immediate retry bursts.
- Summary returns only IDs/counts, never message text, avatars, decrypted previews or AI/worker status. AGATA uses a grouped partial-index query; GPoint performs one bulk creator-eligibility query, not one lookup per conversation.
- Inbox refreshes while open when the shared summary changes, and on entry/focus/read/send. Avoid decrypting and hydrating the full Inbox on every unchanged 3-second count poll.
- No speculative cache service. At 1000 concurrently active users, 3-second polling means approximately 333 requests/second; this is a capacity requirement to measure, not a claim that the current deployment sustains it. Record query plan/timings and remaining need for deployment-level load testing.

### Task 1: Persistent accounting and historical baseline

**Files:** Modify `services/agata/app/db/models.py`; create `services/agata/migrations/versions/0010_unread_incoming_messages.py`; create `services/agata/tests/test_postgres_unread_messages.py`; extend `services/agata/tests/test_schema_contract.py` where appropriate.

**Interfaces:** Produce `Message.incoming_read_at: datetime | None`, nullable and default NULL. Migration revision `agata0010`, down_revision `agata0009` verified in `0009_versioned_chat_onboarding_memory_choice.py`; retain all existing ownership constraints and RLS.

- [ ] Write PostgreSQL regressions: existing assistant history receives a migration-time baseline; newly inserted assistant rows remain NULL; content/ownership stay unchanged. Test the declared partial index and existing ingestion uniqueness.
- [ ] Run `AGATA_TEST_DATABASE_URL=<isolated-local-test-dsn> <agata-python> -m pytest services/agata/tests/test_postgres_unread_messages.py -v` with the repo's established PYTHONPATH and observe assertions fail on the missing feature. Resolve interpreter/test database availability without production credentials.
- [ ] Implement the additive column and index. Baseline only existing GPoint real-world assistant rows. Verify the migration role's existing RLS/bypass behavior and exercise more than one tenant; do not disable RLS. New inserts must not inherit the baseline.
- [ ] Run the regressions plus AGATA schema/migration tests against the isolated PostgreSQL database; record revision and commands.
- [ ] Commit the passing persistence change with its tests.

### Task 2: AGATA counts and bounded read acknowledgements

**Files:** Modify `services/agata/app/api/internal.py`; create a focused helper `services/agata/app/messages/unread.py` if needed; extend `services/agata/tests/test_postgres_unread_messages.py` and `services/agata/tests/test_internal_api_e2e.py`.

**Interfaces:** Internal `GET /users/{gpoint_user_id}/unread` under `/internal/v1` returns `{items: [{gpointCharacterId: string, unreadCount: number}]}` across all eligible conversations. Internal `POST /users/{gpoint_user_id}/characters/{gpoint_character_id}/messages/read` accepts `{messageIds: string[]}` and returns `{acknowledgedMessageIds: string[]}`. Existing conversation items gain `unreadCount: number`; history items gain `incomingReadAt: string | null`.

- [ ] Write failing HTTP/PostgreSQL tests for two dialogues, outgoing/system exclusion, more than 30 dialogues, deleted/closed/ineligible scope, no-consent access, foreign identities, malformed UUIDs, oversized/duplicate batches and a mixed foreign batch with no partial writes.
- [ ] Write concurrency tests: repeated acknowledgement preserves the first timestamp; concurrent distinct assistant insertion stays unread; duplicate ingestion remains one persisted incoming row; GET history does not change counts.
- [ ] Run those tests and confirm feature-specific failures before adding endpoints.
- [ ] Implement one scoped grouped count query; extend Inbox counts using a grouped query for the current page, not per-row queries. Authorize every unique supplied ID before an atomic update restricted to owned still-unread assistant messages. Map malformed input to 422 and inaccessible IDs to an appropriate stable 404 without leaking content.
- [ ] Run all AGATA tests with isolated database configured. Capture `EXPLAIN (ANALYZE, BUFFERS)` for unread queries with representative read/unread distribution and a larger fixture; verify no text decryption or one-query-per-conversation path.
- [ ] Commit the verified server API and regressions.

### Task 3: Typed GPoint API integration

**Files:** Modify `packages/agata-client/src/index.ts`, `packages/contracts/src/index.ts`, `apps/api/src/messages.ts`, `apps/miniapp/src/api/client.ts`; extend `packages/agata-client/test/client.test.ts` and `apps/api/test/messages.test.ts`; add an API integration case to `apps/api/test/integration.test.ts` if required for real routing/session checks.

**Interfaces:** Add `AgataClient.unreadMessages(userId)` and `AgataClient.markMessagesRead(userId, characterId, messageIds)`. Public `GET /messages/unread` returns `{totalUnread: number, items: [{creatorId: string, unreadCount: number}]}`. Public `POST /messages/creator/:creatorId/read` accepts `{messageIds: string[]}` and returns `{acknowledgedMessageIds: string[]}`. Extend thread/history DTOs with the Task 2 read fields. Mini App exposes `messagesUnread()` and `markCreatorMessagesRead(creatorId, messageIds)`; summary fetching accepts cancellation via AbortSignal.

- [ ] Write failing client transport tests for exact endpoints, escaping, authentication, payloads and errors. Write API tests proving session identity is authoritative, inaccessible creators are excluded from aggregate counts, total covers all returned eligible creators and malformed read batches are rejected before upstream mutation.
- [ ] Run `pnpm --filter @gpoint/agata-client test` and `pnpm --filter @gpoint/api test`; confirm new-feature failures.
- [ ] Implement the contracts, adapter and fixed summary route. Perform one bulk Prisma eligibility lookup for nonzero AGATA character counts. Reuse existing approved-creator checks for acknowledgement. Preserve existing send/consent/error behavior.
- [ ] Run package tests, API integration tests against isolated local services and `pnpm typecheck`; check frontend fixture compatibility with the additive fields.
- [ ] Commit the verified adapter/contracts change.

### Task 4: Shared polling, visible-message acknowledgement and badges

**Files:** Modify `apps/miniapp/src/main.tsx`, `apps/miniapp/src/messages-ui.tsx`, `apps/miniapp/src/ui/components.tsx`, `apps/miniapp/src/style.css`; create scoped `apps/miniapp/src/messages-unread.tsx` and a small testable `.js` helper with `.d.ts` only if needed; add `apps/miniapp/test/messages-unread.test.js`; extend `tests/browser-smoke/messages.spec.ts` and its existing mock API fixtures.

**Interfaces:** Shared unread hook consumes the Task 3 summary; cache identity is account-scoped. Optional BottomNav item `badgeCount?: number` preserves existing consumers. Observer uses `.chat-history` as root and only persisted assistant rows with NULL read state. Batch acknowledgement consumes Task 3 and refreshes Inbox/summary on successful server confirmation.

- [ ] Write failing tests for zero/1/99/100 display and exact accessible counts; navigation badge outside Inbox; independent thread counts; entering and leaving a chat; reload/new session; hidden document/off-screen incoming rows; delayed send response; failed acknowledgement; account switch; late summary response and visibility/reconnect recovery.
- [ ] Write scheduling tests verifying a single deduplicated query, randomized initial phase, no polling while hidden, no overlapping requests, bounded error backoff and restoration to 3000 ms. Assert repeated unchanged summaries do not refetch/decrypt Inbox.
- [ ] Run `pnpm --filter @gpoint/miniapp test` and `pnpm exec playwright test -c playwright.browser-smoke.config.ts tests/browser-smoke/messages.spec.ts`; observe feature-specific failures before UI implementation.
- [ ] Implement the shared query, batched viewport observer and scoped badge styles using current GPoint tokens. Exclude optimistic outgoing messages; stop observers on unmount/identity change. Retry failed acknowledgements with bounded scheduling, not render-loop requests. Propagate AbortSignal so obsolete polling requests can be cancelled.
- [ ] Run the same tests plus `pnpm typecheck`; verify existing onboarding/pending/navigation regressions. Capture 390x844 screenshots for Inbox and navigation count variants; check clipping, overlap, horizontal overflow and touch targets. Check 360x800, 393x852 and 430x932 as needed for responsive badge placement.
- [ ] Commit the passing UI implementation and tests. Label screenshots OWNER_REVIEW_REQUIRED.

### Task 5: Whole-feature verification and review

**Files:** Create `docs/superpowers/reports/2026-10-03-unread-messages-verification.md`; include test-environment instructions and result references. Modify only in-scope implementation/tests if review finds required defects.

**Interfaces:** Consume Tasks 1–4 and the original owner specification; produce final verified SHA, actual test totals/skips, screenshots, migration notice and limitations.

- [ ] Run `pnpm test`, `pnpm typecheck`, `pnpm build`, isolated-database AGATA pytest, API integration and Messages browser smoke. Record every failure and skipped database test; skipped accounting tests cannot satisfy acceptance.
- [ ] Exercise a real isolated AGATA/GPoint test path: insert incoming messages in two conversations, query counts, acknowledge only visible IDs, refresh/reopen and verify backend persistence. Mock browser tests alone do not prove persistence.
- [ ] Record representative query timings/plans and request counts for unchanged polling. Distinguish local query measurements from end-to-end deployment capacity; do not promise 1000-user capacity without a load test.
- [ ] Obtain an independent whole-change review for ownership, races, baseline migration, UI lifecycle, query load and compliance with the original requirements. For necessary fixes, add a failing regression first, then rerun relevant suites.
- [ ] Publish the verification report, commit/push scoped changes, verify GitHub exact SHA and run `pnpm repo:clean:end` → PASS. Do not dispatch Deploy or apply production migrations.

## Self-review

Plan covers the original ten behavior requirements and all specification regressions. API names and payloads are consistent across Tasks 2–4. The five review-focus cases have explicit test owners. No new message system, materialized counter, production operation or unrelated refactor is included. Performance verification reports observations and limits rather than implying unlimited scalability.
