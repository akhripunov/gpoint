# AGATA Conversation Lab Phase 3 — specification

**Scope:** prepare an offline, reproducible 16-scenario comparison of the installed AGATA baseline and three documented model candidates. No live generation, paid API, worker job, production/runtime edit, migration, training, or corpus download.

## Audited baseline (read-only snapshot)

- `baseline_id`: `agata-mac-codex-cli-config-2026-10-04`; source snapshot `core_sha=3c8e105f2168ed72736cc0a9e6c7df1501b2ed8a`. Relevant source files: `services/agata/app/responses/provider_factory.py`, `services/agata/app/responses/mac_worker.py`, `services/agata/app/responses/pipeline.py`, `services/agata/app/i18n/response_context.py`, `services/agata/app/memory/retrieval.py`, and `services/agata/worker/gpoint_mac_worker.py`.
- Active LaunchAgent `one.gpoint.agata-codex-worker`, worker v2; installed worker SHA256 `606490fcb4f5e3bd119bff5759ee994d25543a8fa7e121db41014fd5617c5f60`, matching source at the audited checkout.
- Worker env file `/Users/akhripuniov/.config/gpoint/agata-worker.env` has mode 0600. `AGATA_CODEX_MODEL` and `AGATA_CODEX_REASONING` are empty. The worker therefore invokes the installed Codex CLI with no model/effort override.
- CLI version `0.160.0`; user `~/.codex/config.toml` currently sets `model = gpt-6.1-sol`, `model_reasoning_effort = medium`. This is the resolved CLI configuration at audit time, not an inference-observed receipt; it can change with the user config or CLI update.
- Billing: logged-in Codex CLI subscription path, not OpenAI API token billing. No per-request USD charge can be derived from this config. Subscription/usage allowance is a separate quota and must be verified before a baseline run; do not call this zero-cost or API-credit equivalent.
- Core route: AGATA configuration selects `mac_worker` only when enabled; provider factory passes configured `llm_model` as request metadata while the Mac worker's empty model override defers to Codex CLI config. No worker job was enqueued.
- Core context `context-v1`: Persona (name, identity/personality, style, boundaries, language), communication settings, up to 24 memories, 4 dialogue summaries and 12 history turns reach the worker; source builder additionally bounds retrieved-memory text and caps recall/context. The worker asks for text only and permits no tools. Core has no generation temperature/max-token setting for the Codex route; worker JSON output is capped at 4,096 characters, timeout at 180 seconds. CLI reasoning is medium from current user config.
- No live production environment values, API keys, messages, memory records, user identifiers, or LaunchAgent process identifiers are part of this snapshot. Runtime provider enablement/effective production values and billed subscription allowance are not independently read from production; mark them UNKNOWN where applicable.
- Read-only Codex account usage snapshot at 2026-10-04 10:03 Europe/Moscow: Plus plan, 98% consumed in the current 5-hour window, 67% in the 7-day window, no additional credits. This account-wide state is not a per-request receipt or proof that 72 baseline calls fit; live baseline execution remains blocked pending a separate quota decision.

## Frozen evaluation inputs

Select the existing `-01` scenario in each Benchmark V1 category, including `long_dialogue-01`: 15 × 4 response rounds and one × 12 = 72 replies per configuration, 288 planned replies across baseline plus three candidates. Freeze exact IDs, full scenario hashes, benchmark manifest, synthetic evaluation Persona hash, per-scenario initial memory/context hashes and `rubric-v1.json` hash. Never include real corpus examples or production memories. Do not alter Benchmark V1 fixtures.

Persona is the tracked synthetic `agata-eval-v1`; it does not assert production Persona equality. Reset to the same initial memory/context for each scenario and each model. After the first turn, each candidate replays only its own assistant replies. Never provide future user turns, story synopsis, evaluation notes or expected answers as model input.

## Adapter contract and offline boundary

One canonical invocation contains scenario ID, frozen Persona, initial Memory/context, reached user turn, that candidate's prior conversation, and generation caps. The frozen context is conveyed to every configuration as a synthetic context item in the existing `dialogue_summaries` field; it is not future turns, setup prose, or evaluator guidance. All configurations use the worker's same outer and request instructions and its `{ "text": string }` response schema. Candidate API payloads request the equivalent provider-native structured output; parsers reject schema-invalid responses. Provider adapters map this to documented OpenAI Responses, Anthropic Messages, and Google generateContent request payloads and normalize mocked/imported response, usage, latency, refusal and transport-error shapes. The baseline adapter identifies the audited Codex route but does not invoke Codex. There is no HTTP client, API-key lookup, process launch, worker enqueue, or generation method in Phase 3. Payload construction is not inference.

Structured output request references (checked 2026-10-04): [OpenAI Responses](https://developers.openai.com/api/docs/guides/structured-outputs), [Anthropic Messages](https://platform.claude.com/docs/en/build-with-claude/structured-outputs), [Gemini generateContent](https://ai.google.dev/api/generate-content). Model/account compatibility is not authenticated or endpoint-tested in this offline phase.

Every network-generation capability remains disabled without a separate OWNER execution grant. No endpoint-availability probe or free-tier fallback. Unknown or absent approval fails before constructing a sender. Future runtime integration is a distinct review.

## Budget and hard bounds

Retain Phase 2 token assumptions only as planning estimates: expected 433,152 input and 73,728 billed output tokens per candidate; candidate expected zero-cache total $3.808512. For candidate worst-case reserve, apply published cache-write input pricing to all OpenAI/Anthropic input and ordinary Google input to the full-replay cap envelope (672,768 input / 147,456 billed output tokens per candidate), add at most one maximum-sized retry per provider (32,768 input / 2,048 output each), then 20% contingency: $9.129063 (ceil $9.13), below proposed candidate-only $10 stop threshold. Planned calls: 288 initial replies; at most three extra retries, total 291 external requests if separately approved. No automatic baseline retry. Actual full-experiment max spend remains BLOCKED/UNKNOWN until subscription quota and baseline usage policy are verified. The $10 is a proposed candidate-only ceiling, not spending permission or provider-side billing protection.

A pure software budget guard must reject missing/invalid grants, unknown billing model, exhausted calls/tokens/USD, or any request whose conservative maximum charge exceeds remaining approval. Guard cannot send requests and cannot establish provider-side billing caps. The eventual execution grant must bind exact candidate IDs, frozen hashes, baseline policy, expiry, retries/calls, per-request token caps and total budget.

## Results and blind rating

Reuse `evaluators/blind.py`. Freeze completed OWNER ratings before unblinding; a locked rating file is immutable. Corrections require a new review-cycle ID referencing prior lock hash and owner reason. Unblinding verifies the private mapping against its hash and a matching locked rating set. Failed/partial/refusal/transport-error replies remain explicit and never become synthetic successful answers. Reviewer packets contain no model IDs, mapping, seed, provider usage headers or cost.

Measure the ten requested quality metrics by model/category/scenario and separately report memory hallucination, contradiction, sycophancy, excessive advice, repetition, generic assistant tone, refusal and transport failures. Human ratings remain primary. Track tokens/thought, input/output, cache, latency, first visible token, request and conversation cost when returned. No quality winner absent actual authorized outputs and OWNER ratings.

## Acceptance checks

- 16 exact IDs, category coverage, hashes and shared Persona/context contract are reproducible.
- Adapter payloads have identical semantic Persona/initial Memory/context/user inputs, correct candidate-specific replay, and no future/evaluator leakage.
- Offline mock run covers 288 replies without sockets, external process, provider auth, Codex CLI invocation or worker queue use.
- Response validation, token estimates, USD guard, bounded retries, rating lock/unlock-by-new-cycle and correct unblinding are tested.
- Existing Phase 2 tests stay green; fresh independent review has no unresolved Important findings.
- Linear existing Conversation Lab updated; stacked Draft PR depends on #12. No merge/deploy.
