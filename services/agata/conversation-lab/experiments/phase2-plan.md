# AGATA Conversation Lab Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Tasks use TDD; OWNER authorized internal review and autonomous execution.

**Goal:** Complete data review and primary-source research; deliver 128 safe multi-turn scenarios and offline validation/blind evaluation.
**Architecture:** Expand the existing Python lab with focused offline modules. Canonical data is read-only; local reports remain gitignored. No runtime/Core changes.
**Tech Stack:** Python standard library, unittest, JSON/JSONL, Markdown, Git and Linear MCP.
**Spec:** services/agata/conversation-lab/experiments/phase2-spec.md

## Global Constraints

- Exactly six existing corpora, immutable canonical input; no downloads or reprocessing.
- Exactly 128 synthetic scenarios, eight per each of 16 categories; ordinary >=4 rounds, long >=12.
- Complete 180 full-text reviews, preserving the 30 completed Russian reviews.
- No Production, G Sales, MAXIBOT, paid API, training or PR #10 merge.
- Single existing Linear AGATA Conversation Lab document; reports under canonical data/runs/phase2-<run_id>/.
- Main checkout belongs to G Sales. New lab files are hidden by shared .git/info/exclude; stage specific approved paths with git add -f, never edit the shared exclusion.

## Review Focus

1. Symlink/path escape from manifest or output path: reject before read/write (Task 1).
2. Truncated/mismatched pointer reads: fail closed, never count metadata as full review (Task 1/2).
3. Near-duplicate fixtures differing only names/punctuation: normalize and require scenario review (Task 3/4).
4. Evaluator expectations or model metadata leaked into prompts/blind packets: explicit field projection (Task 5).
5. Unknown provider/cost and partial failed outputs: explicit unknown/error status, no winner or fabricated success (Task 5/6).

### Task 1: Read-only data boundary

Files: evaluators/corpus.py; tests/test_corpus.py; README.md.
Produces: verify_sources(data_root: Path) -> dict; read_pointer_dialogues(data_root: Path, source_id: str) -> list[dict]; redact(text: str) -> str; write_report(data_root: Path, run_id: str, filename: str, payload: dict) -> Path.
- [x] Write tests for checksum mismatch, raw/processed path escape, pointer identity/count mismatch, original byte preservation, no output overwrite, currency/date/ID versus contact redaction.
- [x] Run unittest for test_corpus: expected FAIL because module/functions absent.
- [x] Implement streaming/hash reads and strictly confined new output files; no downloader invocation.
- [x] Run test_corpus plus existing V1 tests: expected PASS.
- [x] Commit the bounded task; record RED/GREEN evidence in ledger.

### Task 2: Complete 180-dialogue quality review and research

Files: local gitignored runs reports; no source excerpts committed.
Consumes: Task1 pointer reader, verified manifest hashes; original 30 ratings preserved.
Produces: complete dataset-quality-report.json/md, competitor-research.md, scientific-review.md.
- [x] Read remaining five source selections fully in bounded batches; reject truncated inspection.
- [x] Assign all required per-example dimensions and ratings; validate 30 unique matching IDs per source and 180 total.
- [x] Review source aggregates, sampling limits and seven transferable patterns; no copied text.
- [x] Verify competitor/science claims against dated primary sources, retain UNKNOWN rather than speculation.
- [x] Record reviewed counts/provenance and roadmap in existing Linear only; read back.

### Task 3: Scenario schema and synthetic benchmark

Files: configs/benchmark-v1.json; data/golden/synthetic/agata-benchmark-v1.jsonl; data/golden/synthetic/manifest-v1.json; evaluators/benchmark.py; tests/test_benchmark.py; .gitignore.
Produces: load_scenarios(path: Path) -> list[dict]; validate_scenarios(scenarios: list[dict]) -> dict; prompt_fixture(scenario: dict) -> dict.
Consumes: Spec's ten required fields and Task2 abstract research patterns, never source dialogue text.
- [x] Write tests for all fields/types, roles/order, 128 count, eight per category, unique IDs/content, four/twelve rounds, temporal memory references and evaluator-field omission.
- [x] Run tests: expected FAIL for missing module/fixtures.
- [x] Author 128 varied original scenarios with synthetic facts/persona; implement strict schema/coverage checks, hashes and prompt projection.
- [x] Add narrow golden file allowlist. No original examples/versioned reports.
- [x] Run benchmark and corpus/V1 tests: expected PASS; manually review semantic independence of all eight/category.
- [x] Commit and ledger.

### Task 4: Source leakage and duplicate checks

Files: evaluators/leakage.py; tests/test_leakage.py.
Produces: check_leakage(scenarios: list[dict], corpus_paths: list[Path]) -> dict.
Consumes: Task3 valid scenarios and Task1 validated processed paths.
- [x] Write tests with controlled copied turns, copied dialogue spans, whitespace/case changes, benign short phrases, near-copy alerts and future few-shot collision.
- [x] Run test_leakage: expected FAIL for absent functions.
- [x] Implement bounded-memory streaming exact normalized turn/span hashes and lexical overlap alerts; return source IDs/hashes only, no passages.
- [x] Run tests and full real six-source audit locally; exact copied substantive turns/spans must be zero, heuristics reviewed explicitly.
- [x] Commit and ledger; document semantic-leakage detection limits.

### Task 5: Blind evaluation, rubrics and CLI

Files: evaluators/blind.py; evaluators/cli.py; configs/rubric-v1.json; tests/test_blind.py; tests/test_cli.py; experiments/blind-evaluation.md.
Produces: make_blind_packets(scenarios: list[dict], results: list[dict], seed: int) -> tuple[dict, dict]; offline CLI verify/review/benchmark/leakage/blind.
Consumes: stable Task3 scenario hashes, Task4 leakage result; synthetic transcripts only for tests.
- [x] Write tests for deterministic same-seed mappings, balanced/randomized positions, model-key separation, errors/partial transcripts, shared scenario/persona/memory hashes, invalid results and no network calls.
- [x] Run tests: expected FAIL for missing offline evaluator.
- [x] Implement offline result import and reviewer packets using projected safe fields; private unblinding metadata in separate ignored file; no model runner.
- [x] Define anchored manual quality dimensions/critical failures; do not equate regex checks with conversational quality.
- [x] Run complete lab suite and CLI synthetic smoke: expected PASS; commit and ledger.

### Task 6: Official shortlist and pilot budget

Files: experiments/model-comparison.md; local model-shortlist.md and api-test-budget.md.
Consumes: confirmed dated official model pricing and Task5 methodology.
- [x] Verify starting model labels; distinguish internal aliases from actual public API IDs and account availability.
- [x] Estimate 16-scenario pilot (15x4 + 1x12 = 72 response requests/model), replay-growing context, output/reasoning caps, no-cache/cache/retry assumptions and unknown baseline cost.
- [x] Independently recompute arithmetic; no auth or paid calls.
- [x] Document frozen same-Core protocol, privacy/content limits and budget approval requirement; commit safe protocol only.

### Task 7: Independent review and handoff

Files: README.md; existing Linear document; local verification report.
- [x] Run full lab suite, all 128-scenario checks, real-corpus integrity/leakage audit and blind smoke; capture exact outputs.
- [ ] Dispatch fresh whole-branch review against original TZ/spec/plan, fix important findings through RED/GREEN, record limitations.
- [ ] Verify git tracked diff is lab-only, safe synthetic content, no secrets/source text. Commit and push feature branch, no merge/production dispatch.
- [ ] Update all completed-stage summaries/roadmap in existing Linear and independently read back.
- [ ] Run clean:end PASS for isolated worktree; report exact SHA, files, reproduction commands and limitations.
