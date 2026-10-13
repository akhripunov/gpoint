# GPoint Codex direct chat with AGATA — Design

**Status:** OWNER APPROVED 2026-10-13. Local implementation only; production rollout and live inference require separate authorization.

## Goal and constraints

Let the allowlisted GPoint OWNER connect local Codex to one existing approved character conversation. Codex reads and sends through the existing authenticated GPoint messages API. AGATA retains the same identity, conversation, relationship, Persona, Naturalness V1, memory, provider, Mac Worker, idempotency and response guards. No transcript or memory is copied. MCP never calls an LLM.

Do not create another AGATA, relationship, dialogue ledger or memory store. Do not use Telegram initData outside ordinary login, admin rights, raw credentials in chat or direct database writes for dialogue.

## Authorization and storage

GPOINT_CODEX_OWNER_USER_IDS is a fail-closed deployment allowlist. It is an independent OWNER grant; ADMIN or creator-owner roles do not grant access. The normal authenticated Mini App session approves the connection.

The MCP process creates a random device secret and short user code. Only the user code is shown to Codex. OWNER opens the selected existing chat and approves the code. The API verifies the allowlist, authenticated user, approved creator and existing conversation history, then binds access to that user and creator.

Persist only credential hashes and scope metadata in one GPoint API credential table. It contains no messages, prompts, memories or AGATA state. Access credentials are short-lived; refresh credentials rotate. Every scoped message request checks expiry and revocation. Disconnect revokes the selected connection. The scoped credential is denied on every route except reading/sending messages for its bound creator.

A user code expires after five minutes and is single-use with bounded failed attempts. Refresh credentials are stored in macOS Keychain. MCP logs and diagnostics omit tokens, codes and message contents.

## Components and flow

1. A local MCP stdio server provides start_connection, finish_connection, read_history and send_message. A project MCP configuration starts it in a newly opened Codex chat.
2. The OWNER opens the existing Mini App chat for the chosen character. A control appears only when the signed-in account is allowlisted. OWNER enters the one-time code and approves; the control then offers disconnect.
3. MCP exchanges the approved device secret for scoped credentials and stores them in Keychain.
4. read_history calls the existing GET /messages/creator/:creatorId. send_message calls POST /messages/creator/:creatorId with a UUID idempotency key. GPoint applies existing age/consent checks and submits to AGATA. AGATA records and processes the message using the existing relationship and memory, provider and Mac Worker. MCP returns the API response text unchanged. For an asynchronous response it polls that same authorized history and never resends with a new key.
5. Mini App disconnect revokes the credential. Subsequent refresh or messages calls fail closed.

The MCP server cannot enumerate users or the OWNER's inbox. Character selection occurs inside the authenticated Mini App chat.

## Failure behavior

- Missing allowlist, invalid/expired/reused code, non-allowlisted user, unapproved creator, or missing existing history: deny without issuing credentials.
- Expired/revoked access: require reconnection; never fall back to a broad user token.
- AGATA/provider unavailable or still pending: return the actual state; never fabricate a Codex answer or retry with a new ID.
- Refresh rotation is atomic; prior refresh tokens fail.
- Keychain failure: do not write credentials to plaintext files or environment variables.
- Do not log HTTP bodies, messages, tokens, device secrets, user codes or provider results.

## Verification

Automated tests cover device authorization expiry/consumption/rate limits, allowlist and creator scope, route denial, refresh/revocation races, message idempotency/order, exact response preservation, OWNER-only UI and history refresh. MCP tests use a local fake GPoint API and Keychain adapter; no live inference.

After security review and standard release, a separately authorized production smoke must verify: Codex send and exact AGATA response, same Mini App history, Telegram continuation, then Codex history refresh.

Authentication/schema changes require independent security review and standard release gates. This design does not authorize publication, hosted CI, deployment, production data mutation or live inference.
