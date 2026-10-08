# AGATA Worker Bootstrap Deadlock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Allow a one-time legacy-to-three-slot bootstrap to rely on independently verified read-only VPS health/queue evidence when the old Mac Worker cannot reach the gateway, without relaxing queue, idle, lock, post-health, or rollback gates.

**Architecture:** Reuse the existing PR #45 VPS snapshot runner, database read-only snapshot script, and `migrate_worker_runtime.apply()` maintenance/pause/rollback path. Admit only the exact local health-error + queue-404 combination; the VPS fallback must independently prove healthy matching API/core images, exact release identity, tenant binding, read-only database role, and fresh 0/0 queues. After bootstrap, the existing live Worker health and successful-poll checks remain mandatory; failure rolls back.

**Tech Stack:** Python 3.12, existing `unittest`/`pytest` Worker lifecycle tests, canonical SSH/VPS read-only runner.

**Spec:** OWNER task “AGATA Worker: устранить bootstrap deadlock” and `docs/superpowers/specs/2026-10-08-agata-worker-dedicated-slots-design.md`.

## Global Constraints

- Never change Incy/VPN, DNS, routes, proxy, hosts, production API, credentials, or Worker runtime during implementation.
- Never run lifecycle `prepare`, `apply`, `install-lab-slot`, restart, inference, judges, or optimizer as part of code verification.
- Preserve fresh local idle/no-inference, free locks, and authoritative GPOINT/GSALES 0/0 requirements.
- Only exact `worker_gateway_network_error` plus queue `worker_gateway_http_404` may select the independent fallback; all other unverified health/error states fail closed.
- VPS fallback verifies both API/core container health and exact running-image release SHA before a tenant-bound read-only database transaction.
- After any future authorized transition, candidate startup, new Worker health, and successful GPOINT/GSALES polling remain required; failure uses the existing rollback.
- LOCAL-FIRST: local commit permitted; push, PR, merge, Actions, Worker transition, and live E2E require separate explicit authorization.

## Review Focus

- Gateway network errors must not be mistaken for healthy polling; only the independent VPS evidence may admit the queue snapshot, and post-transition polling remains strict.
- A changed/unhealthy API or Core image, release mismatch, tenant mismatch, non-read-only DB transaction, elevated role, stale snapshot, or nonzero queue count blocks the transition.
- A queue job arriving after the verified snapshot remains durable and unclaimed while the old Worker is frozen; the second snapshot after pause blocks any job claimed before freeze.
- Any other Worker health failure, auth error, HTTP status, or queue error remains a hard stop.
- Transition failure returns to the exact prior runtime/plist through existing rollback and never causes a second attempt.

---

### Task 1: Pin the direct-fallback admission contract

**Files:**
- Modify: `services/agata/worker/migrate_worker_runtime.py`
- Test: `services/agata/tests/test_worker_runtime_migration.py`

**Interfaces:**
- Existing `_read_production_queue_status(manifest)` continues to return status with two `VERIFIED` queue snapshots.
- Existing `_read_live_runtime_queue_snapshot(lane, tenant_id)` remains the sole remote queue path and retains its provenance checks.

- [x] Add a failing test proving exact degraded gateway + queue 404 currently rejects before the VPS fallback.
- [x] Add a failing test proving non-allowlisted health/queue errors do not invoke the VPS fallback.
- [x] Add a regression proving valid direct VPS snapshots convert the exact degraded case to verified remote-health/queue evidence and the final strict queue gate passes.
- [x] Implement only the exact allowlisted predicate and permit the local idle inventory checker to accept that predicate pending successful remote validation.
- [x] Run focused lifecycle tests and confirm existing 404, non-404, provenance, freshness, zero-count, and transition-lock tests remain valid.

### Task 2: Verify lifecycle ordering and rollback contract

**Files:**
- Modify only if a failing regression demonstrates a defect: `services/agata/worker/migrate_worker_runtime.py`
- Test: `services/agata/tests/test_worker_runtime_migration.py`
- Test: `services/agata/tests/test_worker_transition_controller.py`

- [x] Extend tests proving the first queue read precedes pause, the Worker is paused before the second read, and a nonempty second snapshot resumes the old PID and aborts before bootout.
- [x] Verify existing post-transition health and successful-poll gates remain intact and use the existing rollback path.
- [x] Verify no queue deletion or enqueue/claim changes were introduced; queued work remains durable in the existing outbox contract.
- [x] Keep unrelated lifecycle code unchanged where existing tests already prove behavior.

### Task 3: Offline verification and handoff

**Files:**
- Update: existing Linear `AGATA Conversation Lab — Handoff` CURRENT STATE block.

- [x] Run focused migration/transition tests (158 passed), guarded Conversation Lab suite (183 discovered, 22 legacy quarantines skipped), Python compile, and `git diff --check`. The repository clean gate is not applicable while this LOCAL_PENDING_RELEASE diff is intentionally present.
- [x] Inspect the exact diff: no GPOINT/GSALES job contract, queue schema, topology, network settings, or installed runtime changes.
- [x] Record direct-fallback evidence and maintenance/rollback bounds in the owner-facing result. The Linear Handoff patch was rejected by auto-review because it would publish local runtime paths and hashes; no retry was made.
- [x] Keep changes local only. No push, PR, merge, Worker transition, or live call.
