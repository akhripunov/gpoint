# GPoint Kamal Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align GPoint ME-52 with `Правила релизов NEW` through a separately guarded first cutover and a short ordinary Kamal release.

**Architecture:** Keep the existing exact-SHA four-job CI/build/release shape and stock Kamal configuration, but split the one-time Compose transition from ordinary releases. Ordinary builds publish only Core, AGATA, and Mini App images; production remains behind existing GPoint approvals and runtime verification.

**Tech Stack:** GitHub Actions, Docker Buildx/GHCR, Kamal 2.12.0/Kamal Proxy, Python/pytest, pnpm.

**Spec:** `docs/superpowers/specs/2026-10-27-gpoint-kamal-release-design.md`

## Global Constraints

- Work only in the isolated `/private/tmp/gpoint-me52-kamal-work` checkout; do not modify the dirty GPoint project checkout.
- Local implementation, checks, and local commit only; no push, PR, Actions dispatch/rerun, GHCR publication, production Apply, database mutation, or infrastructure change.
- Preserve exact-SHA identity, protected production Environment approval, strict SSH, immutable amd64 image digests, SBOM/provenance, Kamal Proxy TLS/routing, and all frozen-path/security checks.
- Keep Compose/V5 recovery and controllers until a separately authorized, verified cutover and OWNER retirement decision.
- GSales remains stopped and is excluded from ordinary builds/deployments; Eros, Sensitive Detector, and ACL/pilot remain outside ordinary release.

## Review Focus

- A normal release must refuse a Compose-mode host and never run one-time pause/backup/Caddy mutation steps; retain read-only migration compatibility validation.
- An ordinary build must never publish a GSales image or start its services; test outputs and build matrix.
- Partial component deploy or failed runtime verification must not create GitHub Deployment success; test each failure boundary.
- Missing or mismatched source SHA, digest, platform, or attestation must fail closed; test exact artifact binding.
- Cutover gates must continue to preserve database/volume state and require backup restore rehearsal, inventory, consumer pause, Kamal Proxy TLS/routes, Caddy retirement, and full verification; retain targeted regression coverage.

---

### Task 1: Establish exact current candidate and pin the workflow boundary tests

**Files:**
- Modify: `.github/workflows/gpoint-native-release.yml`
- Test: `tests/test_native_build_boundary.py`
- Test: `tests/test_deploy_evidence_workflow.py`

**Interfaces:** Ordinary workflow consumes outputs from Workspace CI, Python CI, and immutable image Build; the separate first-cutover workflow consumes its own exact-SHA protected request and existing cutover safety scripts.

- [x] Confirm current remote `main` SHA `9ba6488580732897e4398939858243ad3ab34790` using the current GPoint GitHub runbook. GitHub MCP readback confirmed the exact three changed source blobs; local Git transport could not fetch the two upstream commits.
- [x] Add focused failing assertions that the ordinary workflow builds only Core/AGATA/Mini App, has no first-cutover mutation steps, and keeps success gated on verified manifests.
- [x] Run the focused test modules and confirm the new boundary assertions fail against the current workflow.
- [x] Inspect existing jobs, reusable scripts, environment gates, concurrency, and action history; preserve established safe behavior while making only the smallest separation needed.

### Task 2: Separate one-time Compose cutover from ordinary Kamal release

**Files:**
- Modify: `.github/workflows/gpoint-native-release.yml`
- Create or modify: `.github/workflows/gpoint-first-kamal-cutover.yml` (use the repository's established workflow naming convention if one exists)
- Test: `tests/test_native_build_boundary.py`
- Test: `tests/test_deploy_evidence_workflow.py`
- Test: cutover workflow policy tests in the existing relevant test module
- Test: `tests/test_kamal_release_config.py`

**Interfaces:** Ordinary workflow accepts only an already-Kamal production topology and exact immutable image artifacts. Cutover workflow retains the existing inventory, migration parity, encrypted backup plus isolated restore, resource/network checks, strict SSH, exact consumer pause, Compose recovery, Caddy integration, and full Verify gates.

- [x] Add a failing test proving ordinary release rejects Compose mode and contains no cutover-only mutation commands; add config assertions for public Kamal Proxy TLS/routes and inactive Caddy after cutover.
- [x] Extract the one-time cutover path into its own guarded workflow while preserving protected Environment approval, non-canceling production concurrency, exact SHA admission, and current rollback/recovery behavior.
- [x] Reduce ordinary workflow to independent Workspace and Python CI, immutable Build, then stock Kamal Deploy/Verify; no Compose pause, schema change, first-cutover backup/restore, or Caddy mutation.
- [x] Run focused workflow tests and confirm all boundary tests pass.

### Task 3: Restrict ordinary immutable builds to the three NEW release components

**Files:**
- Modify: `.github/workflows/gpoint-native-release.yml`
- Test: `tests/test_native_build_boundary.py`
- Test: `tests/test_native_image_attestation.py`
- Test: `tests/test_native_image_readback.py`

**Interfaces:** Build outputs expose exact immutable source-SHA/digest pairs for Core, AGATA, and Mini App only; each remains `linux/amd64` with SBOM and provenance.

- [x] Add a failing test asserting no GSales image/build output exists in the ordinary workflow and no deploy input can activate GSales.
- [x] Remove GSales from the ordinary build matrix, digest outputs, and deploy inputs without altering the stopped-state verification.
- [x] Run focused artifact-boundary and attestation tests; confirm source SHA, digest, platform, and provenance checks still fail closed.

### Task 4: Document operation modes and verify local handoff

**Files:**
- Modify: `docs/release/automation.md`
- Modify: `docs/release/gpoint-kamal-cutover.md`
- Modify: relevant GPoint release authorization or operator docs only where needed to distinguish local candidate, first cutover, and ordinary release
- Test: all focused workflow/config/evidence tests and repository-prescribed checks

- [x] Update operator docs to state production remains on its verified current topology until separate cutover authorization and successful cutover Verify; distinguish the cutover workflow from ordinary release.
- [x] Run focused tests for the changed workflow/config/evidence paths and applicable policy/security/frozen-path gates; attempt `pnpm repo:clean:end` as required by GPoint instructions (registry DNS blocked dependency install).
- [x] Review complete diff, ensure no secrets or unrelated changes, then create one scoped local commit.
- [x] Run `python3 ~/.codex/bin/gpoint-check-handoff.py --repo <clean_checkout> --handoff <json_evidence_outside_checkout>`; record exact SHA, base, paths, test evidence, and result. Handoff gate PASS; overall task remains LOCAL_PENDING_RELEASE because hosted Prepare and production cutover are not authorized or performed in this locally approved scope.
