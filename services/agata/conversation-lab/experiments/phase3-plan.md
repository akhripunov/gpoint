# Phase 3 Implementation Plan

The approved Phase 3 task is the scope. Implement only under `services/agata/conversation-lab/`.

1. **Freeze.** Add pilot config selecting each existing category's `-01` scenario. Canonically hash the complete scenarios, benchmark manifest, synthetic Persona, each initial context/memory fixture and rubric. Verify the result from the frozen IDs, do not edit V1.
2. **Contracts/TDD.** Add typed canonical invocation/result and provider adapter protocol. RED tests first for candidate payload contracts, multi-turn replay and no future fields. Add baseline descriptor only; no Codex subprocess or worker call.
3. **Provider payload adapters.** Implement offline builders and parsers for OpenAI Responses, Anthropic Messages and Gemini generateContent. No networking dependency or credentials. Add deterministic test-only provider producing labeled fixture responses.
4. **Dry-run.** Run 16 scenarios × 4 configs with 72 turns/config and candidate-specific history. Emit ignored new run directory with manifests/results clearly marked `FIXTURE_ONLY`, requests=0, live inference=false.
5. **Budget/security.** Implement token/price arithmetic and pure gate/guard; no sender exists. Fail closed on absent or unknown baseline grant, stale hashes, excess calls/tokens/USD, retries. Retry only explicitly granted retryable failures; no retry refusal/truncation. Never claim candidate ceiling equals full pilot budget while baseline is UNKNOWN.
6. **Ratings/blinding.** Extend current blind functions minimally: strict imported-result validation, locked ratings hash, new review cycle for correction, exact mapping/hash unblind only after lock. Test seed and mapping never enter public packet.
7. **Verification/review.** Existing+new full suite, offline dry-run and socket/process sentinels, verify no touched worker/Core/G Sales/Production and no network/paid inference. Fresh whole-branch review versus this spec, fix P1/P2 using RED/GREEN.
8. **Handoff.** Record baseline as-of, frozen IDs/hashes, budget and blockers only in existing Linear doc. Commit/push Phase3 branch from exact PR12 head; create stacked Draft PR to PR12 branch; read back Linear and GitHub exact SHAs. Run clean:end.

No model calls, model-list probing, API key access, Codex CLI jobs, worker interactions, production reads involving personal records/secrets, or lifecycle changes are allowed.
