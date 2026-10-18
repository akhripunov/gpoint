# GPoint Prepare V4 Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce FULL Prepare wall time by running independent exact-SHA Docker image builds with bounded concurrency while retaining all Prepare gates.

**Architecture:** Keep each existing Docker build command, platform, tag, labels, and isolated BuildKit cache scope unchanged. A small bounded scheduler starts at most two independent image builds and starts the next build when a slot frees; any nonzero exit cancels remaining builds and fails Prepare. Reuse requires no component artifacts, and no test or migration gate is skipped.

**Tech Stack:** Python 3.12, subprocess, pytest, Docker Buildx, GitHub Actions.

**Spec:** GPoint Prepare V4 Performance Optimization / Phase 2, attached owner task, dated 2026-10-15.

## Global Constraints

- No GitHub push, PR, merge, Actions dispatch, or extra hosted benchmark without separate OWNER approval.
- No production Deploy, DB mutation, Worker change, MAXIBOT change, or release gate reduction.
- Prepare must retain exact candidate SHA, linux/amd64 image builds, immutable artifact, provenance, and current safety checks.
- Maximum concurrent Docker image builds is 2 pending local runner capacity and an authorized remote rehearsal.

## Review Focus

- First failed image build must cancel any other running build and prevent READY publication.
- Every image build must keep its exact tag, revision label, platform, and unique cache scope.
- Scheduler must make progress when a short build finishes while the first build remains active.
- Failure and timeout cleanup must reap every launched process.
- Concurrent Docker loads/cache exports must not collide across the three distinct images/scopes.

### Task 1: Bounded image build scheduler

**Files:**
- Modify: `scripts/prepare-release.py`
- Test: `tests/test_prepare_release_migration_inventory.py`
- Modify: `docs/release/RELEASE_PERFORMANCE.md` with measured-vs-pending findings

- [x] Add tests for overlapping starts, max concurrency, fail-closed cancellation, and bounded timeout/reaping.
- [x] Implement a bounded subprocess scheduler using the existing background-stage logging/timing helpers.
- [x] Schedule GPoint runtime, AGATA runtime, and GSales runtime in the same stable command definitions, with a limit of 2.
- [x] Run targeted release tests and diff checks.
- [x] Record that Docker/GitHub performance validation is pending because no extra hosted run is authorized.

The authorized local Docker benchmark was not run because another disposable
GPoint database test container is active and Docker BuildKit cache is shared.
Full candidate Docker validation and hosted cost measurement remain pending a
separately authorized rehearsal/canonical Prepare.

No component reuse or test selection changes are part of this implementation.
