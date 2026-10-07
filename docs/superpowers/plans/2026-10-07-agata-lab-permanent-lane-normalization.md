# AGATA LAB Permanent Lane Normalization — Implementation Plan

Base: `origin/main` / `7815362bee308fa1849671f2e66d242c28009151`.

## Objective

Keep LAB permanently registered in the existing N=1 AGATA Worker Pool while keeping every inference fail-closed behind a bounded per-experiment OWNER approval. Ordinary Conversation Lab runs must submit through Promptfoo → Lab adapter → canonical LAB queue without LaunchAgent/plist changes, Worker lifecycle operations, or production queue `0/0` snapshots. Production priority and strict lifecycle gates remain intact.

## Phase 1 — Baseline and failing contracts (TDD)

1. Run the current focused Worker Pool, Mac worker LAB, and Promptfoo adapter/runner tests; record baseline.
2. Add tests showing the current gap: LAB remains present without the legacy enable flag, no-approval stays closed without claiming/executing, status distinguishes lane availability from authorization, and production visibility failures block LAB.
3. Add runner/controller tests proving ordinary Lab experiment path does not invoke lifecycle preflight/apply/rollback or production queue snapshots; retain separate lifecycle tests proving queue/idle evidence remains mandatory for true runtime changes.
4. Run new tests and confirm the intended failures before implementation.

## Phase 2 — Permanent lane and independent authorization gate

1. Remove `AGATA_LAB_WORKER_ENABLED` as a lane-presence/authorization switch in the canonical Worker pool and startup/one-shot routing.
2. Always register the existing LAB claimer and queue in the single Pool; leave GPOINT/GSALES configuration, N=1, scheduler weights, and job contracts unchanged.
3. Represent LAB presence and gate state separately. No valid approval means available/idle with `CLOSED_NO_APPROVAL`; approval is revalidated and consumed exactly once immediately before inference; consumed/expired/mismatched approvals remain fail-closed.
4. Verify every production queue claim attempt must succeed with fresh visibility before any LAB claim is considered; no per-experiment PR #45 snapshot is added. Existing lifecycle preflight code remains unchanged.

## Phase 3 — Separate experiment flow from lifecycle control

1. Keep strict lifecycle controller limited to install/migrate/switch/LaunchAgent/rollback.
2. Simplify the ordinary Promptfoo experiment controller to preflight manifest/auth/approval/budget, reserve the durable ledger before submission, submit only to the canonical LAB queue, and await terminal receipt.
3. Remove only ordinary-run requirements for lifecycle operations, LaunchAgent state, runtime changes, and authoritative production queue `0/0`; retain lifecycle APIs and their strict gates.
4. Add offline end-to-end proof with fake Worker/approval/receipts and snapshots proving exact-once execution, priority, approval replay denial, and unchanged plist/PID/runtime.

## Phase 4 — Documentation and independent review

1. Update the existing AGATA Workers operational contract to state permanent LAB presence, separate approval gate, background priority, and lifecycle-only queue snapshot requirement.
2. Update only the CURRENT STATE block in the existing Conversation Lab Handoff; preserve all history. Update Canonical Architecture only if a narrowly necessary operational clarification is absent.
3. Run focused suites, full guarded Conversation Lab offline suite, Worker regressions, lint/compile checks, and `git diff --check`.
4. Perform an independent review, fix all findings, rerun affected tests, verify the isolated checkout is clean after local commit. No push/merge/install/restart/deploy or live inference.

## Acceptance

- LAB is always a lane in the existing pool; approval is a separate fail-closed inference gate.
- Production claims have priority; LAB is blocked whenever GPOINT/GSALES visibility is failed/unknown.
- Ordinary experiments do not call lifecycle or queue-snapshot controls and do not alter plist/LaunchAgent/installed runtime.
- Strict lifecycle safety gates remain for actual lifecycle mutations.
- All requested offline regressions pass; docs agree; no production or live-call actions occur.
