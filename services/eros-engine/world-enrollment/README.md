# Automatic GPoint World System enrollment
Status: review candidate, not approved for production DDL apply. Owner wants
automatic activation on first user message, no separate world registration.
Existing chat eligibility, data handling and account controls remain in effect.
This integration uses one invoker-rights function and one AFTER INSERT trigger
in the dedicated Eros database, no Rust fork, no extra DB access for Core API,
no backfill, no changes to the stock SQLx ledger.
Existing usable worldviews are never overwritten; worlds are owner-scoped.
World simulation and feed generation remain stock Eros functionality; feed UI
is separate.
Production risk contract: exact SQL hash and protected database identity,
verify stock schema and trigger inventory, fresh protected backup with isolated
restore, exact-schema and stock-engine E2E rehearsal (including concurrency,
runtime ACL, latency, rollback), no live apply on failed gates, deploy lock and
bounded timeouts. Rollback removes only integration objects, preserves all
worlds and user data. The ordinary release workflow does not apply this DDL;
a separately approved supported protected operation is required.
The hosted fixture tests a minimal disposable schema and invoker permissions,
not the full Eros engine. Never run fixture.sql on production.
