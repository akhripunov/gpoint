# GPoint durable production release flow

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

`docs/release/state/production.json` is the current adoption/release pointer.
`docs/release/manifests/` is append-only release history. A future release must
read the actual live runtime and DB head and block on drift from this state.
