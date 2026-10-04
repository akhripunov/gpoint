# Future model pilot — authorization required

Research snapshot: 2026-10-04. Public API identifiers verified against official documentation; account availability is NOT_VERIFIED. Do not authenticate or send requests during Phase 2. Existing AGATA baseline provider/model/cost is UNKNOWN until a separately authorized read-only configuration snapshot; it cannot be assumed free or substituted silently.

| Public model ID | Input / output USD per 1M tokens | Candidate role |
|---|---|---|
| gpt-6.1-sol | 2 / 10 | Proposed OpenAI slot |
| claude-sonnet-5-5 | 2 / 10 | Proposed Anthropic slot |
| gemini-3.8-flash | 0.75 / 3.75 | Proposed Google slot |
| gpt-6-astra | 10 / 50 | Optional replacement for OpenAI slot |
| gpt-6-luna | 0.10 / 0.50 | Optional budget replacement for OpenAI slot |

Sources: [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [OpenAI models](https://developers.openai.com/api/docs/models), [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Google pricing](https://ai.google.dev/gemini-api/docs/pricing). Pricing and identifiers must be rechecked before spending. Cache write/read, reasoning billing and availability differ by provider; no latency measurement was performed. Vendor claims are not AGATA measurements. Detailed dated evidence remains in the existing Linear document and ignored local research reports.

## Pilot budget, not permission to spend

16 scenarios: one per category, 15 × 4 rounds plus one × 12 rounds = 72 replies/model. Assumed static context S=4096, user U=256, visible assistant A=512, billed output/reasoning O=1024 tokens/reply. Full-history input at round r: S+rU+(r-1)A. Across the pilot: 433152 input and 73728 output tokens/model.

Expected zero-cache candidate costs: Sol $1.603584; Sonnet $1.603584; Flash $0.601344; three candidates $3.808512, excluding the UNKNOWN baseline. At a 2048 billed-output cap with corresponding full replay growth: 672768 input and 147456 output tokens/model, candidate total $6.697728 before cache writes/retries. Conservative cache-write assumptions and 20% retry reserve give $8.844595; proposed candidate-only ceiling $10. Baseline and any larger model replacement require a separate priced allowance. Cache savings are conditional, not a guaranteed cost. Reasoning tokens are billed and must count against limits; do not silently use uncapped defaults.

Before launch obtain OWNER approval for exact models, baseline, token caps, scenario IDs, maximum calls, aggregate USD ceiling including baseline/retries and data policy. Abort at the ceiling; never add candidates automatically. Use synthetic data only. Match the blind protocol and unchanged Core; respect provider content rules, without jailbreaks or special prompt tuning. The full 128-scenario comparison is a separate budget decision. No paid operation was run in this phase.
