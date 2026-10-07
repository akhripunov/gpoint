# AGATA Conversation Lab V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Promptfoo the isolated execution/evaluation backbone for AGATA Conversation Lab V2 while AGATA code retains domain authority, privacy, blinding, calibration, and decisions.

**Architecture:** Add a Lab-only Promptfoo toolchain and invoke it through an AGATA-owned run coordinator. AGATA compiles parity-checked arm requests, keeps independent histories and private receipts, validates blind packets, isolates three judges, and deterministically aggregates. The optimizer remains offline-only until its calibration lifecycle and a separate bounded OWNER authorization permit a run.

**Tech Stack:** Python 3 standard library for AGATA orchestration, validation, receipts, and tests; Lab-only Node 22 tooling with Promptfoo 0.123.1 and `@openai/codex-sdk` 0.156.1; existing unittest suite.

**Spec:** `docs/superpowers/specs/2026-10-06-agata-lab-promptfoo-v2-design.md`

## Global Constraints

- Promptfoo is execution/evaluation engine; AGATA Lab is domain-specific authority.
- Keep dependencies and lockfiles under `services/agata/conversation-lab/tooling/promptfoo/`; do not change root or production package manifests.
- Model is exactly `gpt-6-luna / medium`; only existing Codex/ChatGPT subscription auth is permitted; reject API-key auth paths; no fallback; `maxRetries=0`; no retries, resume, or cache reuse.
- Bound this batch to 8 conversation arms, 32 underlying generator provider invocations, and 15 judge provider invocations, tracked separately.
- Baseline and Candidate have identical initial fixtures and separate histories; only Naturalness treatment differs.
- Keep mapping and private metadata out of Promptfoo judge inputs and public blind packets; lock OWNER ratings and control selection before unblind.
- Sanity validation records defects without rewriting model output.
- Exclude compromised Holdout V1; enforce DEV / VALIDATION / SEALED HOLDOUT V2 boundaries.
- `OPTIMIZER_READY` requires the full calibration gate; `OPTIMIZER_RUN_AUTHORIZED` requires separate bounded OWNER approval.
- No production AGATA, prompt, database, Worker, GPOINT/GSALES, VPS, deployment, paid API, or live optimization changes/calls.
- If offline evidence cannot establish Codex SDK compatibility, stop before the live call and request the one approved compatibility decision.

## Review Focus

- Provider environment contamination: tests must show API-key variables/config fail closed and are never forwarded.
- Arm-history crossover: tests must show each request carries only its own prior assistant turns.
- Blind-data leakage: tests must show no mapping, treatment, candidate SHA, generator reasoning, or judge outputs reach judges/public packets.
- Holdout access: tests must show current paths cannot load compromised V1 `-01` data and optimizer allowlists reject SEALED HOLDOUT V2.
- Partial/ambiguous invocation accounting: tests must charge the correct ledger stage once invocation begins and never retry.

---

### Task 1: Lab-only Promptfoo toolchain and fail-closed invocation contract

**Files:**
- Create: `services/agata/conversation-lab/tooling/promptfoo/package.json`, `package-lock.json`, `promptfooconfig.yaml`
- Create: `services/agata/conversation-lab/evaluators/promptfoo_runner.py`
- Test: `services/agata/conversation-lab/tests/test_promptfoo_runner.py`

**Interfaces:**
- Produces `validate_live_environment(env: Mapping[str, str], provider_config: Mapping[str, object]) -> None`, `build_cli_environment(env: Mapping[str, str]) -> dict[str, str]`, `require_subscription_login(status_output: str) -> None`, `build_provider_request(prompt: str, *, working_dir: str, model: str = "gpt-6-luna", reasoning: str = "medium") -> dict`, and `run_promptfoo_eval(...)` with subscription-auth preflight, isolated temporary config, and explicit child environment.
- Produces a Lab-only YAML config pinned to Promptfoo `0.123.1`, Codex SDK `0.156.1`, `maxRetries: 0`, cache off, concurrency 1, read-only sandbox, no network/search, and no fallback.

- [x] Write failing tests for API-key env/config rejection, `inherit_process_env=false`, minimal env allowlist, exact model/reasoning, no fallback, zero retries, and disabled tools/network/write access.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_promptfoo_runner.py' -v`; confirm expected failures.
- [x] Implement config validation and subprocess invocation that passes only the explicit minimal environment and refuses any API-key path.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_promptfoo_runner.py' -v`; confirm PASS.
- [x] Validate the pinned Node manifests/config offline; do not make model calls.
- [x] Commit `feat(agata-lab): add isolated Promptfoo invocation contract`.

### Task 2: Parity-checked conversation arms and hard call ledger

**Files:**
- Create: `services/agata/conversation-lab/evaluators/run_coordinator.py`
- Modify: `services/agata/conversation-lab/evaluators/naturalness.py`
- Test: `services/agata/conversation-lab/tests/test_run_coordinator.py`

**Interfaces:**
- Produces `compile_arms(scenario, fixtures, treatment, *, run_id) -> list[dict]`, exactly two arms named only in private state.
- Produces `CallLedger(max_arms=8, max_generator_invocations=32, max_judge_invocations=15)` with an append-only fsynced journal, cross-process file locking, crash recovery, arm/scenario/configuration linkage, sequential turn reservations, and `snapshot() -> dict`. Provider invocation is forbidden unless exact 8/32/15 durable limits are verified before subprocess execution.
- Consumes Task 1 provider invocation contract.

- [x] Write RED tests proving exact input parity except treatment, independent histories, 4 Baseline + 4 Candidate arm cap, stage-specific call ceilings, and invocation charged once with no retry after timeout/ambiguous result.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_run_coordinator.py' -v`; confirm failures are about missing coordinator behavior.
- [x] Implement serial four-turn coordination over Promptfoo one-turn requests; preserve exact model, reasoning, immutable scenario/user turns, and no cross-arm history.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_run_coordinator.py' -v`; confirm PASS.
- [x] Commit `feat(agata-lab): coordinate parity-checked blind arms`.

### Task 3: Persona sanity and fail-closed blind packet boundary

**Files:**
- Create: `services/agata/conversation-lab/evaluators/persona_sanity.py`
- Modify: `services/agata/conversation-lab/evaluators/blind.py`
- Test: `services/agata/conversation-lab/tests/test_persona_sanity.py`, `services/agata/conversation-lab/tests/test_blind.py`

**Interfaces:**
- Produces `validate_transcripts(scenario, arms) -> dict` containing `structural_status`, stable defect codes with scenario/turn references, and private quality findings; never returns rewritten text.
- Blind packet creation fails closed on structure, arm-label, or metadata leakage. Persona/gender findings are private and force `HUMAN_ESCALATION` without embedding defect metadata into the owner-facing packet.

- [x] Write RED tests for female AGATA self-reference violations, known male user gender errors, unknown-gender non-inference, unchanged text, role/order/count mismatches, label/metadata leakage, and private-only quality findings.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_persona_sanity.py' -v` and `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_blind.py' -v`; confirm failures are expected validator/packet behaviors.
- [x] Implement the deterministic sanity gate and connect it to blind packet creation without altering outputs.
- [x] Run both Task 3 unittest commands; confirm PASS.
- [x] Commit `feat(agata-lab): gate blind packets with persona sanity checks`.

### Task 4: Versioned OWNER rubric, isolated judges, and seeded presentation

**Files:**
- Create: `services/agata/conversation-lab/configs/owner-preference-rubric-v1.json`
- Create: `services/agata/conversation-lab/evaluators/judges.py`
- Modify: `services/agata/conversation-lab/evaluators/blind.py`
- Test: `services/agata/conversation-lab/tests/test_judges.py`

**Interfaces:**
- Produces `judge_inputs(packet, rubric, *, batch_id, judge_count=3) -> tuple[list[dict], dict]`; each judge gets a fresh, minimal anonymous X/Y pair and no private fields.
- Produces seeded reproducible judge order and OWNER control selection; seed/orders/selection are private receipt fields only.
- Rubric core dimensions are the exact eight in the approved spec; conditional dimensions never enter the 8/8 denominator.

- [x] Write RED tests proving all eight rubric IDs, no mapping/treatment/hash/reasoning/other judge outputs in prompts, separate contexts, X/Y reversal normalization metadata, and deterministic order/control selection from immutable batch identity.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_judges.py' -v`; confirm expected failures.
- [x] Implement judge input compiler, strict rubric loader, deterministic private randomization, and structured-output validation contract.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_judges.py' -v`; confirm PASS.
- [x] Commit `feat(agata-lab): isolate seeded blind judge inputs`.

### Task 5: Deterministic aggregator and optimizer readiness lifecycle

**Files:**
- Create: `services/agata/conversation-lab/evaluators/aggregator.py`
- Create: `services/agata/conversation-lab/evaluators/optimizer_gate.py`
- Test: `services/agata/conversation-lab/tests/test_aggregator.py`, `services/agata/conversation-lab/tests/test_optimizer_gate.py`

**Interfaces:**
- Produces `aggregate(judge_receipts, private_mapping, sanity_findings) -> dict` with only deterministic `AUTO_WIN_X`, `AUTO_WIN_Y`, or `HUMAN_ESCALATION` outcomes.
- Produces `optimizer_status(evidence) -> Literal["OPTIMIZER_ADAPTER_IMPLEMENTED", "OPTIMIZER_READY", "OPTIMIZER_RUN_AUTHORIZED"]`.
- `OPTIMIZER_READY` requires all approved technical checks, at least 10 OWNER-rated pairs, >=90% judge-vs-OWNER overall agreement, and no systematic critical-dimension failure (at least two distinct OWNER-rated misses/disagreements for a critical dimension).
- `OPTIMIZER_RUN_AUTHORIZED` additionally requires a separately validated bounded approval envelope; READY alone never starts Promptfoo Optimize.

- [x] Write RED tests for 3/3 overall, >=6/8 core-dimension threshold, conditional-dimension exclusion, 2/1, ties, uncertainty, order sensitivity, missing judge, critical defects, insufficient calibration, 90% boundary, systematic failure, and approval scope mismatch.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_aggregator.py' -v` and `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_optimizer_gate.py' -v`; confirm expected failures.
- [x] Implement deterministic aggregation and lifecycle gate with no LLM or side effects.
- [x] Run both Task 5 unittest commands; confirm PASS.
- [x] Commit `feat(agata-lab): add deterministic judge aggregation gates`.

### Task 6: V2 split isolation and validation-only optimizer adapter

**Files:**
- Create: `services/agata/conversation-lab/configs/splits-v2.json`
- Create: `services/agata/conversation-lab/evaluators/optimizer.py`
- Modify: `services/agata/conversation-lab/evaluators/naturalness.py`, `services/agata/conversation-lab/evaluators/pilot.py`
- Test: `services/agata/conversation-lab/tests/test_optimizer.py`

**Interfaces:**
- Produces `build_optimizer_config(split_manifest, *, policy_hash, candidate_limit) -> dict` from an explicit DEV/VALIDATION allowlist.
- Produces `authorize_optimizer_run(config, approval) -> dict`; refuses absent/out-of-scope approval, SEALED HOLDOUT access, excessive candidate/call budget, and mismatched starting policy hash.
- Does not read, hash, load, or add compromised V1 `-01` plaintext; no holdout corpus content is committed. Current V2 dry-run and execution paths must not call the legacy frozen-pilot loader, which selects `-01`; legacy fixtures stay untouched, while tests of current paths use only approved DEV fixtures.

- [x] Write RED tests for DEV/VALIDATION allowlist, SEALED HOLDOUT deny, no legacy `-01` loader access in current paths, compromised V1 exclusion, hash/candidate budget pinning, and refusal to execute without separate OWNER approval.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_optimizer.py' -v`; confirm failures.
- [x] Implement split schema/access checks and validation-only Promptfoo Optimize config builder; no optimizer command execution path is enabled by default.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_optimizer.py' -v`; confirm PASS.
- [x] Commit `feat(agata-lab): isolate V2 splits and optimizer config`.

### Task 7: Private receipts, OWNER lock/unblind, integrated offline proof

**Files:**
- Create: `services/agata/conversation-lab/evaluators/receipts.py`
- Modify: `services/agata/conversation-lab/evaluators/ratings.py`, `services/agata/conversation-lab/evaluators/naturalness_cli.py`, `services/agata/conversation-lab/README.md`
- Test: `services/agata/conversation-lab/tests/test_receipts.py`, `services/agata/conversation-lab/tests/test_naturalness.py`

**Interfaces:**
- Produces atomic restrictive-mode private receipt creation with immutable run identity, requested model/reasoning, counters derived from a verified durable ledger object, hashes, sanity results, private mapping/order/control, judge receipts, aggregate status, and receipt-scoped append-only OWNER lock/unblind events.
- OWNER rating lock must validate packet/rubric hashes before unblind; public packet excludes private metadata and judge verdicts.
- Offline dry-run is the default; live invocation is not called by tests or CLI unless a separate compatibility gate is explicitly satisfied.

- [x] Write RED tests for atomic/private receipts, deterministic run identity, lock-before-unblind, no private data in public packets, no public judge verdict, and integrated offline runner with zero provider calls.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_receipts.py' -v` and `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_naturalness.py' -v`; confirm failures are expected.
- [x] Implement receipt/lock integration and document offline commands plus the exact pre-live compatibility gate.
- [x] Run both Task 7 unittest commands again; confirm PASS.
- [x] Run `python3 -m unittest discover -s services/agata/conversation-lab/tests -p 'test_*.py'` and `python3 services/agata/conversation-lab/evaluators/naturalness_cli.py dry-run`; verify zero live calls, no compromised V1 `-01` access, no raw run artifacts staged, and no production paths modified. Legacy Phase 3 tests are class-skipped because their setup opens compromised V1 `-01` scenarios; final offline suite: 142 passed, 19 skipped.
- [x] Commit `feat(agata-lab): persist private V2 evaluation receipts`.

### Task 8: Independent review, Linear record, and gate handoff

**Files:**
- Review full worktree diff against this plan and approved spec.
- Update: existing Linear document `AGATA Conversation Lab` only.

**Interfaces:**
- Consumes all prior task outputs and tests.
- Produces review findings and canonical Linear evidence; does not push, merge, deploy, migrate, run live inference, or run optimization.

- [x] Run `git diff --check`, the complete test suite, and V2 CLI dry-run with fresh output; `node scripts/repo-clean-gate.mjs end` is run after the final commit (avoid `pnpm` shim because it attempted an install in this isolated worktree).
- [x] Conduct independent whole-branch review; resolve review findings with RED→GREEN tests; independent follow-up review PASS.
- [x] Record architecture/implementation summary, exact local SHA, checks, open compatibility gate, and 0 live calls in the existing Linear document; read it back and verify.
- [x] Commit remaining in-scope documentation/review corrections.
- [x] Stop at `OWNER DECISION REQUIRED — PROMPTFOO CODEX COMPATIBILITY SMOKE`; offline tests do not prove exact Codex SDK/provider compatibility, and V2 DEV/VALIDATION payload manifests remain empty.

---

## OWNER-approved architecture amendment — Worker LAB adapter

The 2026-10-06 addendum supersedes the Task 1 direct `openai:codex-sdk` execution path. Do not invoke the native provider a third time. Continue from this branch and preserve completed V2 work. New live calls remain prohibited until the new compatibility gate is approved.

### Task 9: Reuse and minimally extend the existing LAB queue contract

**Files:**
- Modify: `services/agata/worker/lab_queue.py` and its fixture manifest from the exact V3 source branch, only if required by the technical-fixture audit.
- Modify: `services/agata/worker/gpoint_mac_worker.py` only for LAB-scoped exact model enforcement if required.
- Test: LAB queue contract regressions in `services/agata/tests/` or Lab offline tests.

**Requirements:**
- Add a dedicated one-turn synthetic Promptfoo compatibility fixture, independent of DEV/VALIDATION/SEALED HOLDOUT.
- Preserve existing Naturalness V1 queue validation and legacy jobs.
- LAB jobs must carry exact `gpt-6-luna / medium`; reject model/reasoning overrides or omission for the compatibility fixture before inference.
- Keep Worker approval, max-call consumption, idempotent job IDs, terminal receipts, slot isolation, and production-first scheduler as authorities.
- No installed Worker or LaunchAgent edits, queue submissions, changes to GPOINT/GSALES payloads, or scheduler/topology changes.

- [x] Verify the canonical source bytes for the installed LAB queue matched the source branch in the prior read-only audit; no installed source was changed.
- [x] Write RED tests for dedicated fixture acceptance, Naturalness fixture backward compatibility, invalid model/reasoning rejection, and unchanged approval/receipt contracts; initial run failed on missing fixture/model resolver.
- [x] Run focused tests and confirm RED was isolated to the missing compatibility contract.
- [x] Implement the LAB-only fixture/schema/model extension; existing Naturalness jobs remain exact-schema validated and approval remains required before claim.

### Task 10: Promptfoo custom provider → Worker LAB lane adapter

**Files:**
- Create or modify: `services/agata/conversation-lab/evaluators/promptfoo_worker_provider.py` (or a Promptfoo-supported JS provider that delegates to the same tested Python adapter).
- Modify: `services/agata/conversation-lab/evaluators/promptfoo_runner.py`, `tooling/promptfoo/promptfooconfig.yaml`, and package scripts.
- Test: Promptfoo provider integration and queue/receipt behavior under private temporary roots.

**Requirements:**
- Promptfoo invokes a custom provider; the provider submits only an AGATA Worker LAB job and returns its Worker receipt-backed response.
- No direct Codex process/SDK; no GPOINT/GSALES access; no generic provider routing.
- Reserve the existing durable Lab generator attempt before LAB queue submission; link Promptfoo invocation ID → LAB job ID → Worker receipt identity; a submitted timeout is AMBIGUOUS and never re-sent.
- Use Promptfoo custom provider contract with retry/cache off, maxConcurrency 1, exact model/reasoning, no fallback.
- Keep tests entirely offline with fake/temp Worker interfaces; do not touch the installed queue.

- [x] Write RED tests for provider invocation, LAB-only submission, Promptfoo result return, approval fail-closed behavior, success/error/ambiguous receipts, duplicate ID refusal, no retry, API-key rejection, and unchanged GPOINT/GSALES model resolution.
- [x] Confirm the test suite fails for the missing custom-provider path before implementation.
- [x] Implement the LAB-scoped Python adapter, bounded Promptfoo runner, pinned config, and disabled native Codex SDK entry point.
- [x] Run targeted provider/queue tests (PASS). Promptfoo CLI provider-loading check is `SKIP`: pinned CLI dependencies are not installed; no dependency install or network access was attempted.

### Task 11: Final offline verification and owner handoff

- [x] Run focused Lab and Worker LAB tests, then `python3 services/agata/conversation-lab/tests/run_offline_safe_suite.py`, V2 dry-run, relevant Worker regression tests, and `git diff --check`. Final: 157 guarded tests, 22 expected skips; 38 focused Lab/Worker tests; 26 Worker LAB/pool/integration tests; V2 dry-run has 0 live calls and sealed holdout unopened.
- [x] Independently review the complete diff for queue isolation, approval authority, no retry, exact model, receipt provenance, and absence of production changes; fix all Critical/Important findings. Review found two P2 items (canonical queue identity and timeout margin); both were fixed and the reviewer confirmed follow-up PASS with no remaining findings.
- [x] Update existing Linear Canonical Architecture and Handoff; update `AGATA Workers` only if the source LAB contract actually changes. Read each updated document back. Handoff current state and the minimal canonical inference path were updated and read back; manual history below the current-state block was preserved.
- [x] Commit the verified change in the isolated branch. Do not push, merge, apply Worker, restart LaunchAgent, create LAB jobs, run live inference/judges/optimizer, or change production. Exact final commit is recorded in the existing Linear Handoff and final task report; the task remains isolated and unpushed.
- [x] Return only at `OWNER DECISION REQUIRED — PROMPTFOO → AGATA WORKER LAB COMPATIBILITY SMOKE` with the exact app/Worker hashes, Promptfoo version, provider path, required activation, one-call envelope, and verification evidence.
