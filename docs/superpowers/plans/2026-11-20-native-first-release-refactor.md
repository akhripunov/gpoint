# Native First Release Architecture Refactor Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. Steps use checkbox syntax; this plan is already OWNER-authorized for execution in the existing ME-62 scope.

**Goal:** Use the simplest Native First architecture that preserves every required guarantee, replacing duplicate custom release behavior with native GitHub Actions, Git, Docker Buildx/GHCR, and Kamal 2 mechanisms where that materially simplifies the system.

**Architecture:** Keep `.github/workflows/gpoint-release.yml` as the only production controller. Use GitHub's same-run job dependencies, concurrency, protected Environment, matrix and paths-filter mechanisms; Buildx outputs/attestations; actual Kamal/Docker-installed state; and Kamal's lifecycle/health/rollback commands. Retain the smallest adapters needed for GPoint-specific per-component baselines, migration policy, safety evidence and user-facing smoke where the native tools do not provide equivalent guarantees.

**Tech Stack:** GitHub Actions, pinned `dorny/paths-filter`, Git, Docker Buildx/BuildKit, GHCR, Kamal 2, Prisma, SQLx, Alembic, Python, Bash, Node.js, isolated Docker/PostgreSQL.

**Spec:** `/Users/akhripuniov/.codex/attachments/aedb9b8c-81e9-4400-a2cd-415a4232b7b7/Вставленный текст.txt`; current OWNER correction in ME-62 and PR #181.

## Global Constraints

- One production controller: `.github/workflows/gpoint-release.yml`; no staging workflow/resource or replacement controller.
- Preserve exact SHA, per-component installed baselines, production concurrency, migration safety, rollback, immutable digest identity, healthchecks and smoke.
- No code relocation or denominator change may be counted as LOC reduction.
- Keep ordinary release conditional: no unnecessary image builds, backups, Eros tests or migration gates.
- No production VPS/DB operation, secret/protection change, or MAXIBOT access.
- Keep PR #181 Draft/open/unmerged; update only through the already authorized scope.
- Publish no status as production-ready. Production Kamal Deploy, Runtime Verify and smoke remain gated on separate exact-SHA OWNER authorization.

## Review Focus

- A candidate after a missed/failed/partial release must diff from actual installed component SHA; test distinct baselines and accumulated commits.
- Unknown, renamed, deleted or reintroduced staging/control-plane paths must fail closed or select the required protected route.
- A candidate with changed `main` head, invalid ancestry, split role revisions, or a stale baseline must not be applied.
- Shared Docker contexts must select every affected image; each unique image must build once and resolve to one verified GHCR digest.
- Pending/destructive migrations, Eros schema changes, rollback compatibility, and failed health/smoke must retain their current fail-closed outcomes.

## Function inventory and replacement hypotheses

| Block | Current function | Native replacement to prove | Custom code retained only if |
|---|---|---|---|
| Tests job / exact-SHA evidence | CI, baseline discovery and event/path admission | `needs`, `github.sha`, job outputs, `concurrency`, protected `Environment`, pinned `dorny/paths-filter` | Installed component identity or unknown-path admission needs a small adapter |
| Build job | Four image builds, per-image metadata, provenance and GHCR readback | Buildx Bake or a static component matrix; BuildKit metadata; GHCR pull by digest; GitHub artifact attestation verification | Exact GPoint source, Eros upstream, role and component identity cannot be asserted by native action outputs |
| Baseline discovery | Per-component live source SHA, image, roles and split-version validation | Stock `kamal app containers/details/version/images`, `docker inspect`, OCI labels | Kamal output lacks a stable machine-readable component/role baseline or mixed-role fail-closed proof |
| Component planner | Cumulative exact-base file classification, build/deploy selection, unknown-path guard | Per-component `dorny/paths-filter` with exact SHA base, Git ancestry, workflow expressions and matrix | GitHub's filter cannot express the required dynamic base/unknown-path contract declaratively |
| Migration gates | Prisma/Alembic/SQLx chain, pending exact set, backup/restore and compatibility | Native Prisma/SQLx/Alembic commands plus PostgreSQL | Cross-stack approval, applied-history immutability, compatibility or recovery contract is not supplied by those tools |
| Kamal topology/deploy/verify | Role topology, selected deploy, health, image identity and smoke | Stock Kamal 2 config/app/deploy/rollback/healthcheck and GitHub Environment | Exact production role identity, digest or public user-flow smoke needs additional proof |
| Recovery/result | Partial outcome, rollback target, deployment status and release evidence | Kamal old-container inventory/rollback, native GitHub Deployment status/summary | Need to bind exact prior verified SHA/digest, preserve DB, or represent partial multi-component results |

## Fixed measurement

Keep the original baseline and selectors unchanged. Record all seven spec measures (workflow LOC; release-specific helper LOC; shared referenced LOC; controller count; unique image builds; custom state/manifest abstractions; ordinary deploy checks). Count new `.hcl`, composite-action, JS, or other files in the same executable inventory. A source relocation is not a reduction. OWNER update 2026-11-20: the 50% reduction is informational, not a blocking acceptance requirement; do not rewrite code or move complexity solely to lower LOC.

At exact PR candidate `0b721128f910748fe3643ffe1b83de7d9f27559b`, fixed measures are: six-path controller/adapters 4,562 / 3,871; workflow 2,437 / 2,363; workflow plus referenced helpers 7,876 / 6,935; extended inventory 12,486 / 9,098. Report recalculated values after material refactors; no percentage threshold gates acceptance.

## Tasks

### Task 1: Pin current behavior and architecture map

**Files:**
- Modify: `docs/release/automation.md`
- Create/update: `tests/test_simple_release_architecture.py`
- Test: existing component-scope, migration, Kamal, OCI and recovery tests

- [x] Map each workflow step and referenced helper to one row in the function inventory, its invariant, native replacement candidate, consumer, and existing test evidence.
- [x] Identify the ordinary no-migration/no-Eros path and count its required GitHub jobs, image builds, external operations, scripts, and safety checks.
- [x] Add architecture tests for the single controller, zero staging resources, exact-SHA job dependencies, conditional migration/Eros operations, and fixed LOC selectors.
- [x] Run the new tests RED before any implementation changes.
- [x] Recompute all seven measures from base `3d680fa153fd6aebf786455d6f36d3b1a67cc78f` and candidate `HEAD` with a checked-in reproducible script/test; make no selector or denominator changes.

### Task 2: Replace duplicate component intent logic with native path filters

**Files:**
- Modify: `.github/workflows/gpoint-release.yml`
- Modify: `scripts/native_release_intent.py` only for behavior not expressible by Actions/Git/paths-filter
- Modify: `scripts/resolve_native_build_scope.py` only for live baseline facts not exposed by Kamal
- Test: `tests/test_native_release_intent.py`, `tests/test_native_build_scope.py`, `tests/test_release_scope_consistency.py`

- [x] Add behavior tests for two different live baselines, cumulative changes, renames/deletions, shared paths, unknown paths, invalid ancestry, no-op and split role versions.
- [x] Verify from the pinned action's input/output contract that an exact commit SHA can be used as one `base`; the production planner cannot be replaced by one filter because each component has a different installed SHA. A matrix still needs custom fail-closed aggregation, so it does not remove the planner.
- [x] Remove the redundant same-run Eros test evidence API lookup; the existing `needs.tests` success dependency already blocks Build unless the exact workflow's test job succeeds.
- [x] Keep installed identity from verified Deployment baselines and live Kamal/Docker inventory; both remain fail closed on unprovable identity.
- [x] Keep current path-filter coverage for shared Docker/build inputs and negative tests proving unknown additions cannot be silently ignored.
- [ ] Run focused Python/Action tests, actionlint, scope consistency and the full Python suite. Focused scope suite passes (139); actionlint is unavailable locally; full collection is blocked by Python 3.9/missing test dependencies and will be covered by exact-head hosted CI.

### Task 3: Consolidate Buildx image builds without moving complexity

**Files:**
- Modify: `.github/workflows/gpoint-release.yml`
- Add or modify: tracked Buildx definition only if total release-code LOC decreases and outputs remain exact
- Modify: image identity/readback tests and production image verification only for equivalent native Buildx/GitHub output

- [x] Preserve one build per image, exact candidate source, Eros upstream SHA, platform, role labels, cache isolation, immutable tags, SBOM and provenance.
- [x] Compare the four build-push steps with Buildx Bake metadata and count every new executable/config line in the same total. Bake would require per-target HCL plus a JSON digest mapper; the current four direct steps already expose one digest per image, so Bake is not a reduction.
- [x] Existing scope and architecture tests cover selected component/image sets and exact digest output mapping.
- [x] Retain GHCR digest pull/readback and current provenance verifiers; no equivalent proof for all GPoint/Eros assertions was demonstrated.
- [x] Keep custom verifiers for assertions not proven by native output.
- [x] Existing exact-head hosted CI on the prior candidate passed all four Buildx image builds and GHCR digest readback. The new candidate still requires a fresh hosted check after the stale unit contracts are fixed.

### Task 4: Reduce deploy and verification orchestration to stock Kamal

**Files:**
- Modify: `.github/workflows/gpoint-release.yml`
- Modify: `scripts/write-kamal-production-manifest.py`, `scripts/verify-kamal-production-baseline.py`, `scripts/verify_native_rollback_evidence.py` only where stock Kamal/Docker/GitHub results are equivalent
- Preserve/update migration and runtime verification tests

- [x] Inventory which deploy steps Kamal 2 already performs: image selection, role update, locking/concurrency, health checks, proxy transition, old-container retention and rollback.
- [x] Retain stock `kamal app version` cross-checks and `kamal deploy`; remove redundant `kamal app details`/`containers` text queries after the Docker inventory is confirmed to verify all roles and exact image identities.
- [x] Retain live role topology, exact installed image digest/source revision, partial-deploy state, DB compatibility and smoke guarantees using live Docker facts where Kamal output is insufficient.
- [x] Test the local workflow contract and pinned Kamal config checks; no Layer7 access was performed. Runtime behavior on the VPS remains NOT_VERIFIED pending separate exact-SHA OWNER authorization.
- [x] Preserve rollback target proof and database non-downgrade invariants.

Task 4 hosted feedback: exact SHA `1d058bc0aa708656f5c142f996ad82fa15270d21` caught shellcheck SC2016 from literal Markdown backticks in a single-quoted `printf`, plus old tests expecting the removed detail/container output. Removed the formatting trigger and rewrote the obsolete test contract. Updated local boundary/scope suite: 196 passed; the corrected exact SHA is pending hosted validation.

### Task 5: Simplify migration, Eros and environment gates

**Files:**
- Modify: `.github/workflows/gpoint-release.yml`
- Modify: `scripts/eros-native-database.py`, `scripts/prisma_migration_release_plan.py`, `scripts/verify-kamal-migration-parity.py`, `scripts/resolve_native_environment_deployment.py` only after call-graph/equivalence tests
- Test: isolated PostgreSQL, migration-safety, Eros DB and protected Environment boundaries

- [x] Keep ordinary releases free of migration backup/restore operations when no pending migration exists; the workflow gates the backup and database Apply on the exact `migration_plan.apply` output.
- [x] Invoke Prisma, SQLx and Alembic stock commands directly where their status/plan output proves the framework-specific result; Alembic graph discovery already uses native `ScriptDirectory`.
- [x] Keep the exact approved migration set, applied migration/checksum immutability, cross-stack compatibility and backup/restore gates. Prisma `migrate deploy` applies every pending migration and does not check schema drift, so it cannot replace these GPoint release boundaries.
- [x] Keep Eros synthetic E2E conditional on relevant code, data or infrastructure baseline changes, isolated in the CI workspace.
- [x] Preserve protected Environment identity and native Deployment success only after Runtime Verify; no custom polling/identifier removal was proven equivalent.

### Task 6: Verify functional acceptance and record complexity

**Files:**
- Update: `docs/release/automation.md`
- Update: `tests/test_simple_release_architecture.py` and existing scenario matrix

- [x] Run the focused release/scope, OCI, Kamal and architecture suites locally (201 passed on the docs-updated candidate); exact-head hosted run `44757686891` passed Python, Workspace, actionlint, isolated PostgreSQL, Eros/Mini App E2E, Buildx and GHCR digest read-back.
- [x] Record the 35-scenario matrix using automated fixtures, isolated services and exact-head hosted evidence. Production-only actions remain NOT VERIFIED and are not exercised without separate exact-SHA OWNER authorization.
- [x] Report each major function block as a native replacement or narrowly retained GPoint code in `docs/release/automation.md`; the block audit found no additional material safe simplification after the dorny, same-run dependency, prepared-run and duplicate Kamal-query removals.
- [x] Recalculate the unchanged LOC measures for information only; no denominator changes or code relocation.
- [x] Update the existing Draft PR #181 and ME-62 with exact SHA, scope, local evidence and exact hosted CI run. Keep PR Draft and unmerged.
- [x] Attempt repository completion gates and record their existing environment failures: `pnpm repo:clean:start` could not resolve `registry.npmjs.org` (`ENOTFOUND`); `CI=true pnpm repo:clean:end` fails with `ERR_PNPM_LOCKFILE_CONFIG_MISMATCH` because current pnpm ignores `pnpm.overrides` in `package.json`. Neither command changed tracked files; no dependency or lockfile change is included.
- [x] Leave the branch clean with scoped local commits. Production checks remain pending a separate exact-SHA OWNER authorization after an integrated main SHA is selected.
