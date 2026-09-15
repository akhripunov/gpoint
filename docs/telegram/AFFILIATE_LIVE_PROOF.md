# Telegram affiliate live proof

## Release status

`AFFILIATE_PROOF = BLOCKED` — no Telegram test/production bot, Mini App, affiliate peer, referral URL, or clean test user was supplied or available in this environment. No payment or commission claim is made.

Latest attempt: 2026-09-15, Europe/Moscow. No Telegram credentials were present in the environment. Dependency retry reached npm registry DNS failure (`ENOTFOUND`), therefore `NPM_REGISTRY_STATUS = ENVIRONMENT_BLOCKED`.

## Manual action required

MANUAL_ACTION_REQUIRED

The smallest prerequisite sequence is:

1. Seller owner creates `@seller_test_bot` in @BotFather, enables a Main Mini App pointing to the deployed HTTPS Seller App, and confirms the seller bot can issue an `XTR` invoice for `Test VIP Access`.
2. GPoint owner creates `@gpoint_affiliate_test_bot` in @BotFather. Do not share its token in chat or commit it.
3. Seller owner enables the official affiliate program for the seller Mini App, preferably with `commission_permille=200` and no `duration_months`, then provides only sanitized bot username/ID and settings.
4. GPoint owner uses the official Telegram client as the owner of the GPoint bot to connect that bot as the affiliate peer. This is a user-only MTProto operation; provide the generated referral URL with its token redacted only if the URL cannot be safely shared.
5. Confirm a clean buyer account that has never opened the seller Mini App or purchased from it.

After confirmation, the proof can continue with baseline balances, first purchase, repeat purchase without relaunching the referral URL, and Telegram Bot API transaction evidence. Do not send bot tokens, API hashes, payment credentials, or personal session strings.

## Required run record

- Timestamp: not run
- Seller bot / username: not provisioned
- Seller Main Mini App: not provisioned
- Affiliate peer: not provisioned; target is a GPoint-owned bot
- Commission / duration: not configured
- Sanitized referral URL: not generated
- First purchase / seller Stars / GPoint commission: not observed
- Second purchase without ref link: not observed
- Existing-user behavior: documented unknown

## Fail-closed decision

The architecture cannot be approved until a clean referral produces both seller-side successful payment evidence and Telegram-side affiliate revenue evidence, and the same referred user produces commission on a second purchase without reopening the referral URL. Mocked unit tests are not live proof.

## Manual runbook

1. Create seller bot and enable its Main Mini App in @BotFather.
2. Implement a minimal Stars `XTR` invoice in that Mini App and verify seller-side `successful_payment`.
3. Seller owner enables affiliate program in official Telegram UI/MTProto with a test commission.
4. GPoint owner connects a GPoint-owned bot peer using official Telegram client flow and records the returned URL without secrets.
5. Use a clean Telegram account, follow the URL, start the Mini App, buy once, record seller transaction and affiliate balance/`TransactionPartnerAffiliateProgram` evidence.
6. Re-enter normally and buy a second time; record both sides again.
