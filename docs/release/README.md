# GPoint release gate and historical state files

> **Current operations:** The canonical workflow is [GPoint release automation](automation.md), subject to [release authorization](../RELEASE_AUTHORIZATION.md) and the current [GPoint release policy in Linear](https://linear.app/bestuzhev/document/pravila-reliza-gpoint-183dba239394). Successful `main` Prepare automatically invokes the trusted controller and read-only preflight. `READY_FOR_OWNER` is not permission to apply production. This document describes the lower-level fail-closed release gate, not a competing manual workflow.

> **Production bookkeeping:** The authoritative latest production pointer and release manifests are maintained on the separate `gpoint-release-ledger` ref. `docs/release/state/production.json` and `docs/release/manifests/` on `main` are historical checkout snapshots, **not** the authoritative current production state. Automated release execution supplies canonical ledger paths to the release gate. Never infer the current production SHA/DB head from `main` snapshots or replace ledger state with them; verify ledger, immutable evidence and live runtime as the task requires.


`scripts/release-gpoint.mjs` is the canonical release gate. It accepts only a
40-character candidate commit SHA and is fail-closed by default.

```sh
pnpm release:gpoint --dry-run --candidate <remote-verified-sha>
pnpm release:gpoint status
```

The dry-run verifies the remote candidate, creates an isolated clean checkout,
runs the reproducible install/build recipe, inspects the Mini App build SHA,
checks effective compose parity, captures the recorded production SHA/DB head,
and writes a planned manifest without changing production. It does not migrate,
replace containers, switch Caddy, or create a production ref.

Apply mode is reserved for a controlled production release and requires
`--apply --confirm-production-apply` plus the production adapter configuration.
It must use `prisma migrate deploy`, never `prisma migrate dev`. Before a
migration it must create a non-empty GPoint-only backup and record its path,
timestamp, and protection status.

## Recovery model

Recovery inputs are the exact GitHub SHA, the immutable `gpoint-production-*`
ref, protected production environment/secrets, and a restorable GPoint database
backup. Local worktrees and VPS caches are disposable. Code rollback and DB
rollback are separate decisions; migrations are not automatically reversed.

## State

For current production state use the separate `gpoint-release-ledger` ref at
`docs/release/state/production.json`, its matching manifest and verified immutable
deployment evidence, plus fresh live readback where required. The copies at those
paths on `main` are **historical**, not valid as current-state evidence.
The canonical release workflow sets the ledger-backed state/manifest locations
for its gate. Any mismatch or unknown state must block protected mutation.
