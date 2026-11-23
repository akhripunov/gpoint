# GPoint Bot token source audit — 2026-11-23

One-time, read-only search for an **existing** Telegram bot token after the Compose → Kamal migration.

Production Bot bootstrap #45147228927 stopped before Apply because `/opt/gpoint/env/production.env` has no nonempty `TELEGRAM_BOT_TOKEN`.

The existing protected deploy will report **presence and file/container source only**, never the token. It will check known production .env locations, stopped legacy Bot container metadata, protected GitHub Environment secret names, and whether the previously verified legacy VPS at 38.180.110.92 remains reachable over a pinned host identity. No token is copied, changed, rotated, or printed. No webhook, DB, container, or product mutations are authorized for this audit.

This newly added exact file is the sole trigger; future pushes skip the audit.