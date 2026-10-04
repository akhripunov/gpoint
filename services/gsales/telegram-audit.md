# Telegram reference audit — 2026-10-04

Reference: [bchewy/telegram-agent-plugin](https://github.com/bchewy/telegram-agent-plugin/tree/ad8d98370a13a35c213e6f221ef7a74a1ea123d8),
exact audited SHA `ad8d98370a13a35c213e6f221ef7a74a1ea123d8`.
Read via GitHub API: LICENSE, pyproject, auth, client, session_store and messages.

| Area | Finding | G Sales decision |
|---|---|---|
| License | MIT, copyright Brian Chew 2026 | No plugin source copied; use own limited connector |
| Dependencies | Python>=3.11; Telethon>=1.43; cryptography, keyring, MCP | Python3.12, pinned/tested Telethon1.42, cryptography45; no MCP/cache/group-management dependency |
| Auth | User API ID/hash, phone/code, optional 2FA; StringSession | Explicit hidden interactive setup; no automatic login or bot-identity substitution |
| Session storage | Keyring default account or encrypted file; environment fallback | Per-account opaque UUID references, encrypted files outside repository, 0700 directory/0600 files; no raw-session env fallback |
| Multiple accounts | One global client and default keyring account | Metadata/vault references prepared per account; MVP deliberately one enabled account |
| FloodWait | Telethon default sleeping plus wrapper can sleep and retry once | Zero automatic request retries/flood sleep; disable account and record deadline |
| Sending | High-level send_message generates transport identity per invocation | Exact deterministic random_id on one claimed attempt; no automatic retry after uncertain delivery |
| Scope | Broad dialog/history/export/search/group tools | Managed private dialog new updates and bounded targeted reconciliation only |

The upstream implementation is a useful technical reference, not a safe drop-in
CRM/outreach workflow. Its global history tools and retry semantics are outside
this owner-approved scope. Mock transport tests cover sender/recipient identity,
random_id response mapping and disabled retries; live authorization remains NOT RUN.
