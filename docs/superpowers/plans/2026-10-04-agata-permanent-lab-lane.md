# AGATA Permanent LAB Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LAB a persistent target of the existing Unified Worker V3 LaunchAgent without coupling lane availability to an individual inference approval.

**Architecture:** Add an explicit persistent enable/disable operation to the PR #16 LaunchAgent controller wrapper. The operation changes only `AGATA_LAB_WORKER_ENABLED` in the existing plist, serializes through the shared transition controller, snapshots the prior plist for guarded rollback, and verifies the installed worker bundle SHA supplied by the owner. Existing V3 approval validation, expiry, max-call accounting, queue isolation, and target weights remain unchanged.

**Tech Stack:** Python 3.12, plistlib, existing `LaunchAgentController`, unittest.

**Spec:** Owner request in the current task; base controller commit `bf4dbb5fc3410e341b99d29ef52aa825b0c77fd3`.

## Global Constraints

- Do not edit GSales checkout, release pipeline, Docker images, VPS, PR #15/#16, or Worker bundle source.
- Preserve the shared serialized lifecycle controller, bounded polling, PID wait, error-37 diagnostics, receipts, and rollback behavior.
- Enabling the lane must not create or require an Approval B file.
- Do not relax LAB worker approval scope, expiry, source pinning, or max_calls.
- Do not restart the installed LaunchAgent while a critical GSales release/Worker integration phase is active.

## Review Focus

- Missing approval file: enabling LAB must still succeed; inference remains blocked by the existing Worker gate.
- Worker bundle drift: enable must fail before changing the plist or leaving a snapshot.
- Existing plist drift after enable: disable must fail closed instead of overwriting another future Worker/GSales update.
- Lifecycle transition failure: the controller must restore original plist bytes and the wrapper must clean only its newly-created snapshot.
- Persistent behavior: the on-disk LaunchAgent retains `AGATA_LAB_WORKER_ENABLED=true` across process restarts.

---

### Task 1: Persistent LAB LaunchAgent control

**Files:**
- Modify: `scripts/agata/lab_launch_agent.py`
- Test: `services/agata/tests/test_lab_permanent_lane.py`

**Interfaces:**
- Consumes: `LaunchAgentController.replace_plist_from_factory(...)` from the PR #16 base.
- Produces: `enable_lab(...)` and `disable_lab(...)`; CLI actions `enable --apply --expected-worker-bundle-sha256 <sha256>` and `disable --apply`.

- [x] Write a failing unittest proving enable works with no Approval file, verifies the exact worker bundle SHA, and leaves the LAB flag persisted with a safe original-plist snapshot.
- [x] Run the test and confirm it fails because `enable_lab` is not implemented.
- [x] Add minimal persistent enable/disable functions and CLI actions using the existing controller.
- [x] Add tests for bundle mismatch/no mutation, rollback on failed transition, disable restoration, and fail-closed protection if the plist changed after enable.
- [x] Run the new test module and existing offline Worker LAB integration suite under the bundled Python 3.12 runtime (16 tests passed).
- [x] Run the full offline Conversation Lab unittest suite (100 tests passed) and Naturalness V1 `dry-run` (16 development scenarios, 0 holdout, 0 live requests, no file writes).
- [x] Review the diff and confirm only the controller wrapper, local Naturalness job adapter, focused tests, and this plan changed; the Worker V3 bundle source is unchanged.
- [x] Commit the isolated branch; do not merge or trigger a production release.

**Activation gate:** Not run. Fresh status showed the running Worker V3 reports `worker_target_unavailable` / `worker_gateway_network_error`, and GitHub Actions API access was unavailable. GSales' active Codex task was running the production Deploy/Worker transition. Per owner instruction, do not restart the shared LaunchAgent or consume Approval B without a fresh proven-safe window.
