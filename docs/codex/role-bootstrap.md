# GPoint Codex role initialization and verification

## Initialize a role

The root `AGENTS.md` is role-neutral. A chat has no specialized role until the user explicitly initializes exactly one role with one of these commands:

- `ROLE=DEV` — ordinary GPoint development.
- `ROLE=LAB` — AGATA Conversation Lab.
- `ROLE=DEVOPS` — release preparation and operations.

Send the selected command once at the beginning of a new GPoint Codex chat. The agent then reads only the matching `.codex/roles/` profile and its required current Linear context, and keeps that role for the chat. Without a selected role, ordinary research and auxiliary tasks remain allowed; role-specific processes and documents must not load automatically. A chat title alone never selects a role.

Do not combine profiles, histories, or private task context across roles. If specialization changes, start a new chat with the other role. Reuse the current chat only if the OWNER explicitly directs a role change. A role command selects the work context; it does not grant authority beyond the task.

## Existing chats

An existing chat does not inherit a role from its title or from these files. For a new specialized task, initialize a new GPoint Codex chat with the exact selected command. Do not send role commands to similarly named ChatGPT conversations.

## Safe activation checks

For each role, create a fresh Codex chat attached to the GPoint project and initialize it with the exact role command. While validating unmerged changes, use the implementation branch.

1. Ask it to report the selected role, profile, required Linear documents actually read, and displayed model/reasoning. Verify it loaded only the selected profile/context.
2. Give a small read-only task with a recoverable ambiguity and ask it to resolve the ambiguity from repository evidence without asking OWNER unless evidence is genuinely insufficient. If it delegates, ask it to return the subagent's question to the main agent and resolve it there.
3. Ask the agent to describe the release, Lab, database, payment, and security gates that still apply. Do not run production, live inference, or payment actions as activation tests.
4. Test a correction loop using a disposable documentation-only fixture with an intentional inconsistency; require diagnosis, an in-scope correction, and a rerun of the check. Do not modify real project behavior.

Record each result as PASS, NOT_VERIFIED, or BLOCKED. Reading a profile proves instruction loading, not end-to-end autonomy or model selection.

## Project defaults and model limitations

This trusted project configures `gpt-6-luna`, reasoning `medium`, and enables subagents with Luna/Medium defaults. Explicit per-chat or per-subagent overrides may take precedence. Existing chats may retain another selection; verify displayed controls before claiming otherwise. Do not change global Codex settings or an existing chat's model as part of role initialization.

When delegation is useful, explicitly request `gpt-6-luna` and `medium` if the subagent tool exposes those controls. If unavailable, record the actual setting as NOT_VERIFIED. Profiles are repository instructions; they do not create cross-chat messaging, watchers, bridges, or automatic wake-up behavior.
