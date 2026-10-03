# GPoint unread messages — design

Status: OWNER_REVIEW_REQUIRED. Conversational architecture explicitly approved by the owner on 2026-10-03. This written specification must be reviewed before implementation planning under Superpowers.

## Purpose and scope

Implement the owner's original Unread Messages specification in the existing GPoint Messages flow. A user must discover incoming messages after leaving a conversation, see an aggregate badge in bottom navigation and an individual count in Inbox, and retain the same state after refresh or Mini App reopen.

No production Deploy, production migration, runtime mutation, MAXIBOT changes, Telegram push notifications, message transport replacement, or unrelated refactoring is authorized. Local isolated database migrations and tests are in scope for implementation after approvals.

## Existing architecture

- `services/agata/app/db/models.py`: AGATA owns Conversation and Message. Message roles are `user`, `assistant`, and `system`; user_id identifies the owner of the AI conversation, not necessarily its sender.
- `services/agata/app/api/internal.py`: authenticated internal endpoints provide GPoint conversation lists and paginated history, with tenant/user/character/conversation/scope filtering and chat-history consent checks.
- `packages/agata-client/src/index.ts`: GPoint's typed AGATA adapter.
- `apps/api/src/messages.ts`: session-authenticated GPoint API, creator availability checks and Inbox hydration.
- `packages/contracts/src/index.ts`: shared Mini App contracts.
- `apps/miniapp/src/messages-ui.tsx`: Inbox and ModelChat; chat history already polls every 3 seconds.
- `apps/miniapp/src/main.tsx` and `apps/miniapp/src/ui/components.tsx`: application shell and shared bottom navigation.

Extend these boundaries. Do not copy message bodies or maintain a second message store in Prisma.

## Chosen accounting model

Persist a nullable incoming-read timestamp on each AGATA Message. New `assistant` messages start unread. Count only retained, eligible incoming messages with no read timestamp. `user` and `system` messages never contribute to the user's incoming count. No arithmetic increment/decrement driven by frontend events is a source of truth.

Each read acknowledgement supplies specific message IDs, at most 100 per request, belonging to a single owned GPoint conversation. Resolve identity from the authenticated GPoint session, and enforce tenant, user, character, conversation, source system, real-world scope, consent and eligible role on the server. Invalid IDs or cross-conversation/foreign IDs must not mutate any messages; malformed or excessive input is rejected. Repeated acknowledgements keep the original timestamp and are idempotent.

An atomic update affects only the explicitly acknowledged, still-unread incoming rows. A concurrent incoming message not included in the acknowledgement stays unread. Existing message-ingestion idempotency remains the protection against duplicated persisted messages; repeated delivery of an event cannot produce an additional count.

In the current product, the other side is an AI character, not a second human recipient account. Do not interpret the conversation owner's user_id as the sender and accidentally count outgoing messages. Do not introduce a creator-side human inbox as part of this feature. Human-to-human messaging, if added later, will require explicit recipient accounting.

## Initial historical state

At migration time, establish existing incoming messages as the initial read baseline, avoiding an unbounded badge for history whose past visibility was never recorded. This is an accounting initialization, not proof of a historical user view. Document the baseline explicitly. New incoming messages after migration remain unread. Do not use a permanent date constant or a lazy first-login reset that could discard new messages before the user's next login.

Migration must be additive, scoped to AGATA, and preserve message content, retention, consent, ownership and RLS. Include a suitable unread-query index following the existing tenant-scoped indexing conventions. Test migration of old messages and insertion of new messages on an isolated PostgreSQL database. Release review must identify the pending AGATA migration; this specification does not authorize applying it in production.

## API and synchronization

Expose unread counts through the existing GPoint Messages API and typed AGATA client. Inbox items gain an unreadCount. Provide an aggregate summary independent of the first Inbox page, so more than 30 conversations are counted. Include only conversations accessible through the existing GPoint creator eligibility rules; an inaccessible creator must not create a badge that the user cannot clear.

Provide an explicit authenticated read-acknowledgement operation; fetching history remains read-only. Return authoritative acknowledgement results and refresh both aggregate and Inbox counts after a successful write.

Use React Query and the existing HTTP polling infrastructure. Poll the lightweight aggregate every 3 seconds while the authenticated application is visible, including when the user is outside Messages. Refresh on visibility/focus restoration and reconnect. Poll Inbox while it is open; retain existing chat-history polling. Disable background polling and scope/clear query state across identity changes. Network failures retain the last known count rather than falsely resetting it to zero.

Cancel outdated in-flight count queries before processing successful read acknowledgements, then invalidate/refetch the related caches. A stale response must not permanently restore a cleared badge. Server state is authoritative; concurrently arriving messages will be reflected by the next successful poll. This feature offers polling latency, not instantaneous push delivery.

## When a message is read

Opening a conversation starts fetching and rendering history. Only persisted incoming messages displayed in the visible chat viewport are eligible for acknowledgement. A hidden Mini App, consent gate, failed history fetch, prefetched history, off-screen older history, or conversation the user has already left must not trigger acknowledgement.

Observe visible message rows within the scrollable chat area. Acknowledge eligible IDs in bounded batches after render; observe newly arrived messages and messages revealed by scrolling. A send response rendered before history refresh uses its persisted incoming message ID and the same read path. Outgoing optimistic bubbles are excluded.

Disconnect observers on conversation change/unmount and pause acknowledgement when the document is hidden. Failed acknowledgement preserves unread state and retries through the normal visible-chat lifecycle with bounded/backed-off requests. Do not silently consider a failed mutation successful.

## UI

DESIGN_MODE C: modification of the existing Messages screen. Reuse existing Inbox, thread rows, BottomNav, Lucide icons and GPoint typography/color tokens. The supplied screenshot is a runtime reference for the existing layout, not a new Golden or a redesign request. New elements are small count badges only; no new visual pattern or image asset is needed.

Place the aggregate badge on the Messages navigation icon and a per-thread badge alongside the thread's time. Highlight unread threads using existing emphasis tokens. Hide badges at zero, show exact counts through 99 and `99+` above that, and expose the exact count through accessible labels. Preserve all five navigation items, touch targets and safe areas. Visual status remains OWNER_REVIEW_REQUIRED until owner acceptance; automated screenshots cannot self-approve appearance.

Primary mobile verification: 390x844. Also check overflow/overlap at 360x800, 393x852 and 430x932 if responsive badge placement needs additional validation.

## Verification and acceptance

Use Superpowers TDD: observe the relevant new tests fail before product implementation, then verify passing tests and the existing suites. Server tests must exercise real PostgreSQL accounting and ownership, not just frontend mocks.

Required regression cases:

1. New incoming messages increase the thread and aggregate counts; outgoing/system messages do not.
2. Two conversations retain independent counts; reading one leaves the other's count intact.
3. Refresh, new authenticated Mini App session and reopening preserve backend state.
4. Duplicate ingestion and repeated read acknowledgements do not double count or alter original read timestamps.
5. A message inserted concurrently with an acknowledgement remains unread unless explicitly acknowledged.
6. Foreign tenant/user/conversation IDs, invalid IDs, malformed and excessive batches cannot mutate another conversation.
7. Paginated Inbox does not truncate the aggregate total; inaccessible/deleted messages and creators follow eligibility rules.
8. Visible incoming rows are acknowledged; hidden app, off-screen rows, failed fetch and consent gate are not.
9. Late poll responses, failed acknowledgement, network outage and focus restoration preserve eventual consistency without false zero counts.
10. Old-history migration establishes a baseline; newly inserted incoming messages are unread.
11. At 390 px, badges render without clipping, overlap or horizontal overflow; zero, single-digit, multi-digit and 99+ states are checked.

Before completion, independently compare the implementation with the original detailed request, inspect the diff for scope, run clean-end verification, commit and push verified changes, and provide the final SHA, test results, visual evidence, test-environment steps and limitations. No feature-complete claim is permitted while tests or implementation remain unfinished.

## Known trade-offs

- Active-app discovery latency is approximately one 3-second polling interval plus network/server time; a suspended Mini App refreshes on return.
- Existing history begins at an explicit baseline because historical read evidence does not exist.
- Per-message updates require an additive AGATA migration during a separately authorized release.
- Long-message readability is not proven by viewport visibility; the read semantics are availability for viewing, as requested.
- This scope supports the existing user-to-AI conversation architecture, not a new human-to-human messaging product.
