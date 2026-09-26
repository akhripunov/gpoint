# Source provenance

Core selection was copied from the existing local AGATA worktree as it stood at task start; the source repository was not modified.

- Source: `/Users/akhripuniov/Downloads/Codex/AGATA`
- Branch: `feature/locale-pack-foundation`
- HEAD: `0173a8e852e0eddc51d8831d7de12da05ffcb190`
- Dirty status captured before copy: 173 entries
- SHA-256 of the complete captured status listing: `b9a6194a835e15d46db868f4c04ff79d740a6b24245949c868a70af22a56db24`
- `.env`, credentials, keys, old PostgreSQL volumes/data, staging seed, and deployment files were excluded.

The transferred blocks are the selected dialogue/response, character/personality, message encryption and ingestion, compliance gates, retrieval and memory graph, explicit/shadow memory and promotion, summary/embedding worker, outbox, locale contract/resolution, and the minimum SQLAlchemy model closure. The original `0001`–`0024` migration history is not used; `migrations/versions/0001_agata_core_baseline.py` is a newly authored schema-scoped baseline.
