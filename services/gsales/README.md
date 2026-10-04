# G Sales V1

Independent FastAPI CRM and controlled Telegram application using the shared
AGATA HTTP API. Owner spec v1.2; workflow correction: reports are in the current
Codex chat, with agreed technical documentation in Linear. No automatic chat forwarding.

## Boundaries and defaults

- G Sales owns `gsales.{sellers,telegram_accounts,leads,lead_sources,outreach_attempts,dialog_controls}`.
- Migration receipts/checksums live in private `gsales_meta.migrations`.
- Persona, conversation messages, relationships and memory remain in AGATA.
  G Sales does not import Core code or retain message plaintext in CRM.
- Fixed GSALES tenant: `c4d67889-877e-4d1b-a8d8-597c479d638f`.
  GPOINT tenant stays `7508f36f-774a-48aa-8c6b-42f6f160415d`.
- No cross-product FK, public schema rename, second repository/VPS or changes
  to Conversation Lab/MAXIBOT.
- Outreach, automatic replies, management bot, MTProto listener and registration
  lookup default OFF. Production credentials are not included.

## Local development and tests

Python 3.12 is required. Run from this directory:

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check app tests
.venv/bin/mypy app
.venv/bin/uvicorn app.main:app --port 8010
```

The integration harness additionally installs `../agata[test]` into a developer
venv, for tests only. Runtime dependencies/image contain no AGATA package.

```sh
PYTHONPATH=. .venv/bin/python tests/run_postgres.py
```

It creates and removes its own loopback-only, disposable pgvector PostgreSQL16
container. It reproduces the old GUC spoof vulnerability, then checks role-bound
RLS, six business tables, concurrent sends, owner controls, Core HTTP, encrypted
worker drafts, delivery confirmation and the full pre-existing Core PostgreSQL
regression suite. No production environment, accounts or paid inference are read.
Tests that need this harness skip in the unit-only run; that run is not a security gate.

## Owner API and workflow

Bearer `GSALES_OWNER_API_TOKEN` protects CRM/control routes. Input schemas reject
unknown fields, including client-supplied tenant. OpenAPI is at `/docs`.

1. Explicitly configure existing management bot identity and owner Telegram ID.
   Never create or reuse a bot token automatically.
2. After approval of a real test account, run `python -m app.telegram.login`.
   Interactive API hash, login code and 2FA use hidden prompts. Only encrypted
   session/API credentials are retained outside Git; codes/passwords are discarded.
3. Create seller via `POST /v1/sellers`, with owner-authored Persona in Core and
   the returned account UUID. Seller starts paused; global pause starts enabled.
4. Add lead and public/manual/opt-in source via `POST /v1/leads`. Stable Telegram
   user ID deduplicates across sellers. Check eligibility using the owner endpoint;
   public visibility alone is not an eligibility decision or recipient consent.
5. Request a draft with an idempotency key and either exact owner text or dated,
   attributed verified facts. Core uses the seller Persona. A Mac worker draft
   returns a recoverable pending reference; repeat the same request after worker
   completion. No fake recipient Message is created to trigger cold generation.
6. `/pending` or owner API shows exact text, recipient ID/username, seller/account
   and binding. Approve that binding. An edit creates a new pending draft and
   rejects the old approval. Approval binds stable sender identity/session reference
   as well as content, recipient and seller; changed parameters close the gate.
7. Enable desired configuration and owner resume scopes explicitly. `/resume`
   body supports global, seller or dialog. Seller `auto_reply=true` and dialog
   `auto_reply=true` are separate permissions; environment flags must also allow it.
8. `POST /v1/attempts/{id}/send` sends once. Claim persists before I/O; the final
   gate serializes against pause/takeover/opt-out. A crash/unconfirmed RPC remains
   `sending`/`unknown` and is not automatically retried. FloodWait disables the
   account, records a deadline and requires operator recovery.

Management commands: `/start /status /sellers /leads /pending /pause /resume`,
plus `/send <attempt>`, `/edit <attempt> <new text>`, `/takeover <lead>`.
Approval buttons are owner/private-chat authenticated and tied to the displayed
binding. Webhook setup is a later explicit operator operation; startup does not
register it or send messages.

## Replies, consent and delivery history

The optional `python -m app.telegram.listener` consumes new private updates only
for explicit Sales dialog controls. It verifies the selected personal account,
ignores known AI echoes, and treats unknown outgoing messages as human takeover.
Unmanaged dialogs are ignored. No full account history import occurs.

Recipient opt-out commands stop the dialog even during a pause. A recipient can
explicitly send `/gsales_consent 18+ history memory_off` after being given the AI
and history disclosure; this records self-attestation and history consent through
Core. Core must have an OWNER-approved policy permitting that attestation.
Long-term memory stays OFF. Owner approval to contact is never recipient consent.
Missing consent blocks generation but does not prevent subsequent explicit consent.

Pending replies retain only the inbound ID in dialog controls. Listener polling
recovers completed Core responses after restart. If ingestion acknowledgement was
lost, recovery reads only that exact message in that managed dialog. A fresh final
gate still applies; reply uniqueness and a configured maximum (default 10) bound
reply automation. No scheduled unsolicited follow-ups are generated.

Core marks Sales generated assistant text as a draft until channel confirmation.
Undelivered outputs are excluded from conversation/history/summary context.
Confirmed first contact is ingested only after explicit recipient history consent;
until then it remains a protected draft reference, not a fictitious consented
conversation. CRM keeps history-sync metadata and retries only that sync, never
Telegram dispatch. Draft retrieval expires after 24 hours; consent arriving later
cannot resurrect expired content. This retention boundary must be accepted in live QA.

Reconciliation inspects at most 50 messages in the selected managed dialog since
attempt time. A unique matching text is only a candidate: manual text can match.
Owner must confirm the provider message ID after checking evidence. No match or
ambiguous evidence leaves `unknown`; there is no blind resend/reset endpoint.

Registration is not inferred from a reply, click or outreach delivery. The adapter
is fail-closed until an authenticated GPoint service lookup contract exists and is
approved; matching stable Telegram identity plus Creator ID is required.

## Prepared release boundary

Separate package, image, process, port8010, health endpoints, secrets and business
migration receipts. `/health/ready` verifies the non-owner, non-superuser,
non-bypass, non-role-creating runtime role and FORCE RLS; send-enabled readiness
also checks Core identity and transport configuration.

Mac worker is a real existing provider used in the G Sales development profile;
it is not a mandatory production architecture component. Production mode rejects
Mac/test inference and requires an approved always-on server provider through Core.
`GSALES_PRODUCTION_MODE=true` requires Core production readiness. Core must use
`AGATA_SALES_PRODUCTION_MODE=true` and
`AGATA_SALES_ALWAYS_ON_PROVIDER_APPROVED=true` with the selected server provider
and external credentials. These flags are OFF in local QA. Provider selection and
availability acceptance remain pending; current development mode cannot pass
production readiness.
See [verification](verification.md), [release plan](release-plan.md) and [Telegram audit](telegram-audit.md).
Shared Mac worker routing, credential boundaries and the prepared controlled QA
switch are documented in [worker integration](worker-integration.md).
Source preparation is not a production release. Real account/bot E2E and GPoint
registration verification remain explicit external acceptance blockers.
