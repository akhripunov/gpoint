# GPoint application-owned automatic Eros world enrollment

Initialize on the first valid outgoing message; no extra registration or
confirmation. No new triggers, functions, SQLx migrations, backfill or changes
to stock Eros. Preserve existing worldviews and data.

Protected rollout scope: a dedicated login with exact column grants for
owner/session/instance checks and the two world tables, one protected
credential, and the existing Core native environment. No grants to Core's
ordinary database role, transcript access, foreign DB access, role membership
or table-level grants. BYPASSRLS is required by stock RLS without policies;
effective column privileges are independently checked. Connection limit six.
No changes to other projects, existing credentials, provider configuration,
world data or release protection.

Gates: hosted application and ACL tests, built stock Eros E2E before Apply,
exact live database identity and SQLx history, encrypted backup with isolated
restore, exact candidate and ACL checksum, transactional role creation and
readback, candidate Core image connection test without inference or user
writes. Stop on any failed gate. No secret rotation. Existing runner only.
Core requires the writer after protected setup. Subsequent releases recheck
effective ACL. Rollback to pre-feature Core disables new enrollment and
preserves worlds. Role revocation is separate protected recovery after old
Core is verified. Do not restore over live user data to remove the feature.
Real scheduled events, stories and prompt injection require runtime evidence;
synthetic enrollment tests do not prove live model generation or Town UI.
