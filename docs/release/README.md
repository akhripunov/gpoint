# GPoint production release flow

> **Current model (Native First V3 + Kamal 2):** the single
> `gpoint-release.yml` workflow runs required Tests, Buildx, immutable GHCR
> publication, protected Deploy, Runtime Verify and smoke. Every eligible PR
> validates all three Core/MiniApp/Eros images; the long Eros SQLx and synthetic
> dialogue E2E runs only when the shared path map finds Eros impact or a safe
> cumulative check cannot be made. Deploy selection comes from live Kamal/Docker
> inventory. Preserve migration, backup, rollback and production approval gates.
> The procedures below are historical only; see the current model at the top of
> [automation.md](automation.md).

## Historical design (superseded; do not run)

`scripts/release-gpoint.mjs` was the legacy release gate. It was **removed on
2026-11-12** together with the legacy Prepare/Deploy workflows; the ordinary
release path is `gpoint-release.yml` (Tests → Build → protected Kamal 2
Deploy → Runtime Verify). The commands below are shown only as history.

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
