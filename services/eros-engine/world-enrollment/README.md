# GPoint application-owned Eros World System enrollment

OWNER decision 2026-11-26: automatic on first valid outgoing user message,
no separate registration or confirmation; do not add a database trigger.
Existing GPoint eligibility, age, privacy and account controls are unchanged.

This supersedes the abandoned trigger candidate. No changes to upstream Rust,
stock SQLx ledger, table definitions, or existing world data. The adapter
opens one bounded pool with an isolated writer role, never the Eros runtime or
DDL credential. It checks the exact authenticated owner, session and active
genome-backed instance before idempotent transactional writes. The worldview
is owner-scoped and a usable existing value remains unchanged. Existing
relationships qualify on their next message. Enrollment happens before
inference, so a validated send with downstream inference failure may still
initialize the world. No backfill.

## Production gates — not yet completed

The existing `scripts/eros-native-database.py` ACL gate explicitly forbids
additional CONNECT roles. Do not bypass that gate or substitute a broader
credential. Before activation, approve the narrow ACL exception, modify
and test the production ACL verifier to require exactly the documented grants,
no membership, no foreign database access, no extra object/column privileges
or secrets exposure. Review inherited PUBLIC ACLs and PostgreSQL RLS behavior;
BYPASSRLS is restricted by column grants, not by RLS, so verify effective
permissions including inherited grants. Check the protected backup/restore
process for this role and grant compatibility. The role starts NOLOGIN.
Provision its login password only through the protected secret lifecycle.
Set `EROS_WORLD_ENROLLMENT_DATABASE_URL` and
`EROS_WORLD_ENROLLMENT_REQUIRED=true` in the protected Core environment.
No credential belongs in source or logs.

Test against the exact stock schema and real engine: two owners sharing a
genome, concurrent first turns, retry, preserved worldview, rejection of a
foreign/archived/BYOC session, role privilege boundaries, transaction failure,
existing chats and generated world events/prompt injection. Current adapter
unit tests cover connection validation and ordering only. The production
release is blocked until these gates and protected ACL provisioning are
complete. Rollback disables the application hook and revokes the dedicated
role without deleting existing worlds. Town feed UI is separate.
