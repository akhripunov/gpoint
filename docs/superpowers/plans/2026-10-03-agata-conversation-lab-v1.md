# AGATA Conversation Lab — Starter Collection

Implement a local-only, resumable collection and normalization CLI under `services/agata/conversation-lab/`. It handles exactly six approved starter corpora and records RLDD and Toloka as deferred sources. Originals, processed outputs, and run reports stay under the canonical local `data/` path and are ignored by Git; only code, manifests, docs, and synthetic fixtures are versioned.

## Steps

1. Add the six-source starter/deferred manifest, local data ignore rules, CLI, normalizers, tests, and operating documentation.
2. Run meaningful unit tests and inspect source metadata/licensing; fail closed for missing licenses, unavailable sources, insufficient disk, or unsafe archive paths.
3. Check available space, then download only the six starter sources sequentially with resume, source-revision pinning, SHA256 and safe extraction.
4. Normalize available formats incrementally without external code execution; report unsupported formats as explicit blockers.
5. Verify each original, produce per-corpus metrics and a paraphrased diversity review of at least 20 deterministic examples where processing succeeded.
6. Review the diff, commit/push source only, leave all local data out of Git, and run the repository clean-end gate. Production and fine-tuning stay untouched.
