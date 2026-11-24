# GPoint Eros Dialogue Lab for Codex

This is the existing GPoint MCP package, configured under the existing `gpoint_agata` server entry. It uses GPoint's authenticated conversation API and the owner-approved `models-hub` creator only. It does not call an LLM directly, create another account or conversation, or store a second transcript. Telegram Mini App and Codex use the same GPoint/Eros relationship, session, history, and memory.

## Connect

The project Codex configuration passes the existing `GPOINT_API_URL` setting through to the MCP process. It must be the trusted HTTPS GPoint API base URL. The current Codex user configuration has this variable set; its value is not copied into the repository. Restart or open a trusted Codex project session after changing the tool list.

1. Call `connect_gpoint` for the fixed `models-hub` creator. The MCP stores the temporary device code in macOS Keychain and returns a short-lived link to the existing Telegram bot.
2. Open the link in Telegram and approve it in the existing bot chat with the owner account. GPoint independently verifies the owner allowlist, approved creator ownership, chat consent, and existing Eros history before issuing a scoped grant.
3. Call `finish_gpoint_connection`. Access and refresh credentials are stored in macOS Keychain. `get_gpoint_connection_status` reports local Keychain state only; a successful authenticated history read is the server-side check.
4. Use `read_gpoint_history` to read the existing Models Hub transcript. `disconnect_gpoint` revokes the server grant and removes local Keychain credentials.

The production API already registers the device start/exchange/refresh/revoke and Telegram approval endpoints. Before a production Telegram challenge can succeed, DevOps must configure the exact owner user ID in `GPOINT_CODEX_OWNER_USER_IDS`, the existing bot name in `TELEGRAM_BOT_USERNAME`, and the socket-peer IPs of trusted proxies in `GPOINT_CODEX_DEVICE_START_TRUSTED_PROXY_IPS`. The existing bot must reach the API's HTTPS ingress ending in `/api/internal/telegram/update`; its webhook approval route is `/api/auth/codex/telegram/codex-approve`. Fail-closed proxy and TLS requirements remain unchanged. No values belong in Git or chat. The latest ME-27 production audit found the three API environment settings missing; production configuration is not changed by this package.

## Manual conversation

Call `send_gpoint_message` once with the owner's exact text. It posts through `POST /messages/creator/:creatorId`, preserving the existing GPoint consent/age gates and Eros session. The tool result contains the exact Models Hub reply as its first text block and message/client IDs in a separate correlation block. Codex should display the reply verbatim.

## Automatic scenario

1. Call `begin_models_hub_scenario` with the test scenario and a hard `maxTurns` from 1 to 20.
2. Codex authors one next user turn and calls `send_models_hub_scenario_turn` once. It must show that exact Models Hub reply in the Codex chat before composing another turn.
3. Continue sequentially until the scenario ends or the server reaches the cap, then call `stop_models_hub_scenario`.

Only the cap and turn count are persisted in Keychain; the scenario text remains in the Codex conversation. If delivery times out or returns pending, do not create a new message. `resume_pending_gpoint_message` resubmits the exact payload with the same `clientMessageId`, relying on the existing GPoint/Eros idempotency contract. A pending send blocks later sends and disconnect until reconciled. Resume attempts do not consume another scenario turn. Never continue after an error, revoked grant, or unknown delivery result.

The MCP is only an API client. It never synthesizes model text or bypasses Eros. No live inference should be sent until the owner has approved a bounded real test and confirmed the Telegram link.
