# GPoint component release V4 — local engineering groundwork

> Historical design note. ME-42 superseded this implementation approach with
> the GHCR-only model. Do not run its GitHub Release asset instructions or use
> this document as current release authorization. The original details below
> are retained as history.

Status: LOCAL_ENGINEERING_READY / NOT_INTEGRATED / NOT_DEPLOYED.
This is not an authorized partial-release workflow or a performance claim.
Existing V3 remains the ONLY production release process.

## Baseline and dependency facts

- Baseline: origin/main dd6ca175ecb78362aa352b82f15767ba724b2744.
- Goal: quick and economical site changes and isolated service releases.
- Last AGATA FULL release had ~9m11s primary Prepare; independent GSales
  PostgreSQL regression took 274.503s; Docker image builds 64.819/34.629/
  19.879s; full archive upload 130.8s, load 41.8s; stages can overlap.
- GPoint image combines API, Bot and Worker, so these remain one image unit.
- AGATA runtime is shared by GPoint AGATA and GSales AGATA consumers.
- Current Caddy site path /opt/gpoint/app/miniapp is a real directory, so
  atomic symlink switching requires a guarded one-time migration.

## Local-only code implemented

1. scripts/release_component_impact_v4.py — conservative exact-SHA full-diff
   planner; rejects mixed/security/DB/CI/unknown paths, deletes, symlinks,
   unsafe git status and missing provenance. Narrows only UI/AGATA response
   source paths. Flags maySkipV3Gates and mayDeploySelectively are false.
2. scripts/release_component_manifest_v4.py — offline per-role image metadata
   for inherited and new revisions, with SOURCE SHA DISTINCT FROM RELEASE SHA.
   Existing immutable COMPLETE receipt, native VPS image IDs, compatibility
   and owner authorizations must be independently proved before apply.
3. scripts/release_ui_artifact_v4.py — deterministic static ZIP with exact
   candidate SHA and per-file integrity; rejects traversal, symlinks, extra
   members, hash mismatch and oversized archive expansion.
4. scripts/release_ui_switch_v4.py — offline immutable directory staging,
   atomic symlink switching and rollback. Refuses to overwrite real live
   directory or rollback after concurrent source change.
5. infra/production/docker-compose.yml and docker-compose.gsales.yml now
   support per-role optional image source-SHA overrides (APP/AGATA/GSALES).
   Their defaults resolve to the legacy RELEASE_SHA, as tested by actual
   Docker Compose config rendering with override absent and present. The
   shared AGATA image maps to both GPoint and GSales AGATA consumers.
6. 31 local new unit tests PASS, 9 existing V3 impact tests PASS, real
   Docker Compose config fallback PASS. No GitHub Actions or production
   mutations were performed in this work.

## Integration with V3 Prepare (still advisory)

The V4 source plan is now invoked as an additional read-only step after the
existing V3 exact-SHA impact analysis during Prepare. It recomputes the git
diff from V3 immutable baseline and emits an advisory JSON outside checkout.
The output does NOT feed Prepare gate selection, receipts, image builds,
preflight, Deploy or production ledger. Any advisory failure stops the
candidate without applying changes. New unit regressions run in the existing
preflight-coordinator test step. Activation awaits the remaining milestones.

## Verified next engineering stage: component provenance and CI static artifact

Added source-backend-safe V4 evidence without changing the actual trusted V3
production apply:
- scripts/release-component-bundle-v4.mjs strictly verifies current immutable
  parent COMPLETE and each new/inherited role image identity/digest/revision,
  plus shared AGATA/GSales dependency provenance. It can derive the image
  baseline from existing V3 ledger artifactIdentities, including GSales-AGATA
  (which uses the AGATA runtime image rather than GSales runtime).
- scripts/release-component-vps-proof-v4.mjs builds only read-only Docker
  inspect commands and verifies native image IDs/labels/platform and each of
  eight running API/Bot/Worker/AGATA/GSales containers. The command was actually
  executed read-only on GPoint VPS against canonical exact release
  dd6ca175ecb78362aa352b82f15767ba724b2744: PASS for all 3 roles.
- scripts/prepare-release.py generates a deterministic static Mini App
  miniapp.zip during canonical V3 UI_ONLY Prepare (additional advisory asset).
  The normal 3-image V3 receipt, mandatory gates and image archive are NOT
  replaced or skipped. scripts/release_assets.py validates and publishes the
  extra ZIP using the same immutable exact-SHA GitHub Release, with receipt
  SHA256 and fail-closed UI_ONLY proof.
- scripts/release-gpoint.mjs adds componentImages and miniappSourceSha to
  successful FULL V3 manifest and ledger, without altering existing global
  SHA and recovery semantics. This identity is additive only.
- New Node and Python regression coverage added to existing Prepare tests.
  Existing Node regressions were adjusted to recognize the now-versioned
  Compose image fallback and explicitly copy the required new module into
  paths containing spaces.

**Proof:** 130 release Node tests PASS and V3 + V4 Python suites PASS. The
V4 native VPS proof used read-only Docker inspection only. No image, container,
schema, current ledger, source files, or live static directory was modified.

**Still NOT a fast Deploy.** To satisfy the owner-facing objective, the
canonical V3 selector/cache/bundle preflight/Apply/ledger must be upgraded
atomically to understand a V4 receipt and distinct source revisions for
inherited components. The source remains a draft PR, not main or production.

## Not done — required next integration, no shortcuts

- [ ] Wire component plan to trusted V3 Prepare / exact production ledger.
- [ ] Produce/verify versioned immutable static artifact for UI in CI.
- [ ] Add actual selective Docker build/transport only after full dependency,
      source and PostgreSQL/HTTP integration-test coverage proofs.
- [ ] Upgrade scripts/release-bundle.mjs (today schema 1, all 3 candidate
      SHA images mandatory) to per-component digests, parent immutable COMPLETE
      receipt, native inherited image parity and explicit provenance.
- [ ] Extend scripts/release-gpoint-vps-adapter.mjs, compose rendering,
      scripts/release-gpoint.mjs and post-release reconciler to support real
      selectively switched components plus health/backup/rollback/recovery.
- [ ] Evolve production ledger schema, controller and read-only preflight
      atomically so inherited image source labels need not match candidate
      release SHA but always prove original immutable provenance.
- [ ] Safely migrate existing real Caddy UI directory to versioned symlink
      under explicit exact-SHA release, preserving previous site and rollback.
- [ ] Prove E2E with disposable tests for UI text/block, AGATA, shared code,
      schema/security changes, missing image, drift and interrupted deploy.
- [ ] Collect actual hosted Actions minutes, transfer bytes and timings.
- [ ] Require applicable OWNER exact-SHA approval for production Deploy.

Not allowed: a new uncontrolled release path, treating COMPONENT_CANDIDATE
as deployment approval, bypassing migrations/security/Worker/health gates,
touching MAXIBOT, or publishing instructions solely to spend Actions.

## 2026-10-13: integrated STATIC_UI_ONLY implementation

A separate typed schema 2 / STATIC_UI_ONLY receipt now supports proven
Mini App-only exact-SHA diffs inside existing V3 GitHub main Prepare -> pinned
artifact -> Mac preflight -> OWNER exact-SHA approval -> Deploy/ledger.

- Prepare runs Vite build, Mini App test and pnpm test:release, validates
  immutable COMPLETE production source baseline, and creates deterministic
  SHA256-member-attested static ZIP without Docker images or disposable DB
  when and only when V3 proves UI_ONLY. Mixed/unknown changes remain FULL.
- Release bundle, asset publication and Mac artifact cache recognize strictly
  differentiated legacy V3 and V4 static typed receipts.
- Preflight checks main SHA, verified parent, native live Docker identity and
  eight containers, AGATA/GSales/Worker/config, DB migration identities,
  current site directory, release journal and immutable source diff.
- Apply uses canonical production lock, no container restart, unchanged
  runtime RELEASE_SHA in production.env, no schema mutation, staged immutable
  ZIP, Linux atomic same-filesystem RENAME_EXCHANGE, journalled rollback,
  external HTTP route and API smoke, native Docker parity, and release ledger
  source-vs-runtime version vectors. Ambiguous recovery is fail-closed.
- Multiple static releases can inherit the same runtime images. Full V3
  baseline checks now distinguish served UI SHA from running image revision.
- Synthetic single-file UI change in disposable local worktree: fast Prepare
  on warm Mac took 24.20 seconds, yielding 1.1 MiB static ZIP, schema2
  receipt verified, 165 Node release tests PASS; no VPS mutation. Cold first
  run exposed Python 3.9 incompatibility after 42.77 seconds; fixed and
  successfully reran. This is NOT a hosted GitHub Actions measurement.
- Linux Python 3.12 Docker atomic exchange/rollback tests: 16 PASS.
  Actual VPS read-only exact ledger/native parity for all 8 containers:
  PASS. Current root is a real directory, supported without symlink migration.

**Outstanding before production:** hosted branch rehearsal and independent
review, then FULL bootstrap integration to main, Prepare and read-only
preflight, new exact-SHA OWNER approval and actual production Deploy.
Afterwards a separate UI-only commit must complete hosted-to-production
cycle for time/Actions-minute measurement. The AGATA, API, Bot and Worker
selective-image routes are NOT implemented; their changes continue FULL V3.
Do not claim fast backend deployments or full production speed until measured.
