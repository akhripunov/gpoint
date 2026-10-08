# Three-slot Worker bootstrap and lane updates

The first transition from the legacy single-runtime Worker to the three-slot
pool is a **pool-wide runtime transition**. The legacy supervisor does not
resolve `runtime/slots/<LANE>/current`, so creating or changing only the LAB
pointer cannot activate LAB until the new pool supervisor is installed.

Use the canonical `migrate_worker_runtime.py apply` action for that one-time
bootstrap. It replaces the shared supervisor and LaunchAgent runtime, and is
therefore subject to the full transition gates: exact clean `origin/main`
candidate, loaded Worker, no active inference, free transition/release locks,
fresh authoritative GPOINT and GSALES queue snapshots at 0 pending / 0 running,
and successful post-transition polling with rollback protection. If any gate is
unknown, stale, or degraded, do not apply and do not create lane pointers.
This bootstrap does not run GPoint Prepare or Deploy and does not mutate the
VPS; it is still a shared local Worker lifecycle change.

`install-lab-slot` is intentionally **not** a bootstrap command. On a legacy
supervisor it fails with
`worker_pool_bootstrap_required_use_full_transition_apply`, before staging or
changing a pointer. It must never escalate a LAB-only request into a shared
Worker restart.

After the pool supervisor is installed, `install-lab-slot` stages the exact
candidate and atomically updates only `runtime/slots/LAB/current`. It does not
restart the LaunchAgent, change GPOINT/GSALES pointers, or require production
queue 0/0 snapshots. It still requires the transition lock to be free, the
production release lock to be free, no maintenance stop, and LAB slot ownership
to be available. Inference remains fail-closed without a current OWNER LAB
approval.

This separation allows the initial supervisor bootstrap to retain strict
production-lane safety while ordinary LAB-only runtime updates remain
independent of GPOINT/GSALES queue contents.
