# GPoint Codex direct chat with AGATA — Design

**Status:** OWNER APPROVED 2026-10-15 — revised Telegram authorization. Local implementation only. Production release and live inference are not authorized.

## Goal and constraints

Let the allowlisted OWNER start a local Codex connection to one existing GPoint conversation, then read and send messages from Codex. Codex uses only the existing GPoint message API; that API submits to the same AGATA Core and production provider/Worker path as Telegram Mini App. Persona, Identity Core, Naturalness V1, relationship, history, memory, consent, response handling and idempotency remain owned by the existing pipeline. No transcript, relationship, memory or generated reply is copied or recreated. MCP never calls an LLM.

Do not add Codex controls to any GPoint user interface. Do not create another bot, AGATA, relationship, conversation, memory store, test database, or hidden admin route. Do not spoof Telegram initData, disable access checks, expose permanent credentials in chat, read another user's data, change AGATA behavior, or deploy/run live inference without separate OWNER authorization.

## Authorization and storage

`GPOINT_CODEX_OWNER_USER_IDS` remains a fail-closed exact GPoint-user allowlist. The existing GPoint Telegram bot authenticates the Telegram principal: it accepts only `/start codex_<opaque-token>` in a private chat where `chat.id == from.id`, then forwards Telegram's sender/chat identity and payload over HTTPS authenticated by `TELEGRAM_WEBHOOK_SECRET`. The API must map Telegram ID to the existing GPoint user, require that user's exact OWNER allowlist entry, require the APPROVED selected creator and the OWNER's `CreatorOwner` relation, and independently require existing AGATA history for that same user/creator pair. Every check is mandatory; CreatorOwner or history alone does not authorize access. Repository inspection found the production bot and API share Docker networks and the API binds loopback HTTP; that topology does not encrypt the container bridge. Therefore production bot startup fails closed unless `GPOINT_INGEST_URL` is an HTTPS API URL with the fixed external `/api/internal/telegram/update` path. The existing Caddy `/api/*` handler strips `/api` before proxying to the API, so all derived bot/API paths retain `/api` externally. The deployed TLS ingress and effective runtime URL are not available in local verification and must be confirmed by DevOps before release.

The MCP starts device authorization with the requested existing creator slug. The API returns a `t.me/<existing bot>?start=codex_<random 256-bit token>` link and stores only a hash of that token, the MCP device-secret hash, source rate key, expiry and eventual user/creator binding. The Telegram token expires in five minutes and can grant once. The API grants only after the bot callback passes private-chat, Telegram identity, OWNER allowlist, APPROVED creator and existing-history checks. A copied link cannot authorize a different Telegram account; the callback is replay-safe. Token exchange is one-use. No GPoint session, Telegram initData, bot token, bearer token or raw device token is placed in the URL except the short-lived one-use challenge itself.

**Authorization boundary:** the callback requires both `CreatorOwner` relation and the same OWNER's existing AGATA history for the selected creator. If either relation or history is absent, approval fails closed; no fallback identity, relationship, or copied memory is permitted. Do not report access as available for a specific production conversation until its existing server-side relationship and history checks pass.

Credential rows contain authorization metadata only, never messages, prompts or AGATA state. The access token is short-lived; refresh credentials rotate; every read/send request checks expiry, revocation, OWNER allowlist, and exact creator scope. Refresh credentials remain in macOS Keychain. Codex disconnect calls the scoped revoke endpoint and deletes local credentials. The device start rate limit and trusted-proxy fail-closed behavior remain in force. Every bot→API request carrying `TELEGRAM_WEBHOOK_SECRET` disables redirects to prevent forwarding the shared secret to another origin.

## Components and flow

1. The existing local MCP stdio server exposes `connect_gpoint(creatorSlug)`, `finish_gpoint_connection`, `read_gpoint_history`, `send_gpoint_message` and `disconnect_gpoint`. MCP stores its pending device secret and resulting scoped credentials in macOS Keychain.
2. OWNER runs `connect_gpoint` from Codex for the slug of the existing conversation. Codex displays the short-lived Telegram deep link and expiration. OWNER opens it and starts the existing GPoint bot chat.
3. The existing bot's `/start` handler distinguishes the `codex_` payload, requires a private chat, and forwards that single update to a new internal API approval endpoint using the existing bot→API secret channel. It does not expose any Codex control in the Mini App or send authorization to a different service. The bot returns a generic approved/expired response.
4. The API validates the payload hash, expiry, one-use state, `chat.id == from.id`, mapped GPoint account, exact OWNER allowlist, requested APPROVED creator and pre-existing AGATA conversation. It binds the pending device request to that exact existing user/creator. MCP polls/exchanges the device secret, then stores scoped credentials in Keychain.
5. History calls the existing `GET /messages/creator/:creatorId`. Sends call the existing `POST /messages/creator/:creatorId` with a UUID idempotency key. The same conversation and relationship state used by Telegram are read and updated by AGATA's current production flow. MCP returns the completed API response text unchanged. A pending response remains pending; Codex does not guess which subsequent assistant message belongs to it.
6. `disconnect_gpoint` revokes the bound server credential and removes local Keychain items. No Mini App Codex status, approval, button, panel, or revoke control remains.

## Failure behavior

- Missing OWNER allowlist, Telegram username/configuration, webhook secret, invalid private-chat proof, mismatched Telegram sender/chat IDs, unknown/non-OWNER GPoint account, invalid/expired/replayed token, unapproved creator, missing OWNER `CreatorOwner` relation, or missing pre-existing AGATA history for that same user/creator pair: deny and issue no credential.
- No user enumeration: a failed approval does not disclose account, creator or history existence to the bot or caller.
- Bot/API/network unavailable: leave the request pending only until expiry; never fall back to a GPoint user token or an LLM call from Codex.
- Expired/revoked scope: require a fresh Telegram authorization; never broaden scope.
- AGATA/provider unavailable or pending: report the actual API state; never synthesize a reply or submit a duplicate with a new idempotency key.
- Keychain failure: do not fall back to plaintext files, environment variables or chat-stored persistent credentials.
- Logs omit deep-link token, device/access/refresh credentials, message bodies and provider results.

## Verification and release boundary

Offline tests prove the Telegram callback input checks, one-use/expiry/scope properties represented by the shared auth helpers, MCP Keychain persistence, exact response passthrough, send ordering, Mini App removal, and URL transport policy. Tests use fake API/Keychain dependencies and never invoke live inference.

Static route/source inspection can establish that Codex sends through the same production API route as Telegram. It cannot prove deployed Worker/provider configuration, same production `conversation_id`/relationship/Persona/memory, Telegram visibility in both directions, or concurrent response attribution. Those facts require a separately authorized post-release production E2E; until it passes, status is never `VERIFIED`. No production deploy, migration apply, Telegram production message, or live inference is part of this implementation.

After standard security review and release, and only after separate OWNER permission for live inference, the real E2E must prove: same OWNER/creator/conversation_id/relationship/Persona/Naturalness V1/memory/provider path; Codex send visible in Telegram; Telegram send visible in Codex; concurrent sends correctly correlated; and a fact introduced through one interface recalled through the other after normal asynchronous memory processing. Do not inspect another user's data. Preserve pending-response behavior if production lacks an unambiguous response correlation ID.

## Alternatives considered

- Reusing the Mini App approval control violates the revised no-Codex-UI requirement.
- A new bot would add credentials, deployment and lifecycle with no capability gain: the existing grammY bot already receives private `/start` messages and forwards authenticated internal Telegram events.
- Browser login would require a new public callback/login surface and would not be simpler than the existing bot's Telegram identity proof.

Therefore the approved path reuses the existing bot and API callback authorization while requiring TLS for production bot→API traffic. The current runtime environment value and external TLS routing cannot be inspected in this local task; those remain explicit release checks.
