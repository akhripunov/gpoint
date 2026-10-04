# G Sales V1.1 Safe Real Codex QA Recovery Watchdog Plan

> **For agentic workers:** execute inline in this session; keep the installed V2 running until a new exact-SHA approval.

**Goal:** Prepare an independent, time-bounded recovery watchdog and prove restoration through authenticated polling without stopping production V2.

**Architecture:** The one-shot controller records an armed recovery session and starts a detached watchdog before bootout. Main-controller cleanup and watchdog recovery serialize through one per-session file lock and share the same exact LaunchAgent/hash/poll verification. The watchdog starts recovery when its parent exits or at 220 seconds and expires at the 300-second outage deadline.

**Tech Stack:** Python 3, `fcntl.flock`, LaunchAgent/`launchctl`, pytest, existing worker status file.

**Spec:** Owner attachment “G Sales V1.1 / Safe Real Codex QA”, sections 1–5 and 9.

## Global Constraints

- Do not stop the installed V2 until a new exact-SHA OWNER approval.
- Do not run Real Codex QA, deploy, migrations, V3 installation, credential changes, Telegram sending, or production queue mutations.
- Keep recovery evidence in a user-private local directory; do not record secrets.
- Initiate recovery no later than 220 seconds; hard outage deadline is 300 seconds including authenticated polling, leaving up to 80 seconds for bounded QA termination, bootstrap, and poll confirmation.
- A new implementation SHA invalidates the previous QA approval.

## Review Focus

- Main controller killed: detached watchdog notices parent loss and restores V2.
- Normal restore races watchdog: one lock serializes recovery and prevents duplicate bootstrap.
- Poll reports authentication failure: LaunchAgent loaded alone must not count as recovery.
- Test process hangs or controller receives SIGINT/SIGTERM: stop its process group before recovery.
- Bootstrap/bootout fails or the budget is insufficient: leave production up or prioritize recovery, and fail closed.

### Task 1: Recovery lifecycle tests

**Files:** Modify `services/gsales/tests/test_real_codex_controller.py`.

- [x] Add failing tests for parent-crash watchdog recovery, repeated watchdog idempotence, restore lock serialization, authenticated-poll rejection, QA-process termination, bootout/bootstrap exceptions, and depleted outage budget.
- [x] Run focused tests and confirm failures are due to missing lifecycle behavior.

### Task 2: Independent recovery controller

**Files:** Modify `scripts/run-gsales-one-shot-real-codex.py`.

- [x] Add secure per-run state and lock helpers plus watchdog mode (`--watchdog-state`).
- [x] Start detached watchdog before bootout; trigger on parent exit or by 220 seconds; stop at the shared 300-second deadline.
- [x] Serialize main/watchdog recovery and persist completion only after exact artifact hashes and a fresh idle, error-free authenticated poll.
- [x] Terminate any outstanding QA process group before recovery on normal, timeout, SIGINT, SIGTERM, and exception paths.
- [x] Keep CLI preflight read-only and do not invoke the production path in tests.
- [x] Run focused pytest and Ruff.

### Task 3: Record incident and candidate handoff

**Files:** Modify `docs/security/gpoint-agata-worker-token-rotation-handoff-2026-10-04.md`; update relevant Linear canonical doc and Draft PR #11 after verification.

- [x] Record the observed minimum 24m15s gap from last pre-recovery poll to first confirmed post-recovery poll; mark the 300-second objective missed.
- [ ] Publish the prepared implementation SHA and test/review evidence; request new exact-SHA OWNER approval without stopping V2.

### Task 4: Full verification

- [x] Run worker/controller regression, the focused watchdog suite, and static checks.
- [ ] Run `pnpm repo:clean:end` after commit/publish.
- [x] Perform a separate line-by-line review of the final diff and list residual limitations, including total loss of Mac availability.
