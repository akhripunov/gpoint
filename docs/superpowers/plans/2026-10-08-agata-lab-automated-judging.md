# AGATA Lab Automated Judging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement an offline-verifiable Promptfoo → AGATA LAB judge jobs → strict receipts → deterministic aggregation and calibration packet path.

**Architecture:** Extend only the existing LAB queue with a source-verified judge job type and reuse Slot 03/Codex Local, approval and execution machinery. Add an AGATA coordinator that reserves each judge call before submission, validates structured receipts, and invokes the existing pure aggregator; test it through the pinned Promptfoo CLI using a fake Worker execution transport.

**Tech Stack:** Python 3, existing AGATA Worker LAB spool, pinned Promptfoo 0.123.1 local tooling, Node CLI tests.

**Spec:** OWNER APPROVAL — AGATA Conversation Lab Automated Judging; canonical rules in Linear AGATA Conversation Lab — Canonical Architecture §§4, 4.1, 8–11, 15.

## Global Constraints

- No generator/judge inference, Promptfoo Optimize, API keys, paid API, Worker install/restart, production, network/VPN, or GitHub publication.
- Preserve exact `gpt-6-luna / medium`, subscription auth, retries=0, no fallback, and existing OWNER approval authority.
- Judges see only rubric, minimal Persona brief, anonymous X/Y conversations, and their own seeded order; no mapping, policy, candidate provenance, or other judge data.
- AUTO_WIN requires 3/3 agreement, >=6/8 core dimensions, no critical defects, uncertainty, or position sensitivity; all other cases escalate.
- Existing generator arm/call ceilings remain 8/32; judge ceiling remains 15 per existing ledger; no hidden retry or extra consensus calls.
- Preserve private blind pair and sealed holdout; do not read or expose saved blind outputs/mapping.
- Leave the work in the current isolated worktree as LOCAL_PENDING_RELEASE; no push, PR, merge, Actions, or runtime installation.

## Review Focus

- Malformed or extra judge JSON must produce HUMAN_ESCALATION and consume its reserved call exactly once; test in strict parser/coordinator task.
- Prompt or structured receipt leakage of private mapping, policy, or peer judge output must fail closed; test in judge compiler and Worker contract tasks.
- Expired/missing approval must fail before execution; test in LAB queue/Worker integration task.
- Duplicate invocation/job and ambiguous terminal results must not retry or double-count; test in coordinator task.
- Promptfoo CLI must run the actual provider path without a live model and preserve 3 independent contexts; test in mock E2E task.

---

### Task 1: Strict isolated judge protocol

**Files:**
- Modify: `services/agata/conversation-lab/evaluators/judges.py`
- Modify: `services/agata/conversation-lab/configs/owner-preference-rubric-v1.json`
- Test: `services/agata/conversation-lab/tests/test_judges.py`

**Interfaces:**
- Produces `parse_judge_output(text, *, judge_slot, scenario_id, presented_labels) -> receipt` and a strict JSON schema shared with Worker jobs.

- [x] Add RED tests for all required fields, eight dimension keys, defects, uncertainty, and denial of private metadata/extra keys.
- [x] Run focused tests and confirm failures.
- [x] Implement the parser/schema and ensure prompt inputs include only the minimum Persona brief, rubric, and one anonymous pair.
- [x] Run focused tests and confirm PASS.

### Task 2: LAB-only Worker judge job and execution

**Files:**
- Modify: `services/agata/worker/lab_queue.py`
- Modify: `services/agata/worker/gpoint_mac_worker.py`
- Modify: Worker LAB asset inventory/tests as required by exact bundle contract.
- Test: `services/agata/conversation-lab/tests/test_lab.py`, `services/agata/worker/tests/test_lab_queue.py`, `services/agata/worker/tests/test_worker_integration.py` (existing paths; add focused coverage there).

**Interfaces:**
- Adds an `agata-conversation-lab-judge-v1` job validated against a strict schema and canonical origin, assigned only to `LANE_SLOTS["LAB"]`.
- Worker executes judge instructions through its existing Codex Local process path and stores structured verdict data in the existing terminal LAB receipt.

- [x] Add RED tests for valid judge jobs, wrong source/lane/model/schema, mapping/policy/peer-judge leakage, expired/missing approval, and GPOINT/GSALES contract non-regression.
- [x] Run focused tests and confirm failures.
- [x] Extend only the LAB job schema/dispatch; retain existing generation behavior and use existing execution/approval/receipt handling.
- [x] Run focused Worker tests and confirm PASS.

### Task 3: Judge coordinator and automatic aggregation

**Files:**
- Create: `services/agata/conversation-lab/evaluators/judge_coordinator.py`
- Modify: `services/agata/conversation-lab/evaluators/run_coordinator.py` only if an existing API is insufficient.
- Test: `services/agata/conversation-lab/tests/test_judge_coordinator.py`

**Interfaces:**
- `run_judge_batch(packet, rubric, *, batch_id, dataset_split, ledger, private_mapping, sanity_findings, receipt_directory, private_directory, env, promptfoo_executable) -> batch_result`.
- Reserves each unique judge invocation before submission; validates correlation IDs and terminal receipt; no retry; calls `aggregator.aggregate` only after exactly three valid independent receipts.

- [x] Add RED tests for reservation ordering, separate judge budget, one-time success/error accounting, duplicate IDs, ambiguous no-retry, missing verdict escalation, and deterministic AUTO/HUMAN outcome.
- [x] Run focused tests and confirm failures.
- [x] Implement orchestration, private receipt persistence, and aggregation using existing components.
- [x] Run focused tests and confirm PASS.

### Task 4: Promptfoo provider path and offline CLI E2E

**Files:**
- Create: `services/agata/conversation-lab/evaluators/promptfoo_judge_provider.py`
- Create/modify: `services/agata/conversation-lab/tooling/promptfoo` mock judge config and script.
- Test: `services/agata/conversation-lab/tests/test_promptfoo_judge_mock_e2e.py`

**Interfaces:**
- Promptfoo judge provider invokes the AGATA coordinator; offline harness substitutes only Worker execution with deterministic structured mock receipts and cannot select a live provider.

- [x] Add a test invoking the pinned Promptfoo CLI with mock-only config; assert three distinct judge job IDs/contexts, three structured terminal receipts, one aggregator decision, no live-provider/API auth path, and zero inference attempts.
- [x] Run the E2E test and confirm it fails before implementation.
- [x] Implement the minimal provider/config glue and mock-only CLI harness.
- [x] Run E2E and confirm PASS.

### Task 5: Batched OWNER calibration packet and final verification

**Files:**
- Create: `services/agata/conversation-lab/evaluators/calibration.py`
- Create: `services/agata/conversation-lab/evaluators/v2_provenance.py`
- Test: `services/agata/conversation-lab/tests/test_calibration.py`
- Update: existing Linear `AGATA Conversation Lab — Handoff`.

**Interfaces:**
- `build_calibration_packet(locked_judge_batches, *, minimum_pairs=10)` returns a consolidated blind OWNER packet with no mapping until rating lock; compare OWNER ratings only after lock.

- [x] Add RED tests for minimum 10 pairs, mock judge pre-rating, private mapping isolation, and no unblind before OWNER rating lock.
- [x] Implement packet compilation, manifest-backed V2 provenance validation, and agreement evidence reuse without new LLM calls.
- [x] Run focused tests, guarded full offline suite, real Promptfoo mock E2E, worker regressions, and `git diff --check`.
- [x] Perform independent review, fix Important provenance findings, and rerun relevant checks.
- [x] Update and read back Handoff with IMPLEMENTED / INSTALLED / LIVE VERIFIED explicitly separated; preserve private pair and note zero live calls.
- [x] Leave local status `LOCAL_PENDING_RELEASE`; do not publish or install.
