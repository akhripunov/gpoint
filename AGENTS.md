# GPoint Project Instructions

## Canonical Sources

- GitHub `origin/main` is the durable source of application code and `AGENTS.md` instructions; the Linear AGENTS mirror is a synchronized copy, never an independent release policy. Verify repository state and exact SHA before release work.
- OWNER policy and frozen Release Orchestrator V2: [Правила релиза GPoint](https://linear.app/bestuzhev/document/pravila-reliza-gpoint-183dba239394), current canonical block first. Release history: [Журнал релизов](https://linear.app/bestuzhev/document/zhurnal-relizov-640efa60dacf); it is not fresh runtime evidence.
- Technical authorization: [docs/RELEASE_AUTHORIZATION.md](docs/RELEASE_AUTHORIZATION.md). Release procedures and executable behavior: [docs/release/automation.md](docs/release/automation.md), workflows, scripts, and tests. Keep detailed procedures there.
- For design work, use the repo-local [gpoint-design skill](.agents/skills/gpoint-design/SKILL.md).
- For editable Pen work, use the canonical connected Pen MCP/CLI only; Computer Use/GUI may inspect but must not modify canvas, files, artboards, or properties. Validate render/read-back and preserve OWNER visual approval.

## Safety & Release Invariants

- Use only the canonical release flow, exact verified SHA, applicable `EXACT_SHA` or `TASK_SCOPED_AUTO` authorization, and all fail-closed gates. Successful main Prepare automatically starts controller read-only preflight; only its successful immutable `READY_FOR_OWNER` evidence plus applicable OWNER authorization permits production apply. A push alone is never apply authorization.
- Never release a dirty checkout, branch name, unverified `HEAD`, mutable image tag, or artifact without exact immutable provenance. Never bypass approval, locks, backup/migration, compatibility, identity/parity, recovery, health, or other canonical gates.
- Before an Actions dispatch, check for an existing run and do not create a duplicate. Do not cancel/rerun an active release without authority. Use only the supported CLI/API with exact workflow/SHA and supported status/log interfaces. Never use Computer Use or browser UI to manage Actions or production; if the supported path is unavailable, stop and report it.
- The VPS is a runtime/artifact target, not a source-editing workspace. Publish source fixes through GitHub first; never copy VPS source edits back. Under GPoint work, never touch MAXIBOT files, containers, database, domain, or release state.
- Release policies are strictly repository-scoped. GPoint and MAXIBOT are independent projects with separate canonical Linear documents, authorization records, workflows, runners, migration systems, artifacts, release ledgers, and production environments. Never apply one project's release procedure or authorization to the other. Shared OWNER restrictions apply to both only where explicitly stated. For cross-project work, resolve and verify each project's policy independently.
- Keep production credentials, secrets, private user data, and environment files out of Git, Linear, and logs. Never assign the trusted GPoint production runner to untrusted PR work.
- Record server deployment, Mac Worker readiness, and real user/end-to-end acceptance separately; heartbeat is not a user response. Never create synthetic production Messages jobs. Preserve Worker compatibility gates.

## Autonomous Execution

Treat a complete OWNER-approved task or handoff as the contract. Check current repository state; plan, implement, test, review, correct, and document through acceptance. Resolve ordinary technical questions and reversible choices within scope. Do not reopen settled Compass/design decisions or request repeated approval for authorized work. Use applicable Superpowers without weakening gates.

Delegate bounded work when useful, request Luna/Medium when supported, integrate results, and remain accountable. Diagnose and fix ordinary failures. Stop for a material unresolved decision, scope/authority boundary, required protected-operation approval, conflicting canonical rule, or objective blocker.

Do not transfer secrets, personal data, sealed inputs, or private role-chat context except the exact minimum necessary and authorized. Updates do not block continued work. Autonomy grants no additional production, database, payment, security, or release authority.

## Role Selection

The root is role-neutral. Initialize exactly one role with `ROLE=DEV`, `ROLE=LAB`, or `ROLE=DEVOPS`; then read only its profile and required Linear context: [DEV](.codex/roles/dev.md), [LAB](.codex/roles/conversation-lab.md), or [DEVOPS](.codex/roles/devops.md). Without a role, ordinary research/auxiliary work is allowed, but no specialized workflow or role context loads. Titles do not select roles. Keep roles and histories separate; recommend a new chat when specialization changes unless OWNER directs otherwise. See [role initialization](docs/codex/role-bootstrap.md).

## Instruction Changes — Direct GitHub Publication (OWNER decision 2026-10-08)

`origin/main` is canonical for `AGENTS.md`, Codex role profiles and bootstrap instructions; relevant Linear documents are synchronized mirrors or distinct project policy sources, not a substitute for Git history. For an already OWNER-approved instruction task, DEV may commit the scoped changes directly to `main` without a mandatory PR, review/merge ritual, or additional per-commit OWNER approval, provided repository permissions and branch protection actually allow it. PRs remain optional when useful or technically required, never an extra blanket approval gate for ordinary instruction edits.

Before writing, read the current `main` file SHA and applicable rules, preserve concurrent changes, limit the diff to approved instructions, and run relevant syntax, policy, security, and regression checks. Preserve Git history, exact commit/blob SHA, attribution, independent QA when a canonical gate requires it, and fail-closed conflict detection. Do not bypass protected branches, repository restrictions, CODEOWNERS or required checks. If direct `main` publication is blocked, stop, report the actual blocker and propose a compliant path; do not force-push or secretly bypass the restriction.

Ordinary editorial, routing, and consistency updates within an approved task require no separate OWNER approval for each commit. A material change to agent powers/autonomy, security, OWNER-only actions, approval conditions, release infrastructure, or protected policy still requires **explicit, scope-specific OWNER authorization before publication**; this instruction-publication decision grants no such new powers. Retain all release authorization, QA, frozen-path, privacy, database, and production gates.

After publication, read back exact `origin/main` SHA and the changed instructions, synchronize the corresponding Linear mirror(s), then safely refresh the authorized Mac installation without changing, resetting, stashing, or checking out another active Codex chat's branch/worktree. Preserve existing conversations, including `ROLE=LAB`; verify actual role/document loading, subagent behavior, and active model independently, reporting `NOT_VERIFIED` for absent runtime evidence. A `main` instruction push may trigger automatic Prepare/read-only preflight; these are not production Deploy authorization. **Never perform production Deploy only to publish instructions.**

## Task Completion

Run `pnpm repo:clean:start` before work and `pnpm repo:clean:end` before completion. Leave the worktree clean with committed/published changes, or report all remaining files and the blocker. New chats use current project instructions.
