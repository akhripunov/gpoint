# Core Real Data V2 release report

`GPOINT_CORE_REAL_DATA_V2 = PARTIAL`

## Verified

- Auth dev session: PASS; production rejects dev auth unless explicitly configured.
- `GET /me`: PASS.
- Creator profile API: PASS for approved creators; unapproved profiles are not public.
- Posts API: PASS with cursor-shaped response and published-only filtering.
- Reels API: PASS with published `isReel` posts and approved creators.
- Follow persistence/idempotency: PASS; duplicate follow/unfollow is safe.
- VIP offer API and safe Telegram redirect: PASS.
- AffiliateClick + `VIP_CTA_CLICK` + `AFFILIATE_LINK_OPEN` transaction: PASS.
- Seed: 12 fictional creators, 36 posts, 12 reels, VIP offers, users, roles and events.
- Build: miniapp/api/bot/worker/packages PASS.
- Tests: 6 assertions PASS; workspace test scripts complete.
- Secret scan: PASS.

## Remaining

The Mini App's Creator Profile, Reels, Follow, Profile and VIP interactions still contain local UI state/fixture paths and are not yet fully connected to the new V2 endpoints. They must be migrated before claiming `FRONTEND_FIXTURE_DEPENDENCY = NONE`. Media upload and full admin authorization are also deferred by this phase's scope.

Affiliate remains `UNVERIFIED_EXTERNAL_DEPENDENCY`; no Telegram payout claim is made.
