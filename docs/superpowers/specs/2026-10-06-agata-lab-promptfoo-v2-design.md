# AGATA Conversation Lab V2 — Promptfoo Backbone and Blind Evaluation Design

> **Architecture amendment (OWNER-approved 2026-10-06):** this design's original direct `openai:codex-sdk` execution path is superseded for current Lab inference by §11. Promptfoo remains the caller; the existing AGATA Worker LAB lane is the only current inference execution layer. Do not run the native provider again.

**Status:** OWNER approved 2026-10-06
**Scope:** local research/evaluation tooling in `services/agata/conversation-lab/` only.
**Base checkout:** isolated worktree at `codex/agata-naturalness-v1`, starting SHA `1b21e8f8eb5d1bc42826baaa54559672969b3f40`.
**Frozen Naturalness candidate:** PR #14 head `e4b843d7ddaed1f96a8d24e8a186712b2e371896`; this design does not modify it.

## 1. Goal and boundaries

Move generic evaluation execution into Promptfoo while keeping AGATA's identity, scenarios, privacy boundaries, blinding, OWNER locks, and promotion decisions under AGATA-owned code. The Lab remains a local research layer. Nothing in this design changes production AGATA, Core, GPOINT/GSALES, Worker, VPS, database, or deployment behavior.

Promptfoo is an execution engine, not the authority for AGATA behavior or release decisions. The design does not create additional OWNER/Judge chats, a new Worker lane, a dashboard, or a general training platform. The compromised Benchmark V1 `-01` material remains unopened and excluded. The first locked human-rated Naturalness smoke is permitted only as a calibration reference described below; it is distinct from the compromised benchmark material.

## 2. Selected architecture

Use a pinned Promptfoo installation under `services/agata/conversation-lab/tooling/promptfoo/`, with a dedicated `package.json` and lockfile. Do not add Promptfoo or Node dependencies to the root workspace or any production AGATA package. The selected initial pin is Promptfoo `0.123.1` and `@openai/codex-sdk` `0.156.1`, subject to the offline compatibility checks in the implementation plan. The local runtime is Node `22.23.1`; the Codex CLI is `0.160.0`.

Use Promptfoo's `openai:codex-sdk` provider with an explicit model `gpt-6-luna`, reasoning `medium`, `maxRetries: 0`, ephemeral threads, `sandbox_mode: read-only`, network/search disabled, a dedicated empty working directory, and no API-key configuration. The Codex/ChatGPT subscription login is the only permitted auth path. No API keys are searched for, copied, or placed in configuration. Because Promptfoo documents a default of three scheduler retries, `maxRetries: 0` is mandatory in every live configuration. Caching is disabled for bounded scientific calls, concurrency is one, and no retry/resume command is used.

Promptfoo does not expose the backend-resolved model name in its Codex SDK result. Therefore the implementation may prove that the exact model is requested and that no configured fallback exists, but it must not claim independent backend model attestation unless a supported receipt provides it. If a live compatibility proof is needed, it is a separate, one-call OWNER gate after all offline work; do not make that call during implementation.

### Data flow

```text
AGATA scenario / Persona / memory / context builders
                    ↓
AGATA-owned serial multi-turn orchestrator
  - identical initial inputs; independent A/B histories
  - exact model/reasoning; hard call ledger; no retries
                    ↓
Promptfoo Codex SDK provider (one ephemeral call per turn)
                    ↓
Private immutable raw receipts and outputs
                    ↓
AGATA deterministic Persona/Grammar Sanity Gate
                    ↓
AGATA blind packet + separate private arm mapping
                    ↓
Promptfoo isolated LLM judge invocations (3 per pair)
                    ↓
AGATA deterministic aggregator / HUMAN_ESCALATION
                    ↓
OWNER control/escalation review, locked before unblind
```

AGATA retains its turn orchestrator because it must prove equal initial fixtures and separate histories. Promptfoo is invoked for each generation turn with the reached user turn and only that arm's preceding turns. A baseline invocation never receives Candidate replies and a Candidate invocation never receives Baseline replies. Promptfoo's generic session state is not used to merge histories.

Promptfoo executes judges only after AGATA creates an anonymous X/Y packet. Each of three invocations receives a fresh context, the same minimum Persona evaluation brief and versioned Owner Preference Rubric, and the same pair with controlled presentation order. The judge input contains no arm mapping, candidate SHA, Naturalness Policy, candidate examples, generator reasoning, or other judge outputs. The deterministic aggregator alone sees normalized verdicts and private mapping. It is not an LLM.

## 3. OWNER Preference Rubric V1

Add a machine-readable versioned rubric, separate from Naturalness Policy. It scores overall preference plus these eight permanent core dimensions:

1. `naturalness`
2. `human_likeness`
3. `emotional_appropriateness`
4. `persona_consistency`
5. `initiative`
6. `less_ai_template_like`
7. `conversation_coherence`
8. `direct_request_fulfillment / responsiveness` — whether AGATA fulfills the user's direct request rather than unnecessarily interviewing, evading the task, or giving unsolicited advice.

The deterministic aggregator's `>=6/8` dimension threshold uses only these eight permanent core dimensions as its denominator. Memory, disagreement/boundaries, humor/flirt, and engagement are conditional dimensions and remain additional evidence/escalation signals when applicable; they are never included in the 8-dimension denominator.

The rubric includes OWNER-confirmed positive and negative conversational signals supplied in the task. It is an evaluation contract, not a generation instruction. The first locked OWNER-rated Naturalness smoke is one golden calibration pair. Three blind judges may evaluate its already-recorded pair without seeing the OWNER result; only after their outputs are locked may calibration agreement be computed. One case is initial evidence only and cannot establish broad judge reliability.

## 4. Persona / Grammar Sanity Gate

Add an executable AGATA-owned validator in the shared blind packet path. It checks both arms before a packet can be emitted, never edits text, and returns stable defect codes with scenario/turn references. It checks explicit persona contradictions, AGATA's feminine self-reference, known user gender when declared by the scenario, neutral handling when user gender is unknown, internal arm labels, technical/treatment metadata, exact roles/turn count/order, and unchanged approved user turns.

Structural, label-leak, or technical-metadata failures fail closed and keep the packet private. Persona/gender findings are persisted in the private run receipt, force `HUMAN_ESCALATION`, and remain quality evidence; they are not embedded in the public blind packet or shown alongside dialogue before OWNER lock. This preserves blinding and follows the gate's non-editing contract. No silent retries are allowed after a model invocation.

For current scenarios the fixture explicitly identifies a male test user and the canonical synthetic AGATA evaluation Persona. For a scenario with unknown user gender, the validator does not infer gender from a name or appearance; it flags high-confidence gendered second-person forms for review where appropriate and uses a conservative neutral-reference policy. Heuristics are deliberately framed as a sanity check, not a complete Russian morphology parser.

## 5. Blind judges and deterministic aggregation

Run three independent judges per pair with explicit `gpt-6-luna / medium`, isolated ephemeral contexts, zero retries, and structured JSON output validated locally. Rotate pair presentation order: one judge receives X/Y, one Y/X, and the third independently randomized; the receipt records presentation order privately and the aggregator normalizes choices to the canonical labels. No judge sees another's output.

`Owner Preference Rubric V1` is the only evaluation guide. Anonymize both candidates and strip generation/provider metadata before Promptfoo sees the pair. Use Promptfoo's LLM-rubric / pairwise evaluation execution where it fits; AGATA owns the exact schema and normalization.

The Python aggregator is deterministic and versioned. `AUTO_WIN_X` or `AUTO_WIN_Y` requires 3/3 normalized overall agreement, majority for the same side on at least six of the eight permanent core dimensions listed in §3, no uncertainty, no critical Persona/safety/grammar defect, and no order sensitivity. Conditional dimensions are supplemental evidence and do not change that denominator. Any 2/1 split, material tie/disagreement, critical defect, uncertainty, position sensitivity, missing output, or out-of-rubric defect is `HUMAN_ESCALATION`. The system never spends additional calls to manufacture consensus.

Each meaningful batch chooses one random human control before unblind, plus every escalated pair. While fewer than ten OWNER-labeled pairs exist, retain at least one control per meaningful batch. At ten or more, reduce to approximately 10% only if agreement is at least 90% and critical dimensions show no systematic mismatch. Never automatically reduce human review to zero.

For the current four-scenario run, preserve the existing OWNER approval without expanding it: at most 8 conversation arms/runs total (4 Baseline and 4 Naturalness Candidate), each with four assistant generation turns. These arms correspond to at most 32 underlying per-turn generator provider invocations; 32 is not 32 separate Baseline/Candidate experiments. Retries are zero. The judge budget is at most 15 provider invocations: three on the historical golden pair and three per each of four new pairs. The approved 16 user turns are immutable. A call ledger separately counts conversation arms, generator provider invocations, and judge provider invocations. No generation begins until the Promptfoo compatibility path, budget guard, exact Candidate freeze, and offline parity checks are ready. Do not create an extra call to probe compatibility; if the provider contract cannot be proven offline, stop at the single-call OWNER approval gate.

## 6. Holdout V2 and optimization gate

Keep the compromised `-01` scenarios excluded. Define separate train/dev, validation, and replacement sealed-holdout manifests. The development and validation inputs may be used by a future optimizer only under their defined roles. The optimizer adapter receives no sealed-holdout plaintext or path; sealed evaluation runs only after candidate freeze and returns aggregate results. Once a sealed scenario is exposed for an evaluation, mark it consumed and do not feed it back into policy rewriting. Tests must prove that Promptfoo Optimize config is built from an explicit allowlist that rejects the sealed split.

No final holdout plaintext is added to Git. The implementation can define schema, split boundaries, commitments, and access checks without inventing/reading compromised records. Any later creation or opening of replacement sealed scenarios must be additive, synthetic, and recorded as a consumed evaluation asset.

Promptfoo Optimize is the first supported optimizer adapter. Its lifecycle has three distinct states:

- `OPTIMIZER_ADAPTER_IMPLEMENTED`: the adapter/config/code/tests exist and pass offline. This status requires no calibration sample and authorizes no optimization calls.
- `OPTIMIZER_READY`: may be set only when all of the following pass: execution parity; blind/private isolation; judge isolation; deterministic aggregation; replacement DEV / VALIDATION / SEALED HOLDOUT isolation; optimizer sealed-holdout deny/allowlist tests; call/budget guards; at least 10 OWNER-rated blind pairs; judge-ensemble overall agreement with OWNER of at least 90%; and no systematic critical-dimension failures.
- `OPTIMIZER_RUN_AUTHORIZED`: requires separate bounded OWNER approval specifying the exact dataset/split, maximum candidate count/model-call budget, and starting Naturalness Policy hash. `OPTIMIZER_READY` alone never authorizes `promptfoo optimize`.

For this methodology, a **systematic critical-dimension failure** is present when, across calibration pairs applicable to a critical dimension (`persona_consistency`, `emotional_appropriateness`, `direct_request_fulfillment / responsiveness`, or a Persona/safety/grammar defect flag), the judge ensemble either disagrees with OWNER on the winning side for that dimension or misses an OWNER-recorded critical defect in at least 2 distinct OWNER-rated pairs. The overall minimum remains at least 10 OWNER-rated blind pairs; critical-dimension applicability is recorded per pair, and this rule does not create a separate ten-pairs-per-dimension requirement. This definition is deterministic and testable; the critical-dimension set is versioned with the rubric.

The adapter cannot change Persona, Memory, safety, production prompts, merge, or deploy. DSPy is not installed and remains a future comparison after at least 10–20 OWNER-calibrated pairs. Phoenix remains an optional future local UI. Opik, Langfuse, DeepEval, Ragas, TRL, and OpenRLHF are not current runtime dependencies.

## 7. Receipts and isolation

Raw dialogs, judge prompts/outputs, blind mapping, and usage receipts stay in ignored local run directories with restrictive permissions. Version-controlled output contains only code, safe synthetic fixtures, rubric/methodology, hashes, and aggregate summaries without production data. Receipts record run/scenario IDs, requested model/reasoning, Candidate SHA and treatment hashes, sanity result, call ledger, private mapping, judge order, raw structured judge receipts, normalized result, aggregator version/decision, random control selection, OWNER lock/unblind state, and final classification. Judge inputs are separately materialized and tested to omit all private fields.

No production package dependency, production prompt, deployed runtime, database, Worker, GPOINT/GSALES, LaunchAgent, credentials, or VPS file is changed.

## 8. Failure handling

Every attempted provider invocation consumes one unit from its stage's budget once a live turn starts. The call ledger separately records conversation arms, generator provider invocations, and judge provider invocations; limits are 8 arms, 32 generator invocations, and 15 judge invocations for this batch. `maxRetries=0`; no hidden retry, resume, or retry-errors path is used. A failure before invocation may be diagnosed offline. A timeout, quota rejection, provider error, ambiguous completion, leakage, or missing receipt after invocation is counted and not retried. Structural blindness failures stop packet release. Quality/persona defects are recorded privately and route the pair to HUMAN_ESCALATION without editing the text.

Promptfoo settings are pinned in a Lab-only manifest. Before any Codex/Promptfoo live invocation, a fail-closed auth preflight inspects effective process environment and provider/config inputs. It refuses to invoke if `OPENAI_API_KEY`, `CODEX_API_KEY`, provider `apiKey`, or any other explicitly configured API-key authentication path is present. The runner sets `inherit_process_env: false` and passes an explicit minimal environment that cannot forward API-key variables. Tests prove that no API key is read or forwarded. The only permitted Lab auth path is the already signed-in Codex/ChatGPT subscription session. If the official Codex SDK provider cannot establish requested Luna/medium and this subscription login without API-key auth, fail closed and prepare the single compatibility approval request; do not silently substitute another provider or model. If a minimal custom adapter is needed, it must call the proven direct Codex Local path through a restricted interface, retain `maxRetries=0`, and let Promptfoo orchestrate evaluation. It cannot expand filesystem, network, or tool permissions.

The X/Y presentation order and random OWNER control selection are reproducible. Derive a deterministic seed from the immutable run/batch identity (or use an equivalent persisted deterministic mechanism); persist the seed, judge presentation orders, and selected control in the private receipt before judge execution and before OWNER unblind. Public packets contain neither seed nor private selection metadata.

## 9. Verification criteria

Offline tests must prove:

1. Promptfoo and SDK versions are pinned only in Lab tooling; production package manifests are unchanged.
2. Requested model is exactly `gpt-6-luna`, reasoning is `medium`, API-key auth paths are rejected, `inherit_process_env` is false, only explicit minimal environment is passed, no API-key variables are forwarded, no fallback is configured, retries are zero, and tools/network/write access are disabled.
3. Baseline/Candidate payload parity holds except treatment and their histories remain independent.
4. Blind mapping, Candidate SHA, treatment, and generator reasoning never enter judge prompts.
5. Judge contexts and call receipts are separate; X/Y reversal normalizes correctly; presentation order and OWNER control selection reproduce from the persisted immutable run/batch identity.
6. The deterministic aggregator is repeatable and implements exact AUTO/HUMAN thresholds, including 3/3, 2/1, ties, uncertainty, position sensitivity, and critical defects.
7. Persona/grammar checking reports defects without changing text; unknown gender is not guessed.
8. OWNER ratings lock before unblind; control selection is persisted pre-unblind.
9. The compromised legacy holdout cannot be loaded by current workflows; optimizer input allowlists exclude replacement sealed holdout.
10. Call caps independently count 8 arms, 32 generator invocations, and 15 judge invocations; zero retries and no cache reuse are enforced by fixtures and unit tests; raw outputs remain ignored/local.
11. No production or Worker dependency/runtime changes occur.

The current four-scenario results are not shown as a four-pair owner packet by default. After blind judging and aggregation, the OWNER receives one preselected random control pair plus only actual HUMAN_ESCALATION pairs, without judge verdicts or arm mapping before OWNER lock.

## 10. Evidence and open gate

Local checks on 2026-10-06: worktree Node `22.23.1`; Codex CLI `0.160.0` (from prior direct CLI check); isolated worktree is clean at its starting commit. `pnpm repo:clean:start` cannot run in this managed worktree because its dependency bootstrap tries to create a temp file and receives `EPERM`; this is a worktree permission limit, not a source/test failure. No project dependency was installed.

Official Promptfoo documentation says its Codex SDK provider can reuse an existing Codex/ChatGPT login when API-key variables/config are absent, supports explicit model/reasoning, sandbox/network/working-directory controls, creates ephemeral threads by default, and defaults `maxRetries` to 3. Official package metadata reports Promptfoo `0.123.1`, MIT license, Node `>=22.22.0`; the Codex provider docs require Codex SDK `0.156.1` or later for GPT-6 models. Sources:

- https://www.promptfoo.dev/docs/providers/openai-codex-sdk/
- https://www.promptfoo.dev/docs/configuration/rate-limits/
- https://www.promptfoo.dev/docs/guides/llm-as-a-judge/
- https://www.promptfoo.dev/docs/usage/command-line/
- https://www.npmjs.com/package/promptfoo

No package was installed and no model request was made. These are documentation/package facts, not a proof that this account's local login can execute the exact Luna/medium provider path. If offline checks cannot establish that contract, implementation stops before any live Promptfoo invocation and requests exactly one compatibility-call approval.

## 11. OWNER architecture amendment — Promptfoo through the existing Worker LAB lane

This section supersedes the provider and execution-path decisions in §§2, 8, and 10 where they conflict. Promptfoo is still the execution/evaluation backbone and AGATA Lab remains the domain authority. Current live inference must follow:

```text
Promptfoo eval → Lab-local custom provider → existing AGATA Worker LAB queue/slot
               → Codex Local subscription → Worker receipt → provider result → Promptfoo
```

The custom provider is only a LAB-scoped adapter. It may submit to and read results from the existing LAB queue; it must not have GPOINT/GSALES queue paths or generic provider selection. The installed Worker remains the only Codex process manager. No direct Codex subprocess, SDK, or native Promptfoo Codex provider is permitted in this path. The two historical native-provider receipts remain immutable evidence; no third native attempt is allowed.

Read-only audit for this amendment found installed Worker source SHA `a4b7c76650f58d2bd33ca1b33e69006ef7a1ae26`, installed bundle SHA `bb19c0553d341f2464f5680434e3498b318e734472754f4290caaa74edea2c1c`, a private `queues/lab` contract, Worker-owned source-pinned approvals and usage ledger, per-job terminal receipts, an atomic duplicate-resistant local LAB spool, and weighted production-first scheduling. Current status readback reported LAB `DISABLED`; no queue submission or Worker mutation was performed. Its current pinned fixture manifest contains only the Naturalness V1 experiment's development scenarios. A compatibility smoke must not use those scenarios, so a dedicated one-turn synthetic technical fixture and narrowly scoped LAB contract extension are required before a future smoke. The installed environment did not explicitly set `AGATA_CODEX_MODEL` or `AGATA_CODEX_REASONING`; therefore the future smoke must carry and enforce exact per-job `gpt-6-luna / medium`, rather than infer model identity from defaults.

The minimal contract extension is backward-compatible and LAB-only: add an independent `promptfoo-worker-compat` fixture/experiment; allow an explicit exact model/reasoning pair for LAB jobs and reject every other pair; preserve legacy Naturalness job validation; keep Worker approval and call consumption authoritative; and retain existing queue/receipt/slot/scheduler semantics. Do not change GPOINT/GSALES job schemas, scheduling weights, concurrency, Worker topology, or installed Worker. A separate live OWNER gate must cover any later Worker apply/restart that is necessary to activate this new contract.

The Lab durable ledger reserves its generator attempt before submission. A private receipt links the Promptfoo evaluation/test ID to the Worker job ID and immutable Worker receipt. A timeout after queue submission is `AMBIGUOUS` and consumes the attempt; the provider never retries or resubmits. Promptfoo retry is fixed at zero and cache is off. The Worker remains authoritative for approval validity, max-call consumption, model execution, and execution receipt; a missing/expired approval must stop before Codex inference and cannot affect production lanes.

Offline tests use only temporary queue roots and mocked Worker results. They prove the actual Promptfoo custom provider is invoked, submits to LAB only, returns a terminal Worker result to Promptfoo, and cannot reach GPOINT/GSALES. No live jobs, native provider calls, Worker apply/restart, Production, or GPOINT/GSALES changes are part of implementation. DEV remains 4, VALIDATION remains 4, compromised V1 remains excluded, and SEALED HOLDOUT remains unopened/empty.

The remaining gate is `OWNER DECISION REQUIRED — PROMPTFOO → AGATA WORKER LAB COMPATIBILITY SMOKE`. It will specify the exact application commit, Worker source/bundle SHA, pinned Promptfoo version, adapter path, LAB contract path, model/reasoning, subscription-only authentication, one bounded invocation, zero retries/fallback, timeout and expected receipt linkage, and explicit untouched-lane/runtime evidence. Native `openai:codex-sdk` must not be invoked.

### Offline implementation evidence (2026-10-07)

The Promptfoo provider is implemented in `evaluators/promptfoo_worker_provider.py` as a LAB-only local queue adapter. The pinned synthetic compatibility fixture is independent of every Lab data split. The runner constructs a one-provider Promptfoo eval and calls only this Python provider; the previous native `openai:codex-sdk` runner entry now fails closed with `NATIVE_PROVIDER_UNAVAILABLE_CURRENT_ENV`.

The adapter reserves its existing durable generator-ledger attempt before enqueue; a deterministic Worker job ID binds that attempt to the LAB queue. Terminal Worker identity and requested model/reasoning are checked, and a private mode-0600 linkage record stores the terminal receipt hash, job ID, Promptfoo invocation ID, run ID, and outcome. Definite pre-inference Worker rejection records `provider_error`; missing/ambiguous post-submit evidence records `ambiguous`, with no retry or resubmission. The Worker still verifies the OWNER approval and consumes its own existing approval budget before inference. The new LAB-only model resolver pins this fixture to `gpt-6-luna / medium`, rejects API-key environment paths, and leaves GPOINT/GSALES model selection unchanged.

The runner and provider each validate that the queue path is exactly the Worker-selected canonical `queues/lab` path, including both lexical and resolved-path equality; caller-supplied GPOINT/GSALES or arbitrary queue roots fail before Promptfoo starts or queue writes occur. Promptfoo's outer process timeout is 300 seconds, while the provider receipt wait is bounded at 240 seconds, leaving startup/teardown margin without enabling retries.

Offline evidence proves queue schema acceptance, compatibility-fixture immutability, model and auth guards, Promptfoo-to-adapter dispatch, durable reservation ordering, receipt linking, no duplicate submission, and unchanged Worker approval pre-claim behavior. Promptfoo `0.123.1` and Codex SDK `0.156.1` remain pinned in the Lab lockfile; the Promptfoo CLI is not installed in this worktree, so an actual offline CLI provider-load check is `SKIP`. No live provider call, LAB job, Worker install/restart, LaunchAgent action, Production, GPOINT, or GSALES operation occurred. Current Worker source edits require a future separately approved installation/compatibility smoke and do not change the installed Worker.
