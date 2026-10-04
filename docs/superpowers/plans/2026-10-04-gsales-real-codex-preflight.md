# G Sales Real Codex Preflight Implementation Plan

> **For agentic workers:** execute inline in this session; do not dispatch production work.

**Goal:** Prepare a verifiable read-only GO/NO-GO rehearsal preflight without stopping the Mac worker or changing production.

**Architecture:** Read the GPOINT queue and effective TTL from a repeatable-read, read-only transaction inside the verified live AGATA API container over the existing strict-host-key SSH runner. Recheck local worker idleness immediately after the final snapshot. Bound generation, bootout and restore inside one monotonic outage deadline, with a TTL margin. Record the token disclosure and rotation procedure without any secret values.

**Tech Stack:** Python controller and pytest, SQLAlchemy async runtime, Docker exec, existing OpenSSH runner, Markdown incident procedure.

**Spec:** Owner instructions in the current G Sales V1.1 Codex conversation, P0–P2.

## Global Constraints

- Do not stop the Mac worker in this task.
- Do not rotate production credentials without separate owner approval.
- Do not deploy, migrate, install V3, send Telegram messages, or run real Codex inference.
- Do not publish secret values in Git, Linear, logs, or reports.
- Keep PR #11 Draft.

## Review Focus

- Wrong or stale runtime source: reject unless release SHA, API/Core images, tenant, and database identify the live GPOINT runtime.
- Privileged or non-read-only DB query: require read-only transaction, same login, non-superuser/non-BYPASSRLS role, and visible expected tenant.
- Stale queue evidence: query immediately before stopping and recheck local worker/LaunchAgent after the query.
- TTL margin: require effective TTL ≥ total outage cap plus the safety margin; include bootout and restore confirmation in that cap.
- Token rotation: document the only affected bearer credential/connection and coordinated Mac/VPS procedure; never write or display its value.

### Task 1: P0 credential incident record

**Files:** Create `docs/security/gpoint-agata-worker-token-incident-2026-10-04.md`.

- [x] Record exposure fact, credential scope, and a coordinated rotation runbook without secret values.
- [x] Verify the record contains no secret values or partial/hash representation.

### Task 2: P1 authoritative snapshot

**Files:** Create `scripts/gpoint-agata-readonly-queue-snapshot.py`; modify `scripts/run-gsales-one-shot-real-codex.py`; test `services/gsales/tests/test_real_codex_controller.py`.

- [x] Add tests for missing/stale/untrusted snapshots, wrong runtime/tenant identity, and zero/nonzero queue counts.
- [x] Run the focused tests and observe the new cases fail before implementation.
- [x] Implement a remote, aggregate-only query in a repeatable-read READ ONLY transaction using verified runtime identity and the expected GPOINT tenant.
- [x] Fetch a fresh snapshot immediately before bootout and then re-read LaunchAgent/local idle state; never derive zero counts from local status.
- [ ] Approve a live snapshot: dry-run correctly blocks because the running AGATA container's configured tenant does not match the expected GPOINT tenant. The SQL probe scoped to the expected tenant is informational only and insufficient evidence.

### Task 3: P2 total outage budget

**Files:** Modify `scripts/run-gsales-one-shot-real-codex.py`; test `services/gsales/tests/test_real_codex_controller.py`; update `services/gsales/worker-integration.md`.

- [x] Add tests proving the full outage cap reserves process termination and restoration time, and effective TTL includes the configured safety margin.
- [x] Replace the old 220-second/221-second policy with one monotonic deadline from bootout start through confirmed polling resume; cap test time to reserve stop/restore verification.
- [x] Repeat focused tests and Ruff (7 tests pass).
- [x] Run local preflight dry-run: NO-GO, safely blocked on live runtime tenant mismatch.
- [ ] Finish repo clean gate and preserve PR #11 as Draft.
