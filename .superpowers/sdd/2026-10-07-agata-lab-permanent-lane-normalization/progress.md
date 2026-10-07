# Progress Ledger — AGATA LAB Permanent Lane Normalization

- [x] Read owner request and current Linear Canonical Architecture, Workers, and Handoff.
- [x] Confirmed the previous single Promptfoo compatibility invocation was consumed and failed before durable ledger reservation; no retry was made.
- [x] Created isolated checkout from exact base `7815362bee308fa1849671f2e66d242c28009151`; main checkout untouched.
- [x] Wrote implementation plan and recorded focused baseline (73 tests passed before behavior changes).
- [x] Added failing TDD contracts for permanent lane, no-approval behavior, scheduler visibility/priority, and ordinary-run lifecycle isolation.
- [x] Implemented permanent LAB presence with an independent fail-closed approval gate; removed legacy `AGATA_LAB_WORKER_ENABLED` lane-presence gating.
- [x] Routed one-shot and watch operations through the existing single Worker Pool; kept N=1, production lane contracts, and production-first polling.
- [x] Centralized bundle identity and private approval/usage validation in stdlib-only `lab_queue.py`; Promptfoo checks approval before durable reservation and Worker revalidates/atomically consumes before inference.
- [x] Preserved strict lifecycle queue/idle/lock/rollback gates and updated lifecycle readback to accept new permanent closed-gate state plus safe legacy states.
- [x] Added offline regressions for plist/PID/runtime byte identity, symlinked approval usage paths, approval gating, production priority/visibility, and lane isolation.
- [x] Updated local Conversation Lab README and Linear AGATA Workers operational contract.
- [x] Full Worker tests: 219 passed, 33 skipped. Guarded Conversation Lab offline suite: 159 passed, 22 skipped. Python compile and `git diff --check`: PASS.
- [x] Independent code review: the initial review found a symlinked usage-directory issue; it was fixed and re-reviewed with no remaining Critical or Important code findings.
- [ ] Linear Handoff CURRENT STATE update/readback: Linear transport failed during save and retry read. Do not claim synced until readback succeeds.
- [x] Local isolated commit created; branch clean; no push/merge/install/restart/live call.

No installed Worker, LaunchAgent, production config, VPS, GPOINT/GSALES runtime, or live inference changed.
