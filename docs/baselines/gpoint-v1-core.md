# GPoint V1 Core Baseline

Status: frozen web/core baseline; Telegram live gates remain preproduction.
Date: 2026-09-15
Baseline SHA: `5326a89ee1d489d28d040270b16cfd95aa94246e`.
Baseline tag: `gpoint-v1-core-baseline-2`.
Canonical remote: `git@github.com:akhripunov/gpoint.git`
Branch: `main`
Database migration head: `20260915143000_post_moderation_reason`
Node: `v22.23.1`
pnpm: `11.19.0`

## Architecture

- Frontend: React, TypeScript, Vite, TanStack Query, Telegram Mini App web/core.
- Backend: NestJS, Fastify, TypeScript.
- Bot: grammY.
- Persistence: PostgreSQL with Prisma.
- Cache/jobs: Redis and BullMQ.
- Media: S3-compatible storage abstraction; MinIO is the local integration provider.
- Video V1: original `video/mp4` upload and native browser playback; no FFmpeg.

## Proven product flow

Telegram Mini App → Explore/Reels → Creator → free GPoint content → VIP CTA → approved Telegram destination.

Creator: Profile → Media → Post/Reel → VIP → Moderation → correction/resubmit → publication.
Admin: Creator, content, and VIP moderation queues.
Consumer: Explore, Profile, Posts, Reels, and VIP CTA.

Free content is hosted/displayed in GPoint. VIP content remains creator-side, primarily in a private Telegram channel. GPoint does not collect and redistribute creator Stars.

## Invariants

- `MediaAsset` is canonical media identity; storage keys and signed URLs are implementation details.
- READY media is creator-owned and enforced server-side; cross-creator access is denied.
- Public media requires approved creator and published content; admins can access moderation media.
- Production DEV_AUTH is fail-closed; admin authorization is server-side.
- Browser API CORS allows GET, HEAD, POST, PATCH, DELETE, OPTIONS with configured origins, not a convenience wildcard. Verified regression: OPTIONS 204 → PATCH 200.
- Moderation transitions and durable `ModerationAction` evidence cover Creator/Profile, Post, Reel, and VIP rejection/resubmission flows.
- Bodyless requests do not receive an artificial JSON content type.

## Verification baseline

- Unit: 7 passed / 0 failed.
- Integration: 4 passed / 0 failed (API 2, root 2).
- Core E2E: 22 passed / 0 failed.
- Total: 33 passed / 0 failed.
- Repeatability: 3 consecutive full Core E2E runs passed 22/22 without broad DB reset.
- Clean checkout reproducibility: PASS from remote `5326a89ee1d489d28d040270b16cfd95aa94246e`.
- E2E namespace cleanup: only `e2e-creator-*` creator graphs; User rows are retained; non-E2E rows deleted: 0.
- Typecheck and Mini App/API/Bot/Worker builds: PASS.
- Secret scan: PASS.
- Manual DB mutations during Core E2E: 0. Broad DB reset: NO.

## Deferred / preproduction gates

- `TELEGRAM_LIVE_SMOKE`: `DEFERRED_PREPRODUCTION`.
- `TELEGRAM_AFFILIATE_LIVE_PROOF`: `DEFERRED_PREPRODUCTION`.
- Affiliate status: `UNVERIFIED_EXTERNAL_DEPENDENCY`.
- FFmpeg transcoding, video thumbnails, and multipart chunked upload: DEFERRED.
- Exact mobile viewport matrix: `DEFERRED_UI_HARDENING`.

This baseline proves automated web/core Mini App behavior; it does not prove a real Telegram WebView or affiliate revenue attribution.
