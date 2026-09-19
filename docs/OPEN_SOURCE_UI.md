# GPoint UI foundation audit

GPoint keeps its React + TypeScript + Vite, Tailwind, TanStack Query and existing API/data architecture. The projects below were audited on 2026-09-19 using repository metadata and the referenced license files. No source was copied from an unclear-license repository.

| Project | Repository | Audited commit | License | Commercial use | Decision |
| --- | --- | --- | --- | --- | --- |
| shadcn/ui | https://github.com/shadcn-ui/ui | `a87a63b2ca25143d26c8bd0903e4e9bc77b3f824` | MIT (`LICENSE.md`) | Yes, with MIT notice | Adopt patterns/components; no catalogue import |
| Radix Primitives | https://github.com/radix-ui/primitives | `f7ecd5ab16f5e1e820eb5786a1419a98a2d594ae` | MIT (`LICENSE`) | Yes, with MIT notice | Adopt accessible primitive patterns when needed |
| Lucide | https://github.com/lucide-icons/lucide | `ba6751ac45d379f6359d75ef2228cff6ba96c122` | ISC (`LICENSE`) | Yes, with ISC notice | Adopted `lucide-react`, importing individual icons |
| Social UI reference | https://github.com/mnfhobbyprojects/react-social-media-app | `20c294b028ffb709134ed477edf189aa8b11f8b4` | No repository license file verified | Not established | Reference only; no code copied |
| Clonagram | https://github.com/zivavu/Clonagram | `c2f5324c6abfe048d4585c01edf8e02dc2f843df` | MIT (`LICENSE`) | Yes, with MIT notice | UX/reference only; no backend or source copied |
| Adrian Hajdin social_media_app | https://github.com/adrianhajdin/social_media_app | `9fa032dd198c92aa498db82d248c0d93f28ebf5c` | No repository license file verified | Not established | Reference only; no code copied |

## What GPoint adopted

- `lucide-react` is the only new UI dependency; icons are imported individually for tree-shaking.
- The existing GPoint components remain the source of truth for product behavior and visual identity.
- The mobile shell now uses the required viewport meta, dynamic viewport units, safe-area variables and full-width mobile layout.
- Existing API contracts, auth, Query data flow, creator tools and moderation UI were not replaced.

## Deliberately deferred

TanStack Virtual is deferred: the current feed is finite/paginated and no measured DOM-growth problem justified a new runtime dependency. Embla is deferred: GPoint has no current multi-image carousel requirement.
