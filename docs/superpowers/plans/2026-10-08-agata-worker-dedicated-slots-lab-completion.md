# AGATA Worker Dedicated Slots and Conversation Lab Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Implement three fixed, failure-isolated lane slots in the existing AGATA Worker and verify all offline Conversation Lab paths without crossing live-call or production gates.

**Architecture:** Keep one canonical Pool/supervisor, queue protocol, Codex process manager, and installed-runtime model. Bind one execution slot/runtime pointer to each lane, independently poll/recover lanes, and allow local LAB queue work despite remote queue failures. A shared provider lock stays at N=1 until separately approved evidence proves safe concurrency.

**Tech Stack:** Python 3.12 worker; existing Promptfoo 0.123.1 Lab-local tooling; Python unittest/pytest offline tests; Linear MCP.

**Spec:** `docs/superpowers/specs/2026-10-08-agata-worker-dedicated-slots-design.md` and the OWNER task pasted on 2026-10-08.

## Global Constraints

- Fixed mapping: `slot-01=GPOINT`, `slot-02=GSALES`, `slot-03=LAB`.
- One AGATA Worker Pool and canonical Codex execution path; no second Worker/Core/queue/LaunchAgent.
- Effective Codex concurrency stays N=1; no inference is authorized by this task.
- LAB uses its local queue and does not require VPS queue snapshots to operate when production network is unavailable.
- Preserve GPOINT/GSALES job, tenant, Persona/Memory, API, and scheduler contracts.
- Incy/VPN and all system network settings are read-only and outside scope.
- No production deploy/database/schema/prompt changes, live generator/judge/optimizer calls, paid API/API key, or sealed holdout access.
- Update only the existing AGATA Workers, Canonical Architecture, and Conversation Lab Handoff docs; preserve history.

## Review Focus

- A mismatched slot/lane offer is rejected before state, receipt, claim, or inference side effects.
- GPOINT/GSALES network failures do not prevent polling and dispatch from the local LAB queue.
- LAB approval denial/expiry and duplicate delivery remain fail-closed with no retry.
- Three fixed slots remain visible while provider concurrency stays N=1; N>1 requires capability verification.
- A LAB-only runtime switch/recovery never changes GPOINT/GSALES runtime pointers, child PIDs, state, locks, or receipts.

---

### Task 1: Fixed slot identity and isolated execution evidence

**Files:**
- Modify: `services/agata/worker/agata_worker_pool.py`
- Modify: `services/agata/worker/gpoint_mac_worker.py`
- Modify: `services/agata/worker/lab_queue.py`
- Test: `services/agata/tests/test_worker_pool.py`
- Test: `services/agata/tests/test_worker_lab_integration.py`
- Test: `services/agata/tests/test_worker_lab_queue.py`

**Interfaces:**
- Add `SLOT_LANES: dict[str, str]` and reverse `LANE_SLOTS: dict[str, str]` with the three exact bindings.
- `PoolPaths.slot_runtime_current(lane: str) -> Path` returns `runtime/slots/<LANE>/current` after validating the lane.
- `ProcessSlotRunner.start(slot_id, lane, offer, ...)` rejects any mapping mismatch and launches the lane’s runtime pointer.

- [x] Add failing tests for fixed mapping, slot/lane mismatch, and LAB receipt slot identity.
- [x] Run red tests against the old dynamic scheduler and confirm the expected failures.
- [x] Implement the three fixed slot identities, safe path validation, and additive LAB receipt slot identity.
- [x] Run focused pool and LAB queue/integration tests.

### Task 2: Independent polling and lane failure isolation

**Files:**
- Modify: `services/agata/worker/agata_worker_pool.py`
- Modify: `services/agata/worker/gpoint_mac_worker.py`
- Test: `services/agata/tests/test_worker_pool.py`
- Test: `services/agata/tests/test_worker_lab_integration.py`

**Interfaces:**
- `PoolSupervisor.poll_once()` polls each configured lane independently, chooses the lane-pinned idle slot, and returns launched-job count.
- Lane-specific `failed_until` and `lane_errors` remain keyed by `GPOINT`, `GSALES`, `LAB`; production errors never become a global LAB claim denial.

- [x] Add red regressions for both remote lane failures, LAB failure isolation, and production priority.
- [x] Implement independent backoff/claim handling; keep production preference when production work is observable and treat remote visibility errors as lane-local.
- [x] Run focused pool/LAB tests and verify the existing production claim payload path is unchanged.

### Task 3: N=1 provider guard and LAB-only lifecycle boundary

**Files:**
- Modify: `services/agata/worker/agata_worker_pool.py`
- Modify: `services/agata/worker/migrate_worker_runtime.py`
- Modify: `services/agata/worker/install_codex_launch_agent.py`
- Test: `services/agata/tests/test_worker_pool.py`
- Test: `services/agata/tests/test_worker_runtime_migration.py`
- Test: `services/agata/tests/test_worker_transition_controller.py`

**Interfaces:**
- `PoolPaths.provider_lock` is one pool-wide lock acquired before dispatch and held by the inference child through completion.
- `PoolPaths.slot_runtime_current(lane)` identifies the independently switchable lane executor. Full common-runtime/LaunchAgent switches remain governed by the existing lifecycle controller and full safety preflight.
- A lane-only pointer operation may update only `runtime/slots/LAB/current`; it cannot mutate the common supervisor or production slot pointers.

- [x] Add red regressions for shared provider-lock serialization, N=1 enforcement, and lane pointer isolation.
- [x] Implement inherited provider-lock lifetime, three lane runtime pointers, atomic single-lane install receipts, and strict fresh queue checks for production-slot changes; full-pool gates remain intact.
- [x] Verify LAB and GSALES single-slot pointer changes do not switch other lane pointers or the common runtime/LaunchAgent pointer.
- [x] Verify Worker asset inventory remains canonical and exact.

### Task 4: Conversation Lab offline completion audit

**Files:**
- Inspect/modify only for a demonstrated failure: `services/agata/conversation-lab/evaluators/`, `pipelines/lab.py`, and `services/agata/conversation-lab/tests/`.

- [x] Run guarded full offline-safe Conversation Lab suite: 159 passed, 22 expected quarantine/legacy skips.
- [x] Verify DEV/VALIDATION isolation, legacy V1 plaintext guard, sealed holdout unopened, blind packet and OWNER lock, 8 core dimensions, judge isolation, deterministic aggregation, budgets, and optimizer lifecycle gates.
- [ ] Run Promptfoo runner/adapter tests with the project-compatible Python and pinned tooling if available; do not install global tooling or make live calls.
- [ ] Add TDD tests only for concrete offline contract failures. Do not alter Naturalness policy, frozen fixtures, or split membership.
- [x] Record live compatibility, calibration, and optimizer execution as approval-pending; no live calls were made.

### Task 5: Independent review, docs, and final verification

**Files:**
- Update: existing Linear `AGATA Workers`
- Update: existing Linear `AGATA Conversation Lab — Canonical Architecture` for the OWNER-approved fixed-slot/LAB-independence contract
- Update: existing Linear `AGATA Conversation Lab — Handoff` CURRENT STATE block

- [x] Run focused Worker regressions (141 passed), guarded full Lab suite (159 passed, 22 expected skips), Python compile, Naturalness dry-run, and `git diff --check`.
- [x] Complete a separate code-review pass across slot isolation, concurrency, approval, rollback, and no-side-effect contracts; fix scheduler rejection and pipe/process-group cleanup findings and rerun affected tests.
- [x] Confirm this isolated offline work did not change installed Worker, LaunchAgent, Production, GPOINT/GSALES runtime, database, or Incy/network settings.
- [x] Update/read back existing canonical Linear documents while preserving the Handoff history; distinguish IMPLEMENTED from INSTALLED / VERIFIED / APPROVAL_PENDING.
- [ ] Publish a clean exact commit through GitHub; install only after fresh health/queue safety gates pass; keep live calls closed until new approval.
