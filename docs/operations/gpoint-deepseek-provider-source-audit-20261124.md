# DeepSeek provider source audit — 2026-11-24

OWNER requested a switch from the unavailable Mac Product Worker to the previously used DeepSeek API. Before changing any runtime settings, identify whether an existing DeepSeek credential remains on the current Layer7 host or in the protected GitHub environment.

One-time read-only check using the existing protected production runner and pinned SSH identity. Examine only named GPoint Eros environment files and their historical copies, and existing Eros engine containers. Report presence, source paths and HTTP status of a bounded GET /v1/models request, never credentials or response bodies. Check existing Docker networks for the internal flag. No credential copy, changes to provider configuration, container restart, DB mutation or message generation is authorized by this audit. The newly added exact file is the sole trigger; future pushes skip the audit. Ordinary protected release gates remain unchanged.
