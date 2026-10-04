# AGATA Conversation Lab Phase 2 — specification

Date: 2026-10-04 (Europe/Moscow). Owner: Александр.
Status: DESIGN APPROVED; INTERNAL SPECIFICATION REVIEW PASSED.
Scope: original Phase 2 v1.1 task and Owner confirmations. This is a design,
not a claim that research, benchmark or implementation has been completed.

## 1. Purpose and boundaries

Extend the existing Conversation Lab to evaluate AGATA as a natural Russian
companion: personality, emotional fit, humor, initiative, multi-turn coherence,
accurate memory and respectful disagreement. Do not create another AGATA Core,
message store, persona system or runtime provider.

Production, production databases/migrations, production Persona, G Sales,
MAXIBOT, paid LLM calls, training, fine-tuning, model downloads and new dataset
downloads are outside scope. Do not automatically merge PR #10.
Do not send reports to another chat. All communication stays in this chat and
the single existing Linear document AGATA Conversation Lab.

Work in the isolated worktree and branch codex/agata-conversation-lab-phase2,
based on PR #10 SHA f3f6069f6455f16ca82d59e1e075fab50d8919ee.
Do not switch, stage, stash or clean the dirty G Sales checkout.

## 2. Existing components and targeted changes

The existing lab README, configs/datasets.json, pipelines/lab.py and
12-test suite remain the starting point. Keep six starter sources and the
fail-closed deferred RLDD/Toloka policy. Do not execute downloader, process or
legacy quality-review against the canonical collection.

Add focused offline modules under evaluators/ for integrity/read-only review,
benchmark validation, leakage checks and blind-result preparation. Put
versioned schema/rubrics under configs/, experiment instructions under
experiments/, regression tests under tests/, and only approved synthetic
scenarios under data/golden/synthetic/. README explains exact commands,
input/output paths, stage status and known limitations.

AGATA app/responses/provider_factory.py, app/responses/pipeline.py,
app/i18n/response_context.py, character/persona/memory components, database
schema, API/Worker and release workflows receive no modifications. Their
existing boundaries are documented for a future comparison; Phase 2 does
not implement a second response engine or run production inference.

Changes to legacy lab.py are permitted only for audit-proven problems directly
required by this task, after failing isolated tests. No general refactor or
renormalization of existing processed data.

## 3. Immutable data boundary and provenance

Canonical input directory:
/Users/akhripuniov/Downloads/Codex/GPoint/services/agata/conversation-lab/data/

Read raw, processed, existing manifest and review pointers through explicit
read-only input paths. Do not copy whole corpora into the worktree, move,
delete, overwrite, hardlink, or expose them through writable pipeline symlinks.
Validate source IDs, manifest paths and requested files against canonical input
roots. Reads fail closed if manifest provenance/checksums or pointer identity
are inconsistent. Report the actual verified path, size and SHA256.

Canonical local output is a new, unique directory:
data/runs/phase2-<run_id>/ beneath the existing canonical lab data directory.
It must not overlap raw/processed, manifest.json, report.json or prior run
folders. Existing output files are not silently replaced. Evidence records
base code SHA, client date, input manifest/checksums, versions, command and
run configuration. Original inputs remain untouched, including review pointers.

The V1 audit found that quality_review_one replaces dialogs.jsonl. Phase 2
must not invoke it on original data. A new read-only review path is preferable
to retrofitting a destructive pipeline into the research reader.

## 4. Real-dialogue review: 180 complete conversations

Read all 30 existing pointed conversations in each of Russian Dialogues 2,
Blended Skill Talk RU, DialogSum RU, EmpatheticDialogues, DailyDialog and
Cornell Movie-Dialogs. Verify the pointed record ID and read every turn;
truncated output or metadata inspection does not count. Current evidence is
30 completed Russian Dialogues 2 reviews; the other 150 remain to be finished.
Stream each JSONL once to retrieve requested lines without loading the whole
2.2 GB collection or producing another corpus copy.

Each review records source, source record ID, processed line, input checksum,
turn count, rating HIGH QUALITY / ACCEPTABLE / LOW QUALITY / REJECT, concrete
reason and dimension notes: naturalness, lexical variety, coherence, emotional
fit, initiative, humor, colloquial language, translation artifacts, formulaic
responses, repetition, technical noise, context loss and excessive shortness.
Ratings reflect a full-text human/agent judgment; automatic tags are not proof
of conversational quality. Never report population quality percentages from
this evenly spaced, nonrandom sample.

Per-source report contains rating counts, strengths, weaknesses, selection
bias and useful abstract patterns for daily conversation, humor, support,
romance, disagreement, initiative and sustaining a topic. A missing useful
pattern is explicitly recorded rather than invented. Russian Dialogues 2 must
not dominate style by its corpus volume. No automatic training suitability
claim is made for the 1,839,421 accepted records.

Original dialogues remain local. Before displaying text for inspection,
conservatively redact contacts/URLs and unnecessary identifying information.
Committed artifacts and Linear contain aggregate findings and abstract
patterns, never real corpus excerpts, production memories or contact data.
PII classification is distinct from conservative display redaction. Synthetic
regressions cover phones/email, dates, currency, long numeric IDs and order
references; do not weaken contact protection to eliminate false positives.

## 5. Primary-source research

Competitors: Nomi, Kindroid, Replika, Character.AI and OnlyChat AI. For each,
cover proprietary models, known base LLMs, post-training, Persona, memory,
character consistency, preferences, quality evaluation and transferable
engineering principles. Every claim includes a primary URL, checked date,
evidence and CONFIRMED / REPORTED / HYPOTHESIS / UNKNOWN classification.
CONFIRMED means directly supported by the cited source; proprietary product
claims still identify their vendor origin. Unknown model families or tuning
methods remain unknown. OnlyChat commercial conversation is a research
reference, not authorization to alter G Sales or merge product directions.

Scientific review includes original PersonaGym, EmoCharacter, LongMemEval,
LoCoMo, sycophancy and multi-turn evaluation publications. Record exact title,
source, methodology, measured results, limitations and practical test ideas.
Inspect original papers for numerical claims. Do not merely copy abstracts
or generalize English/fictional-character findings to Russian AGATA.
No datasets accompanying papers are downloaded.

## 6. AGATA Benchmark V1

Exactly 128 independently authored synthetic multi-turn scenarios: eight in
each of the 16 required categories. Scenario IDs are stable and versioned.
Categories: Russian naturalness; daily conversation; humor; emotional support;
romance; AGATA persona stability; nonintrusive initiative; topic change;
return after a long break; long-term memory; fact updates; invented memories;
appropriate disagreement; avoiding constant advice; long coherent conversation;
personal boundaries and consent.

Required fields:
- scenario_id: unique stable string.
- category: one of the 16 canonical category IDs.
- language: ru for V1 scenarios.
- context: synthetic conversation setup, session/time boundaries and purpose.
- character_context: canonical evaluation Persona reference and synthetic
  scenario-specific character context; no production Persona mutation.
- memory_fixture: synthetic facts with stable IDs, provenance/session dates,
  supersession and absence information as applicable.
- dialogue_turns: ordered user prompts and response slots with speaker role,
  session/time metadata and turn-specific expectations; generated replies are
  not prefilled as successful answers.
- expected_behavior: observable positive requirements.
- failure_patterns: concrete negative behaviors, including critical failures.
- evaluation_notes: rubric, evidence needed, limitations and reviewer guidance.

Ordinary scenarios contain at least four user-response rounds. Long-dialogue
scenarios contain at least twelve and test earlier-topic coherence. Session
breaks and fact updates must be represented explicitly. Eight examples per
category must differ in situation, interaction dynamics or memory challenge,
not only names or punctuation. Shared Persona is fixed across comparison;
scenario fixtures vary only by scenario and are identical across models.

Fixtures are independently written; corpora inform abstract patterns only.
Keep the evaluation set apart from future training/few-shot materials.
Do not feed expected_behavior, failure_patterns or evaluation_notes into
candidate prompts: they belong to the evaluator. Versioned manifest records
scenario hashes, provenance as synthetic, category coverage and Persona hash.

## 7. Validation and leakage controls

Offline checks reject missing/wrong fields, unknown categories, invalid roles,
empty or unordered turns, missing memory references, unsupported schema version,
wrong scenario count/coverage, duplicate IDs and duplicate conversation content
including whitespace/case normalization. Flag template/near-duplicate scenarios
for explicit review; lexical variation alone is insufficient independence.

Compare normalized synthetic turns and multi-turn spans against the six
canonical corpora locally, using streaming and bounded-memory hashes. Include
synthetic controlled fixtures proving detection of copied single turns and
multi-turn passages; evaluate near-copy/lexical-overlap alerts without blindly
rejecting unavoidable common phrases. Never export matching source passages.
Report what exact-copy and heuristic checks can and cannot detect; there is no
claim of mathematically proving absence of every possible semantic leak.

Future training/few-shot sets require explicit disjointness checks against the
frozen benchmark manifest. If they are unavailable, mark that check NOT YET
APPLICABLE, not PASS. No source examples enter GitHub or Linear.

Automated structural/leakage checks are separate from conversation-quality
judgments. Regex or schema success does not establish empathy or naturalness.

## 8. Reproducible blind evaluation protocol

Future baseline: existing AGATA Core plus its current provider. A config default
or Codex UI model label does not prove the actual deployed model. Baseline model
and configuration must be recorded from an authorized verified source before
a paid experiment; remain UNVERIFIED if not established in this offline stage.

Candidates use the same Core, Persona version/hash, memory fixtures, locale,
context budget/assembly policy, user prompts and scoring rubric. Record any
provider differences that cannot be equalized, exact model IDs, parameters,
API versions and prompt hashes. Distinguish an end-to-end Core comparison from
a fixed-context model-only comparison; never mix their scores as equivalent.

Prepare an offline run manifest with benchmark/version hashes and fixed seed.
For each scenario, independently shuffle candidate order and assign anonymous
labels. Keep the model-label key and provider metadata in a separate local
unblinding file; reviewer packets contain full conversations, scenario context
and rubrics but no model IDs, prices or identifying provider headers. Persist
actual permutations for reproducibility and avoid model-order position bias.

OWNER scores whole conversations on anchored dimensions, records critical
failures and preference/ties, and unblinds only after scoring. Report category
scores, paired comparisons and disagreements; do not declare a statistically
proven winner from a tiny pilot. A provider error is a failed/incomplete result,
not an empty conversation scored as successful. No response generation occurs
in Phase 2: offline tests use synthetic transcripts explicitly labeled fixtures.

For a later authorized run record latency per turn (and time-to-first-token
only when measured), token usage including reasoning/cache where billed,
conversation cost, retries, failures, refusals/content constraints and total
wall time. Unknown latency is UNKNOWN, not a fabricated estimate.

## 9. Model shortlist and budget, without API calls

Research Owner's starting labels GPT-6 Astra, latest Sol/Luna, Claude Sonnet
and Gemini Flash against current official provider documentation. Internal
Codex labels are not assumed to be public API IDs. Unavailable/unconfirmed
models remain explicit research entries, not fabricated executable candidates.
For confirmed candidates record API ID, API availability caveats, input/output
pricing, caching prices and conditions, context limit, latency considerations,
policy limitations and Persona/style controls, each with dated primary sources.
ChatGPT/Codex subscription is not an API budget.

Design a small pilot of 16 scenarios (one per category): at least four
user-response rounds for ordinary cases and at least twelve for the long-dialogue
case, baseline plus up to three confirmed candidates. Preserve the same
selected scenarios and seeds for every candidate. Estimate cost from explicit
input growth (Persona + memory + replayed conversation), output caps, optional
reasoning, cache assumptions and retry contingency. Show no-cache and eligible
cache estimates separately. Unsupported baseline billing remains unknown;
distinguish that from the cost of API candidates. The pilot is a selection
screen, not the complete 128-scenario benchmark.

No paid runner is enabled, no provider authentication requested, and no network
inference call made. Paid model comparison requires separate OWNER approval of
model IDs, scope and spend cap, even after Phase 2 acceptance.

## 10. Local reports, Git and Linear

Local research outputs under data/runs/phase2-<run_id>/:
dataset-quality-report; competitor-research; scientific-review; benchmark-v1;
model-shortlist; api-test-budget, plus audit/run manifests and private blind key.
Use Markdown for readable reports and JSON/JSONL for structured evidence.
Only safe code, configs, experiment protocols, tests and reviewed synthetic
fixtures are committed. Allowlist specific golden benchmark files; raw,
processed, runs and private unblinding material remain ignored.

Update only https://linear.app/bestuzhev/document/agata-conversation-lab-31d9a8fed563
with completed-stage summaries, sources, actual status, roadmap and blockers.
Preserve earlier manual sections. Read back after edits. Distinguish completed
research from proposed implementation and explicitly list remaining work.
No additional Linear research documents or cross-chat messages.

## 11. Verification and acceptance

Use TDD for behavior changes and isolated synthetic/minimal regression inputs.
Test read-only access/path rejection, checksum/pointer mismatch, no overwrite,
PII probes, scenario schema/coverage/duplicates, leakage detections, deterministic
blind permutations, label-key separation and partial/error results. Run V1 tests
too. No tests may invoke dataset downloads, paid inference or production.

Final review is independent of implementation and checks against the original
v1.1 task, this specification and the Owner's eight confirmations. Supply
verification evidence, exact committed SHA, changed files, local reproduction
commands, report paths and limitations. Clean-start/end gates must print PASS
for this isolated worktree. The separately dirty G Sales checkout is preserved
and must never be reported clean.

Phase 2 is COMPLETE only after all 180 full-text reviews, five competitor
research sections, scientific review, 128 validated independent scenarios,
blind protocol, verified/qualified shortlist and pilot budget, regression tests,
final review and Linear readback. Production unchanged, no paid calls, no
training and no automatic PR #10 merge are mandatory acceptance facts.

## 12. Agreed stage order and approval boundary

Conversation design was approved by OWNER on 2026-10-04. OWNER explicitly replaced repeat approval gates with internal quality review.
Specification Review and Implementation Plan review are internal checkpoints;
proceed autonomously with inline execution and a fresh final reviewer.
Continue audit/research without restarting completed work. Implementation
follows approved boundaries; no infrastructure expansion or runtime changes.
Separate paid-comparison approval remains mandatory.
