# Unified Release Standard V1

This repository contains a small versioned V1 coalescing library under
`scripts/unified_release_v1/`. It is packaged inside each project repo and has
no network or cross-project runtime dependency. Project workflows, production
state, credentials, and recovery remain independent. Project-specific behavior
stays in the adapter, project configuration, and existing workflows.

## Contract

The canonical lifecycle is `COALESCE → PREPARE → AUTHORIZE → PREFLIGHT → PULL
→ APPLY → VERIFY → EVIDENCE`. Coalescing runs on the trusted Mac runner for a
bounded 60-second quiet window. Only an exact current-main `WINNER` may enter
the paid main Prepare. GitHub lookup failure is `BLOCKED`; stale candidates are
`SUPERSEDED` and must not start a diagnostic hosted job. Deploy remains a
GitHub-dispatched Mac executor and retains project-local authorization,
baseline, locks, rollback, and production evidence.

## Project adapter

`project.json` binds this core to one repository and declares its required
project gates. The coalescing entry point validates both `contract.json` and
`project.json` before making the GitHub main lookup. Do not put credentials or
production values in these files. Projects without Docker can replace image
transport with a separately reviewed adapter while preserving lifecycle and
evidence requirements.

For a new project, copy this versioned directory, create a project-specific
`project.json`, implement only the thin repository adapter and project gates,
then run the V1 contract tests and `python3 scripts/unified_release_v1/check_sync.py
--peer-repo <path>` against the adopted project versions. Pin the adopted
standard version in that project's handoff. A shared remote action/workflow is
intentionally not referenced: no central repository has been published or
authorized, and each project must remain independently releasable.

## Current transition limits

`ghcr-oci` is the intended digest transport. `archive` remains an explicit
transition fallback until authorized GHCR end-to-end and rollback evidence is
recorded. This local contract does not prove that GHCR credentials, VPS pulls,
OCI layer reuse, or the production lifecycle work; those remain external gates.
