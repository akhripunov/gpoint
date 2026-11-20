# Native First Release Architecture Refactor Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. Steps use checkbox syntax; this plan is already OWNER-authorized for execution in the existing ME-62 scope.

**Goal:** Replace custom release controllers and duplicate validation with native GitHub Actions, Git, Docker Buildx/GHCR, and Kamal 2 behavior while preserving every required guarantee and reaching the fixed ≥50% release-code reduction target.

**Architecture:** Keep `.github/workflows/gpoint-native-release.yml` as the only production controller. Use GitHub's same-run job dependencies, concurrency, protected Environment, matrix and paths-filter mechanisms; Buildx outputs/attestations; actual Kamal/Docker-installed state; and Kamal's lifecycle/health/rollback commands. Retain the smallest adapters needed for GPoint-specific per-component baselines, migration policy, safety evidence and user-facing smoke where the native tools do not provide equivalent guarantees.

**Tech Stack:** GitHub Actions, pinned `dorny/paths-filter`, Git, Docker Buildx/BuildKit, GHCR, Kamal 2, Prisma, SQLx, Alembic, Python, Bash, Node.js, isolated Docker/PostgreSQL.

**Spec:** `/Users/akhripuniov/.codex/attachments/aedb9b8c-81e9-4400-a2cd-415a4232b7b7/Вставленный текст.txt`; current OWNER correction in ME-62 and PR #181.

## Global Constraints

- One production controller: `.github/workflows/gpoint-native-release.yml`; no staging workflow/resource or replacement controller.
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

Keep the original baseline and selectors unchanged. Record all seven spec measures (workflow LOC; release-specific helper LOC; shared referenced LOC; controller count; unique image builds; custom state/manifest abstractions; ordinary deploy checks). Count new `.hcl`, composite-action, JS, or other files in the same executable inventory. A source relocation is not a reduction.

Initial six-path baseline/current: 4,562 / 3,909 LOC. Target: ≤2,281. Current workflow: 2,399 LOC. Additional work required: 1,628 LOC. The broader workflow-plus-referenced-script measure is 7,876 / 6,973; target ≤3,938. Recalculate from Git objects after every accepted refactor.

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
- Modify: `.github/workflows/gpoint-native-release.yml`
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
- Modify: `.github/workflows/gpoint-native-release.yml`
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
- Modify: `.github/workflows/gpoint-native-release.yml`
- Modify: `scripts/write-kamal-production-manifest.py`, `scripts/verify-kamal-production-baseline.py`, `scripts/verify_native_rollback_evidence.py` only where stock Kamal/Docker/GitHub results are equivalent
- Preserve/update migration and runtime verification tests

- [x] Inventory which deploy steps Kamal 2 already performs: image selection, role update, locking/concurrency, health checks, proxy transition, old-container retention and rollback.
- [x] Retain stock `kamal app version` cross-checks and `kamal deploy`; remove redundant `kamal app details`/`containers` text queries after the Docker inventory is confirmed to verify all roles and exact image identities.
- [x] Retain live role topology, exact installed image digest/source revision, partial-deploy state, DB compatibility and smoke guarantees using live Docker facts where Kamal output is insufficient.
- [x] Test the local workflow contract and pinned Kamal config checks; no Layer7 access was performed. Runtime behavior on the VPS remains NOT_VERIFIED pending separate exact-SHA OWNER authorization.
- [x] Preserve rollback target proof and database non-downgrade invariants.

### Task 5: Simplify migration, Eros and environment gates

**Files:**
- Modify: `.github/workflows/gpoint-native-release.yml`
- Modify: `scripts/eros-native-database.py`, `scripts/prisma_migration_release_plan.py`, `scripts/verify-kamal-migration-parity.py`, `scripts/resolve_native_environment_deployment.py` only after call-graph/equivalence tests
- Test: isolated PostgreSQL, migration-safety, Eros DB and protected Environment boundaries

- [ ] Keep ordinary releases free of migration backup/restore operations when no pending migration exists.
- [ ] Invoke Prisma, SQLx and Alembic stock commands directly where their status/plan output already proves the required result.
- [ ] Keep exact approved migration set, applied migration/checksum immutability, cross-stack compatibility and backup/restore gates when native tools do not enforce them.
- [ ] Keep Eros synthetic E2E conditional on relevant code/data changes and the test isolated from production.
- [ ] Preserve Environment identity and native Deployment success only after Runtime Verify; remove any custom polling or duplicate IDs only if GitHub job/deployment relationships prove exact equivalence.

### Task 6: Verify acceptance and exact reduction

**Files:**
- Update: `docs/release/automation.md`
- Update: `tests/test_simple_release_architecture.py` and existing scenario matrix

- [ ] Run all existing release/scope, migration, OCI, Kamal, smoke, and architecture suites plus `actionlint`, `shellcheck`, and required Docker integration suites.
- [ ] Prove the full 35-scenario matrix through automated fixtures/local isolated services and exact-head hosted GitHub CI/Buildx/GHCR. Do not run production scenarios.
- [ ] Report each function block as native replacement or narrowly retained GPoint code with evidence.
- [ ] Recalculate the unchanged seven measures and confirm ≥50% reduction; no denominator changes or code relocation.
- [ ] Update the existing Draft PR #181 and ME-62 with exact SHA, file scope, local evidence and exact hosted CI run. Keep PR Draft and unmerged.
- [ ] Run repository completion gates; report any pre-existing package/DNS failure precisely.
- [ ] Leave the branch clean with scoped local commits. Production checks remain pending separate exact-SHA OWNER authorization.
