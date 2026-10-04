# Blind Evaluation V1 — offline protocol

This phase delivers fixtures, validation and imported-result packaging, not a paid model runner or an assertion of improved AGATA quality.

## Frozen inputs

Use all 128 versioned scenarios, their manifest hashes and the synthetic evaluation Persona from configs/benchmark-v1.json. This Persona is not asserted to be the production Persona. Every model receives the identical Persona, memory fixture and explicit model_context/time fields for the same scenario. The context.setup story summary, expected_behavior, failure_patterns, evaluation_notes and all future turns are reviewer-only. prompt_fixture projects only the reached round and previous user turns; a future authorized Core adapter must supply the actual preceding assistant replies, not empty slots, and resolve persona_ref to the frozen Persona. Do not turn a story synopsis into information the model already knows.

Keep AGATA Core unchanged for future comparisons: same routing/context limits/memory wrapper, no model-specific prompt improvements. Before paid execution record Core revision, resolved Persona hash, memory/context hashes, actual request payload hashes, provider/model version, decoding parameters and output cap. Verify shared input equality per scenario; assistant history naturally differs between candidates. No adapter or live execution is enabled here. Account access and production baseline model remain NOT_VERIFIED.

## Reproducible packaging

Import a JSON list: model_id, scenario_id, status (success/partial/error/refusal), turns with alternating role/text, optionally usage/latency/cost. A success must include every specified turn and exactly match all user prompts. Duplicate or missing pairs fail closed. No fake success for missing or failed results.

Run evaluators/cli.py blind --results /absolute/local/results.json --seed 20261004 --run-id phase2-<unique>. No network call occurs. Store original imported results privately and preserve their hashes. The reviewer-packet.json omits provider/usage/cost headers and uses candidate-N labels independently shuffled within each scenario; the separate unblinding-private.json maps the pair (scenario_id, label), never a global candidate identity. Preserve the seed for reproducibility. Random order does not guarantee perfectly balanced positions.

Only distribute the public packet, never its neighbouring private key or raw results. Inspect all assistant prose for model self-identification before OWNER review: exact model ID redaction is automatic, arbitrary aliases/self-identification are not. Any manual masking must be logged privately against the immutable original result hash. A rerun needs a new run-id; existing reports are never overwritten.

## Ratings and interpretation

Use configs/rubric-v1.json on full conversations, not isolated attractive replies. OWNER scores anchored 1–5 dimensions and marks critical failures, N/A and pairwise preference. Keep category-level scores, completed denominators and error/refusal counts separate. Freeze ratings before unblinding. Automated structure, lexical leakage and duplicate checks cannot establish conversational quality. Report uncertainty and individual differences; do not extrapolate a 16-scenario pilot to all users.

The test smoke uses synthetic fixture replies explicitly labelled as test data. COMPLETE_IMPORTED_RESULTS means structurally complete imported transcripts, not successful inference, OWNER acceptance or a quality winner. Production, paid calls, training and source-corpus prompt reuse remain prohibited.
