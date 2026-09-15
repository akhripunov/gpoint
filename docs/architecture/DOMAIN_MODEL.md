# Domain model V1

PostgreSQL is the durable source of truth. Users are Telegram identities; Creators are separate business/content identities joined through `CreatorOwner` (OWNER in V1). Creator profiles and posts use auditable moderation states. `AffiliateClick` is a first-party outbound-click event, never proof of Telegram attribution or sale.

`Post` plus `PostMedia` is used instead of a separate Reel table: a reel is a published short-video post selected by the feed boundary. This avoids duplicate content ownership and leaves room for a future subtype without coupling the public API to Prisma models.

Future-only concepts intentionally not implemented: entitlement, subscription observation, recommendation, moderation AI, and payout ledger.
