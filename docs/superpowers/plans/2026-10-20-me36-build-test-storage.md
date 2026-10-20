# ME-36 Docker/Rust build and test storage plan — proposal only

## Goal and scope

Keep the existing Eros 2.2.1 pinned source, ordered patchset, Compose harness, and GPOINT lane. Reuse compatible Cargo artifacts, prevent per-run Docker artifacts from accumulating, and stop expensive builds when macOS free space is critically low. This is local-only; no production, VPS, Worker, GitHub, or inference changes.

This document is a proposal only. The cache-keyed Cargo target, build lock, disk guard, and two-run storage acceptance plan below have not been implemented or approved for implementation. The separate synthetic E2E harness changes (`local-e2e.test.mjs`, `run-local-e2e.sh`, `provider-context-check.mjs`) have been reviewed and exercised in this ME-36 worktree. The E2E proves memory extraction, durable writes, provider-context retrieval and recall through the existing GPOINT queue; it does not use Codex Local or prove live model quality. Cleanup is scoped to exact-run resources and preserves unrelated older ME-36 artifacts.

## Inventory evidence and ownership

Historical read-only inventory from an earlier attempt (the Docker socket was unavailable in that attempt). It is superseded by the fresh snapshot below and must not be used as the current baseline:

| Resource | Last observed size/state | Owner | Needed | Cleanup policy |
|---|---:|---|---|---|
| macOS free disk | about 34 GiB | Host | Yes | Never auto-delete host files; guard new heavy builds at configured thresholds |
| Docker images | 28 images; Docker reported 7.436 GB, 5.015 GB reclaimable | Mixed projects | Some active/pinned | Remove only exact temporary image tags recorded by a ME-36 run and proven unused; preserve pinned Eros images |
| Docker containers | 8 total, 4 active; about 692 KB | Mixed projects | Active containers | Stop/remove only exact containers with the current unique Compose project label |
| Docker local volumes | `me36-rust-cargo-registry`, 443.4 MB, unattached at snapshot | ME-36 shared cache | Yes, for fast repeat builds | Preserve; no shared-volume cleanup |
| Docker BuildKit cache | 73 records; Docker reported 7.191 GB, 2.841 GB reclaimable | Shared builder / mixed projects | Reuse useful entries | No global prune; no reliable per-ME-36 ownership evidence |
| Temporary Rust source clones | about 18 MB each in `/private/tmp` | Prior ME-36 runs, exact run attribution incomplete | Sources may be useful for reproduction | Do not delete without exact manifest and owner proof |
| Rust `target` directories in `/private/tmp` | None found in the last scan | — | No | No cleanup needed |
| Random `me36_*` API image tags | Repeated tags near 1.19 GB reported per tag (virtual size) | Several ME-36 runs; some run ownership not confirmed | Current build outputs only during a run | Track exact run-created tags; avoid summing virtual sizes or deleting unattributed tags |
| Partial `node_modules` in this worktree | about 475 MB after failed offline install | Current ME-36 worktree | May be reusable if install completes | Preserve until the package install state is understood; never clean another worktree |

Docker-reported virtual sizes overlap and must not be summed as physical disk use. Previous incident measurements (77 temporary images, 17.56 GB BuildKit cache, 10.38 GB Rust volumes, 10.4 GB temporary compiler artifacts, about 39 GB Docker.raw) are historical and evidence of accumulation, not a fresh baseline. Before implementation and before measuring acceptance runs, refresh read-only inventory with Docker socket access and attribute only resources that can be proven owned.

### Fresh read-only snapshot after the synthetic E2E runs (2026-10-20)

| Resource | Observed | Ownership and disposition |
|---|---:|---|
| macOS available disk | 46 GiB before and after the runs | Host-wide. No host files were removed. |
| Docker images | 31 total, 1 active, 7.436 GB virtual; 5.721 GB reported reclaimable | 23 `me36_*` tags remain from earlier runs. They are not part of the just-finished runs and have no verified per-run manifest, so preserve them. |
| Docker containers | 1 exited, 0 active | `me36_596467742f-gpoint-worker-1`, exit 0, project label `me36_596467742f`; this predates the latest unique run IDs and is retained pending provenance. |
| Docker volumes | 1, 443.4 MB | `me36-rust-cargo-registry`; shared ME-36 cache, preserve. |
| BuildKit cache | 7.192 GB total: 4.417 GB shared, 2.775 GB private; all marked reclaimable | Shared builder; no safe ME-36-only ownership boundary, so do not prune. |
| Latest run-scoped artifacts | 0 containers/networks/volumes and 0 `me36_<run-id>-*` image tags after cleanup | Exact-run cleanup reported PASS. The four E2E attempts reused cached build steps after the initial build; build duration and physical storage delta were not timed, so the two-run storage acceptance remains unverified. |

The current snapshot supersedes the older 34 GiB free-space line above. The previous large incident totals must not be added to this snapshot because Docker virtual sizes and Docker.raw overlap. The verified E2E run after the image-cleanup fix created and removed all three run-scoped images and its Compose stack; this does not demonstrate two-run Cargo-cache reuse or physical storage improvement.

## Design

1. **Build identity and reusable Cargo cache.** Add a single local build wrapper around the existing pinned upstream checkout and `apply-patches.sh`. Derive a deterministic cache key from the pinned upstream SHA, ordered patch digest, Rust toolchain/container digest, target triple, profile, and relevant Cargo features/configuration. Reuse Cargo registry and `target` volumes for the same key; incompatible keys get separate volumes. Serialize access to a cache key with a lock inside the build container. Do not remove the reusable Cargo cache after a successful run.
2. **Per-run resource manifest.** Assign every test run a random run ID and unique Compose project. Record only resources the run creates: project-labeled containers and network, disposable volumes, and exact temporary image tags/IDs. Cleanup reads this manifest, rechecks labels and unused status, and removes only those exact resources. Make cleanup idempotent and run it on success, failure, timeout, and interruption. Keep the pinned Eros image, reusable Cargo cache, test evidence, and shared BuildKit cache.
3. **BuildKit boundary.** Current evidence identifies BuildKit as shared and does not provide reliable ME-36 record ownership. Do not run `docker builder prune` or `docker system prune`. Enforce disk guard and eliminate duplicate temporary API/worker tags first. If later measurement shows shared BuildKit cache itself exceeds the target, report that separate isolation is required before any scoped pruning is possible; do not create a second builder as part of this minimal change.
4. **Disk guard.** Add a small testable guard using macOS available bytes. Defaults: above 20 GiB proceed; 12–20 GiB warn and report owned stale artifacts; below 12 GiB refuse a heavy build. Thresholds are configurable. The guard never deletes files or Docker resources. Unit tests use injected measurements, so they do not depend on filling the host disk.
5. **Evidence and failure handling.** Emit a redacted run summary with run ID, cache key, free disk before/after, first/repeat build duration, created/removed exact resource counts, cleanup result, and residual owned resources. Do not emit secrets or private payloads. An explicit keep-for-debug option may retain that run's manifest and images; default cleanup remains enabled.

## Implementation sequence

1. Before implementing this proposal, refresh a read-only inventory and verify which existing random image tags and volumes belong to this worktree; preserve unknown/shared resources.
2. Add unit tests first for cache-key separation, same-key reuse, lock behavior, manifest ownership checks, cleanup idempotency, foreign-resource rejection, and disk thresholds.
3. Implement the Cargo wrapper/cache key and lock, then integrate it with the existing pinned build instructions.
4. Add exact-run manifest registration and cleanup to `run-local-e2e.sh` without broad Docker selectors or wildcard filesystem deletion.
5. Add the disk guard before Rust/Docker-heavy work and print a clear stop reason below 12 GiB.
6. Review the full ME-36 diff, including the existing E2E reporting/cleanup changes. Obtain independent functional and security review and resolve findings.
7. Run focused offline tests, then two identical local test runs. Record cache reuse and timing, disk before/after, exact temporary artifact counts, cleanup after a forced test failure, and checks that a foreign image/container/volume remains untouched. No additional real inference.
8. Re-run the available E2E harness only if disk guard permits and required local services are available. Keep memory recall marked BLOCKED if extraction is unavailable; storage optimization cannot change that result.
9. Commit only the clean scoped changes locally, run repository clean gates and the handoff checker, and update existing ME-36 with exact SHA and evidence. No push, PR, Actions, or release.

## Acceptance and stop conditions

- Two identical runs reuse a compatible Cargo target and do not create a second independent compilation cache.
- Per-run containers, network, disposable volumes, and temporary image tags are removed after success and forced failure; reusable Cargo cache and pinned Eros image remain.
- Cleanup is idempotent and refuses resources whose run ownership cannot be proven.
- Cache key changes when toolchain, target, profile/features, pinned source, or patch digest changes.
- Concurrent same-key builds serialize; incompatible keys do not share target artifacts.
- Disk guard passes/warns/stops at the configured boundaries without deleting anything.
- Measured Docker physical storage and host free space improve or remain bounded across the two runs; virtual image size is reported separately.
- No global BuildKit prune is attempted. If the shared cache prevents proving the 5–8 GiB target, report that limitation and keep the implementation safe.
- No production behavior, memory policy, routing, database, Worker, or inference behavior changes.

## Current blockers and prerequisites

- Docker read-only inventory and exact-run cleanup worked through the approved local diagnostic path. Older unattributed ME-36 images/container remain preserved until per-run ownership is proven.
- `pnpm repo:clean:start` previously failed because package registry DNS was unavailable. A targeted test invocation also triggered a workspace install and was interrupted on registry DNS failures; do not claim the repository clean gate passed until it runs successfully.
- Local memory extraction, durable retrieval, provider-context evidence, recall and restart history pass with a deterministic synthetic Worker. Real Codex Local inference and model quality remain unverified and require the separate OWNER grant in the ME-36 master task.

## Risks

- Cargo artifacts may be incompatible despite a matching top-level key if environment flags/features are omitted; key derivation must include all effective build inputs and be tested.
- Cleanup can remove a concurrent run's image if it trusts tag prefixes alone; require the exact manifest plus unused/ownership checks.
- Shared BuildKit usage cannot be safely capped by ME-36 while records are not attributable. Leave it alone and report the measured limitation.
- Random Compose tags currently use `latest`; manifest tracking must not turn that mutable tag into a reusable source of truth. Use immutable IDs for verification and exact run-scoped tags for temporary outputs.
