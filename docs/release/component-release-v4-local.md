# GPoint component release V4 — local engineering groundwork

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
5. 28 local new unit tests PASS; 9 existing V3 impact tests PASS. No GitHub
   Actions or production mutations in this work.

## Integration with V3 Prepare (still advisory)

The V4 source plan is now invoked as an additional read-only step after the
existing V3 exact-SHA impact analysis during Prepare. It recomputes the git
diff from V3 immutable baseline and emits an advisory JSON outside checkout.
The output does NOT feed Prepare gate selection, receipts, image builds,
preflight, Deploy or production ledger. Any advisory failure stops the
candidate without applying changes. New unit regressions run in the existing
preflight-coordinator test step. Activation awaits the remaining milestones.

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
