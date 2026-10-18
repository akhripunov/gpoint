# ME-42 GitHub Access Preflight Implementation Plan

> **Execution:** Inline in the existing isolated ME-42 worktree; no new task, publish, workflow run, or production operation.

**Goal:** Make the current PROSTOR GitHub access standard mandatory in GPoint Codex instructions and prevent sandbox-local DNS failures from being reported as global GitHub/GHCR outages.

**Architecture:** Carry one canonical Linear link and a compact context-aware rule through root instructions, DEVOPS profile, role bootstrap, and release automation. Add a repository-local instruction contract test for the DEVOPS loading path and DNS scope rule. Do not add old Releases, Ledger, or production-tag procedures from ME-39.

**Tech Stack:** Markdown instructions; Python `unittest`.

**Spec:** OWNER request in current ME-42 continuation; canonical Linear document “GitHub доступ”; existing ME-39 local handoff at `254b955722ffbcf02222fff68b76a27d7bab4253`.

## Global Constraints

- No push, PR, merge, Actions dispatch, GHCR write, production/VPS/DB mutation, or MAXIBOT changes.
- Preserve GHCR-only release architecture and existing safety/approval gates.
- Do not expose tokens. Use only standard approved authentication and the exact operation scope.
- Do not treat a DNS failure in one execution context as proof of global service failure.

## Review Focus

- DEVOPS bootstrap must receive and require the canonical standard before GitHub/GHCR operations.
- Diagnostics must distinguish NETWORK, TOOLING, AUTH, PERMISSION, and OWNER_AUTHORIZATION.
- DNS failure must remain scoped to the checked context; host checks do not prove sandbox or Actions access.
- Do not bring back Releases, Ledger, or production-tag rules from the older ME-39 commit.

### Task 1: Add instruction contract test

**Files:** Create `tests/test_github_access_preflight.py`.

- [ ] Assert effective root → role bootstrap → DEVOPS profile requires the canonical Linear URL and exact access preflight.
- [ ] Assert the contract includes the five failure classes and prohibits global BLOCKED solely from sandbox DNS failure.
- [ ] Run the new test and confirm it fails against the current candidate.

### Task 2: Integrate the standard

**Files:** Modify `AGENTS.md`, `.codex/roles/devops.md`, `docs/codex/role-bootstrap.md`, `docs/release/automation.md`.

- [ ] Add concise, context-aware requirements and available safe auth methods; preserve project authorization boundaries.
- [ ] Run the contract test and syntax/format checks.

### Task 3: Verify and hand off

- [ ] Review exact diff; run applicable tests and the required repo clean gate.
- [ ] Create a scoped local commit, run Handoff Gate on exact SHA, update existing ME-42 and ME-39, and leave checkout clean.
- [ ] Record ORAS/network/auth findings without attempting a GHCR write or Actions run.
