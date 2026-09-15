# GPoint V1 foundation

Status: foundation scaffolded; live affiliate proof blocked by missing Telegram entities and credentials.

The repository is a pnpm monorepo with a React/TypeScript/Vite Mini App, NestJS/Fastify API health bootstrap, grammY bot, BullMQ worker boundary, Prisma PostgreSQL schema, and shared contracts. Core product screens are fixture-backed until API persistence is wired; the schema is durable foundation-only and does not pretend to observe Telegram revenue.

Domain truth is split deliberately: `AffiliateClick` is GPoint funnel analytics, while Telegram affiliate attribution/revenue is authoritative and must be imported as evidence. A click never upgrades to a payment or entitlement by itself.

Security invariants: Mini App `initData` is validated server-side; client user IDs and creator ownership are untrusted; secrets are environment-only; no personal Telegram session is used at runtime.
