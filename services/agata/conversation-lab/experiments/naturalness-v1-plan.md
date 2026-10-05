# AGATA Naturalness V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver an offline-only, reproducible A/B preparation package that adds a shared Naturalness V1 policy to the audited AGATA Codex request while preserving Persona, memory, safety, and the sealed Phase 3 holdout.

**Architecture:** Keep AGATA Core and the existing Mac worker untouched. Add a Conversation Lab policy/config/example layer and a small offline evaluator that derives both A/B requests from the frozen Phase 3 request builder, then reuses the existing blind-packet and ratings-lock functions. No network, subprocess, worker enqueue, production integration, inference, or model training is introduced.

**Tech Stack:** Python 3 standard library, JSON/JSONL fixtures, `unittest`, existing Conversation Lab `pilot.py`, `blind.py`, `ratings.py`, GitHub/Linear integrations only for final documentation and Draft PR.

**Spec:** `services/agata/conversation-lab/experiments/naturalness-v1.md`

## Global Constraints

- Work only in the isolated `codex/agata-naturalness-v1` branch based on Phase 3 SHA `d228b862d5d345eb1d55a6fb1164f7d5b4d77902`; do not modify the G Sales checkout.
- Keep `benchmark-v1.json` and `agata-benchmark-v1.jsonl` byte-identical; Phase 3 `pilot-v1.json` and all 16 `-01` scenario inputs are a sealed holdout.
- Use the existing Codex Local route only as a later, separately approved experiment; all work in this plan is fixture-only with zero inference.
- Do not edit AGATA Core, Mac worker/LaunchAgent, production, database/schema/migrations, G Sales, or release workflows; do not add a PostgreSQL table.
- Do not manually open, export, or copy corpus conversations into artifacts. Use aggregate Phase 2 findings and independently authored synthetic examples; if the existing bounded-memory leakage checker can access canonical data, it may scan read-only without emitting source passages. Preserve all source data.
- Do not introduce API calls, provider credentials, paid APIs, network transport, model training, or worker/job execution.
- Keep at most three Naturalness policy variants total, including V1; no tuning against the 16-scenario Phase 3 holdout.
- Update only the existing Linear AGATA Conversation Lab document; prepare a stacked Draft PR on PR #13's branch and do not merge/deploy.

## Review Focus

- **Holdout contamination:** requests or examples accidentally include one of the frozen Phase 3 `-01` scenarios, evaluation labels, expected answers, or future turns. Test exact ID/hash disjointness and inspect every serialized request.
- **Baseline drift:** the A request differs from the audited Phase 3 baseline, or B silently changes model, Persona, memory, context, or safety. Test canonical instruction equality and equality of all non-policy request fields.
- **Fake evidence:** fixture responses are mistaken for model output or a quality result. Assert fixture-only flags, zero inference count, no winner, and no completion claim as model evidence.
- **Worker escape path:** offline code gains process/network/auth/job capability. Test against sockets, subprocess launch, auth lookup and any sender/enqueue interface; statically review for forbidden imports/calls.
- **Persona homogenization / forced rhythm:** shared policy imposes canned empathy, brevity, humor, slang, positivity, or a closing question. Review policy examples for at least three distinct response contexts and no mandatory style token.
- **Corpus leakage:** authored examples repeat source or benchmark material. Test against existing Benchmark fixtures and, only if canonical data is available to the existing bounded-memory checker, run its read-only scan without exporting source excerpts; otherwise report the check as unavailable.

---

## File map

- Create `services/agata/conversation-lab/configs/naturalness-v1.json`: versioned A/B manifest, development IDs, frozen holdout reference, source hashes and recorded blind seed.
- Create `services/agata/conversation-lab/configs/naturalness-policy-v1.md`: candidate-only shared policy text and its scope/version/hash contract.
- Create `services/agata/conversation-lab/data/golden/synthetic/naturalness-examples-v1.jsonl`: small authored examples with evaluator-only anti-pattern metadata separated from candidate-visible turns.
- Create `services/agata/conversation-lab/evaluators/naturalness.py`: load/freeze validation, A/B request construction, fixture-only run, result validation and blind packet wrapper; no sender.
- Modify `services/agata/conversation-lab/evaluators/pilot.py` only to extract a pure scenario-to-request serializer used by both the still-holdout-locked Phase 3 wrapper and the Naturalness `-02` adapter; preserve the current Phase 3 wrapper's strict frozen-ID gate and exact request output.
- Create `services/agata/conversation-lab/tests/test_naturalness.py`: focused RED/GREEN regressions for manifest, policy, request parity, leakage, blind packet, safety and offline-only behavior.
- Modify `services/agata/conversation-lab/experiments/naturalness-v1.md` only if research traceability or implementation evidence needs an additive correction; do not broaden its approved design.
- Create `services/agata/conversation-lab/experiments/naturalness-v1-plan.md`: this execution plan and task checkboxes.
- Modify `services/agata/conversation-lab/README.md` only to add the exact offline command, fixture-only status, output contract and explicit inference gate.
- No other source/runtime/config files are in scope.

## Tasks

### Task 1: Freeze development scenarios and provenance

**Files:**
- Create: `services/agata/conversation-lab/configs/naturalness-v1.json`
- Test: `services/agata/conversation-lab/tests/test_naturalness.py`
- Read-only: `configs/benchmark-v1.json`, `configs/pilot-v1.json`, `data/golden/synthetic/agata-benchmark-v1.jsonl`

**Interfaces:**
- Consumes the existing benchmark scenario schema and `evaluators/pilot.py::load_frozen_pilot` / `scenario_by_id`.
- Produces `load_naturalness_config(lab) -> dict` and `validate_naturalness_config(lab, config) -> dict`; freezes the development set and provenance without modifying Phase 3 inputs.

- [ ] **Step 1: Write failing tests** for exact 16 development IDs, 16 holdout IDs, disjointness, canonical benchmark/pilot hashes, scenario hashes, missing IDs, duplicate IDs, tampering, and future/evaluator fields excluded from candidate views. Use `-02` (not `-01`) from each existing category as the initial development review set; assert `long_dialogue-02` retains all 12 rounds.
- [ ] **Step 2: Run the focused tests** with `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_naturalness.py'`; confirm the new config loader/validator is missing and the intended assertions fail.
- [ ] **Step 3: Implement the smallest config loader/freezer** in `evaluators/naturalness.py`; store stable source and fixture hashes, development IDs, the Phase 3 holdout IDs, and a fixed blind seed in `naturalness-v1.json`. Do not copy scenarios into the config.
- [ ] **Step 4: Re-run focused tests** and verify all benchmark and pilot source bytes remain unchanged.
- [ ] **Step 5: Commit** as `test(agata): freeze Naturalness V1 development inputs`.

### Task 2: Author shared policy and synthetic examples

**Files:**
- Create: `services/agata/conversation-lab/configs/naturalness-policy-v1.md`
- Create: `services/agata/conversation-lab/data/golden/synthetic/naturalness-examples-v1.jsonl`
- Test: `services/agata/conversation-lab/tests/test_naturalness.py`

**Interfaces:**
- Consumes the validated development manifest and aggregate Phase 2 notes; no raw/processed corpus paths.
- Produces `load_policy(lab, config) -> str` and `load_examples(lab, config) -> list[dict]`; evaluator-only labels are structurally separated from `candidate_turns`.

- [ ] **Step 1: Add failing tests** for schema, unique IDs, authored synthetic provenance, exact duplicate rejection against Benchmark fixtures, evaluator-label separation, supported facts only, distinct situations, and policy hash mismatch. Cover all fifteen owner-listed patterns without enforcing a fixed response length, empathy phrase, question, joke, slang or positivity.
- [ ] **Step 2: Run focused tests** and confirm policy/example loaders and validators are absent.
- [ ] **Step 3: Implement the versioned policy and a compact set of independent examples** (at least one example for each major pattern family, no corpus quotations). Map each of the 15 dimensions to an aggregate Phase 2 note by title/section or explicitly label it `OWNER_HYPOTHESIS`; add no unsupported empirical rate or quality claim. If canonical source data are available, run only the existing bounded-memory leakage scan read-only; otherwise record that limitation.
- [ ] **Step 4: Run focused tests** and manually inspect candidate-visible serialization to confirm evaluator labels and rationales are excluded.
- [ ] **Step 5: Commit** as `feat(agata-lab): add Naturalness V1 policy fixtures`.

### Task 3: Build isolated baseline and candidate payloads

**Files:**
- Modify: `services/agata/conversation-lab/evaluators/naturalness.py`
- Modify: `services/agata/conversation-lab/evaluators/pilot.py` (pure request serializer extraction only; preserve public frozen-pilot ID validation)
- Test: `services/agata/conversation-lab/tests/test_naturalness.py`
- Read-only: `services/agata/conversation-lab/evaluators/adapters.py`

**Interfaces:**
- Consumes: validated config, policy and the existing Phase 3 request builder.
- Produces `pilot.build_fixture_invocation(lab, scenario, expected_scenario_sha256, model_id, round_index, history) -> dict` (hash-checked pure serializer) and `build_ab_invocations(lab, config, scenario_id, round_index, history_by_candidate) -> tuple[dict, dict]`, where the public Phase 3 wrapper remains frozen-ID-only, A and B share identical non-instruction inputs, and `run_fixture_dry_run(lab, config) -> dict` always reports `live_inference_requests: 0`.

- [x] **Step 1: Write failing tests** that assert the existing Phase 3 wrapper remains byte/hash-identical to a pre-refactor request snapshot and still rejects `-02`; the extracted serializer reproduces that request when given the pinned scenario SHA; A differs only from B in candidate-only policy/examples; all non-instruction inputs match; distinct prior assistant histories remain candidate-specific; future user turns and evaluator fields are absent.
- [x] **Step 2: Run focused tests** and verify failure on the missing builder.
- [x] **Step 3: Extract the pure scenario request serializer in `pilot.py` without changing request construction, then keep `pilot.build_invocation` validating the frozen Phase 3 ID set before delegating to it. Build Naturalness `-02` requests through the serializer only after the Naturalness manifest validates exact IDs and SHA; add the versioned policy and positive examples only to B. Do not expose any method that sends, launches, enqueues or authenticates.**
- [x] **Step 4: Add and run fixture-only dry-run tests** for every development request, explicit synthetic markers, stale hashes, wrong candidate IDs, prompt injection from context/memory, and zero socket/subprocess/worker access.
- [x] **Step 5: Commit** as `feat(agata-lab): construct offline Naturalness A/B payloads`.

### Task 4: Reuse blind evaluation for fixture-only pairs

**Files:**
- Modify: `services/agata/conversation-lab/evaluators/naturalness.py`
- Test: `services/agata/conversation-lab/tests/test_naturalness.py`
- Read-only/reuse: `services/agata/conversation-lab/evaluators/blind.py`, `services/agata/conversation-lab/evaluators/ratings.py`

**Interfaces:**
- Consumes complete fixture-only A/B transcripts validated against the exact development conversation turns.
- Produces `package_blind_fixture(lab, config, rows, seed) -> tuple[dict, dict]`; public packet labels are anonymous and key metadata remains private.

- [x] **Step 1: Write failing tests** for complete A/B coverage, stable randomized order, model/config IDs absent from public packets, private mapping hash, no packet duplicate, no successful-model claim for fixtures, compatibility with existing `ratings.empty_ratings` and `ratings.lock_ratings`, and rejection of missing/extra turns or mismatched user turns.
- [x] **Step 2: Run focused tests** and verify the wrapper is absent.
- [x] **Step 3: Implement the wrapper with `blind.make_blind_packets` and existing ratings locks**, marking all fixture results and packets `FIXTURE_ONLY`, `quality_status=NO_MODEL_OUTPUTS_OWNER_REVIEW_NOT_APPLICABLE`, and `winner=null`. Do not fork the blind or ratings subsystem.
- [x] **Step 4: Run focused tests** and confirm no model identities or evaluator labels leak into public output.
- [x] **Step 5: Commit** as `feat(agata-lab): prepare blind Naturalness fixture packets`.

### Task 5: Document the offline workflow and integration proposal

**Files:**
- Modify: `services/agata/conversation-lab/README.md`
- Modify if needed: `services/agata/conversation-lab/experiments/naturalness-v1.md`
- Test: `services/agata/conversation-lab/tests/test_naturalness.py`

**Interfaces:**
- Documents exact local fixture-only command/output and the later OWNER gate; has no runtime import into AGATA Core.

- [x] **Step 1: Add failing documentation-contract tests** for documented command existence, `FIXTURE_ONLY` output, request count 0 for live inference, no worker/production wording that implies execution, and no modifications to Core/worker/G Sales.
- [x] **Step 2: Implement only the documentation and a static changed-path/safety assertion**; integration proposal must separate SHARED policy, PER MODEL Persona, RELATIONSHIP context and MEMORY.
- [x] **Step 3: Run focused tests** and review README/spec consistency.
- [x] **Step 4: Commit** as `docs(agata-lab): document Naturalness offline workflow`.

### Task 6: Full verification, independent review, Linear and stacked Draft PR

**Files:**
- Final review of all Phase 4A changes; update the existing Linear AGATA Conversation Lab document only.
- Create stacked Draft PR from `codex/agata-naturalness-v1` onto PR #13's `codex/agata-conversation-lab-phase3` branch.

**Interfaces:**
- Consumes all previous task commits and the approved specification.
- Produces a clean branch, verified test evidence, Linear readback, stacked Draft PR and exact HEAD/parent SHA report.

- [x] **Step 1: Run the full lab suite** with `python3 -m unittest discover -s services/agata/conversation-lab/tests`; expect all existing 77 Phase 3 checks and all new checks to pass. Run `git diff --check` and compare Benchmark V1 / Phase 3 pilot hashes to the frozen values.
- [x] **Step 2: Run the offline Naturalness dry-run** and inspect its manifest and blind fixture packet; require 0 live requests, explicit fixture-only status, candidate parity and holdout isolation.
- [x] **Step 3: Perform independent whole-branch review** against the approved spec; fix every important finding with a failing regression first, then re-run targeted and full suites.
- [x] **Step 4: Update the existing Linear document** with research pattern traceability, exact source/hash results, implementation, verification, remaining inference/quota blocker and roadmap. Read the document back and confirm no new document was created.
- [x] **Step 5: Verify PR #13 is still the intended unmerged base, create a stacked Draft PR** without merge, verify exact base/head SHA and review state, then attach the PR to this task.
- [x] **Step 6: Run `pnpm repo:clean:end` and verify the main G Sales checkout remains unchanged.** `pnpm repo:clean:start` previously triggered an automatic workspace dependency install and failed with `EPERM` in the managed worktree; diagnose the environment and obtain a passing start/end gate without altering lockfiles or dependency policy.
- [x] **Step 7: Report** exact branch/PR/SHA, tests, independent review, Linear readback, fixture-only evidence, and a separate request for OWNER approval before any controlled Codex Local A/B inference.

## Future controlled inference gate (not part of plan execution)

If the owner later approves real generation, propose an initial four-scenario development smoke (daily, humor, support, boundaries; non-holdout `-02` scenarios) with A/B and a fixed small call cap, inspect original outputs without editing, then stop for owner review before consuming the sealed 16-scenario `-01` holdout. Reconfirm shared-worker lock, GPoint/G Sales collision safety, quota and exact hashes first. This section does not authorize that generation; it only reserves the next approval point.
