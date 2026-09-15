# Telegram affiliate program findings

Verified 2026-09-15 against the official Telegram documentation:

- Affiliate programs are opened for a bot's Mini App using MTProto `bots.updateStarRefProgram`.
- `commission_permille` is a share per 1000 Stars; 200‰ = 20%.
- `duration_months` is optional; absent means no expiration.
- An affiliate can be the current user, an owned bot, or an eligible owned channel. GPoint's preferred peer is a GPoint-owned bot.
- The generated URL is returned by `payments.connectStarRefBot`, a user-only MTProto method. Affiliate management is therefore not a Bot API capability and must not be faked in the bot runtime.
- A user is a successful referral when they start the Mini App for the first time after following the referral link; purchases in the Mini App during the period earn commission.
- Owners can only raise commission/duration in place. To lower them they terminate the program; termination invalidates links after the documented end time, while already-attributed users can continue earning for their original period.

Conclusion: `TELEGRAM_AFFILIATE_PURCHASE_SURFACE = MINI_APP_REQUIRED` for the affiliate proof. A bot is required for payments, but a bot-only payment has not been proven to qualify for Mini App affiliate attribution.

Sources: [Affiliate programs](https://core.telegram.org/api/bots/referrals), [Bot Payments API for Digital Goods](https://core.telegram.org/bots/payments-stars), [Telegram Mini Apps](https://core.telegram.org/bots/webapps).
