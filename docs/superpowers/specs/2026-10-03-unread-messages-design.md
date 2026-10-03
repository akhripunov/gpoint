# GPoint unread messages — design

Status: APPROVED_OWNER. The owner explicitly approved the written specification in commit `554c6cac0ad23052522de5aa4d5da82d93d67124` on 2026-10-03. Approval covers architecture and scope, not production deployment or final visual acceptance.

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

## Minimal extension map

The owner reaffirmed the architecture and required this explicit changed/unchanged map on 2026-10-03. No substantial replacement of the current chat is necessary. If implementation discovers otherwise, stop before making that replacement and present evidence and justification to the owner.

### Existing database tables

| Existing table/model | Extension | Preserved behavior |
| --- | --- | --- |
| `agata.messages` / `Message` | Add nullable `incoming_read_at`, a tenant-scoped unread index, and initialize the existing-history baseline in an additive Alembic migration. | Message IDs, conversation ownership, role, encrypted content, ingestion idempotency, deletion/retention and existing RLS remain intact. |
| `agata.conversations` / `Conversation` | No schema changes; use existing ownership and conversation identity when authorizing acknowledgement/count queries. | Conversation creation, relationships, external IDs, scope, history ordering and pagination remain intact. |
| AGATA `User`, `Character`, `Relationship`, `Consent` | No schema changes; reuse current identity, eligibility and consent checks. | Authentication boundaries, disclosure/onboarding, memory opt-in and age assurance remain intact. |
| GPoint Prisma tables | No schema changes and no duplicate message/read-state table. | Creator eligibility and public profile hydration remain intact. |

No separate unread-message store, receipt table, event bus or materialized counter is introduced. New incoming messages use the existing nullable column default; the generation/ingestion pipeline does not need to emit new events or maintain arithmetic counters.

### Existing and additive API surface

| Surface | Extension | Preserved behavior |
| --- | --- | --- |
| AGATA `GET /internal/v1/users/{gpoint_user_id}/conversations` / `list_gpoint_conversations` | Add `unreadCount` to each conversation item. | Existing cursor, page size, preview, sorting and consent/ownership checks. |
| AGATA `GET /internal/v1/users/{gpoint_user_id}/characters/{gpoint_character_id}/messages` / `list_gpoint_messages` | Include the persisted read status needed to acknowledge visible incoming rows. | Fetch remains read-only; existing history content, roles, IDs and pagination. |
| AGATA internal Messages API | Add `GET /internal/v1/users/{gpoint_user_id}/unread` for complete per-character counts and `POST /internal/v1/users/{gpoint_user_id}/characters/{gpoint_character_id}/messages/read` with `{messageIds: string[]}`. | Existing internal authentication and tenant context. No new public AGATA access path. |
| GPoint `GET /messages` / `MessagesController.inbox` | Pass through per-thread `unreadCount` after current creator eligibility checks. | Existing creator DTO hydration, Inbox pagination and preview. |
| GPoint `GET /messages/creator/:creatorId` / `MessagesController.history` | Pass through incoming read status. | Existing approved-creator/session checks, history and CHAT_OPEN analytics. |
| GPoint Messages API | Add `GET /messages/unread` returning `{totalUnread: number, items: {creatorId: string, unreadCount: number}[]}` for eligible creators, and `POST /messages/creator/:creatorId/read` with `{messageIds: string[]}`. | Session-derived user identity; no client-selected recipient identity. Declare fixed routes before dynamic routes as needed. |
| `AgataClient.listConversations`, `AgataClient.listMessages`, shared DTOs and Mini App API client | Extend typed response fields and add typed summary/read methods. | Existing method signatures/options, error mapping and authenticated request transport. |
| Existing send/respond, consent, memory and age APIs | No behavior changes. | Existing idempotency, pending responses, AI generation, memory choice and compliance gates. |

The read response confirms the acknowledged owned IDs; the client then refreshes authoritative Inbox and aggregate data. It must not assume the acknowledged batch represents every unread message in the conversation.

### Existing frontend components

| Existing component/file | Extension | Preserved behavior |
| --- | --- | --- |
| `MessagesInbox` in `apps/miniapp/src/messages-ui.tsx` | Display per-dialog badge/emphasis and refresh while visible. | Thread layout, avatars, previews, ordering, navigation, pagination and loading/error states. |
| `ModelChat` in the same file | Observe visible persisted incoming rows and acknowledge their IDs; refresh relevant count caches. | Composer, pending/retry handling, bubble content, older-history loading, back navigation, onboarding and consent UI. No chat rewrite. |
| Application shell in `apps/miniapp/src/main.tsx` | Mount the authenticated unread-summary query across screens and supply the Messages count to navigation. | Routing, all five tabs, startup/session flow and existing hidden navigation inside Chat. |
| Shared `BottomNav` in `apps/miniapp/src/ui/components.tsx` | Add an optional count capability for the Messages item; default absent/zero means no badge. | Other navigation items, callbacks, icons, touch targets and existing consumers. |
| `apps/miniapp/src/style.css` / existing token styles | Small scoped badge and unread-emphasis rules using current tokens. | Page design, typography, general layout, colors and unrelated screens. |
| Existing browser/API/AGATA test suites | Add feature regressions and fixtures. | Preserve existing tests for onboarding, pending replies, rights and navigation. |

Small focused unread query/visibility helpers may be extracted to avoid bloating existing components. They remain adapters around the existing Messages API and cache; they are not a second chat implementation.

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
