# GPoint Project Instructions

## Canonical Sources

- GitHub `origin/main` is the durable source of application code and `AGENTS.md` instructions; the Linear AGENTS mirror is a synchronized copy, never an independent release policy. Verify repository state and exact SHA before release work.
- OWNER policy and current Release Orchestrator V5: [Правила релиза GPoint](https://linear.app/bestuzhev/document/pravila-relizov-fac48f62832a), current canonical block first. Release history: [Журнал релизов](https://linear.app/bestuzhev/document/zhurnal-relizov-640efa60dacf); it is not fresh runtime evidence.
- Technical authorization: [docs/RELEASE_AUTHORIZATION.md](docs/RELEASE_AUTHORIZATION.md). Release procedures and executable behavior: [docs/release/automation.md](docs/release/automation.md), workflows, scripts, and tests. Keep detailed procedures there.
- **Current artifact and production-state model (ME-42):** GHCR is the only store for build artifacts. Prepare publishes a small OCI receipt artifact bound to exact source SHA, trusted workflow run/attempt and successful publisher-step log digest, tested gates, image identities, and migration/static metadata. The trust evidence is run-bound GitHub metadata plus digest-addressed OCI readback, not a GitHub Artifact Attestation. Runtime images are pulled by immutable digest. Approved V3/V4 UI-only scope may publish a separate Mini App OCI artifact and must not build all runtime images when genuinely independent. GitHub Deployments track attempt status/provenance; read-only VPS and database facts are authoritative for installed runtime, image/config identities, health, migration heads, and data. GitHub Releases and Actions artifact storage are not operational transports. Do not use a release ledger branch, production tags/catalog, production-state file, or Recovery Finalization. See the current model at the top of `docs/release/automation.md`; older procedures below it are historical only.
- For design work, use the repo-local [gpoint-design skill](.agents/skills/gpoint-design/SKILL.md).
- For editable Pen work, use the canonical connected Pen MCP/CLI only; Computer Use/GUI may inspect but must not modify canvas, files, artboards, or properties. Validate render/read-back and preserve OWNER visual approval.

## Обязательный preflight GitHub/GHCR

При любой ошибке GitHub или GHCR сначала прочитать актуальный стандарт [«GitHub доступ» в Linear](https://linear.app/bestuzhev/document/github-dostup-7970682d1b80), затем проверить штатные доступные способы подключения до объявления BLOCKED. Уточнить конкретную операцию и OWNER scope; execution context (Codex sandbox, разрешённый host Mac, GitHub connector/API или GitHub-hosted runner); DNS/HTTPS доступность именно из этого context; нужные CLI и credential helpers; авторизацию и operation-specific permissions. Секреты и значения token/header не выводить.

Разделять причины как `NETWORK`, `TOOLING`, `AUTH`, `PERMISSION`, `OWNER_AUTHORIZATION`. Сбой одного способа — например, недействительный `gh` login — не доказывает отказ других штатных способов. DNS внутри sandbox не доказывает глобальную недоступность: ошибка разрешения имени означает только `NETWORK` для проверенного context, пока допустимые контексты/способы подключения не диагностированы. HTTP 401/405 от GHCR `/v2/` подтверждает достижимость сервера, но сам по себе не подтверждает package read/write. Успешная host Mac проверка не доказывает сетевую доступность sandbox или permissions workflow `GITHUB_TOKEN`.

Проверять только разрешённые контексты и безопасные read-only операции; не обходить sandbox/host approval, не подменять пользователя или token, не запускать дополнительный workflow и не расширять OWNER scope. Для уже разрешённой операции продолжать после восстановления конкретного доступа; если блокируется только один маршрут, останавливать только его. В отчёте указывать контекст, команду/результат без секретов, пять классификаций и одно минимальное действие OWNER, если оно действительно требуется.

## Safety & Release Invariants

- Use only the canonical release flow, exact verified SHA, applicable `EXACT_SHA` or `TASK_SCOPED_AUTO` authorization, and all fail-closed gates. A push or Prepare alone is never production authorization.
- Never release a dirty checkout, branch name, unverified `HEAD`, mutable image tag, or artifact without exact immutable provenance. Resolve and verify the OCI artifact digest, receipt and provenance before Apply; pin images by digest. GHCR outage, incomplete publication, tampering, or digest/label/config mismatch blocks before Apply. Never fall back to arbitrary VPS images or build replacement images on the VPS. Preserve approved rollback images and all approval, locks, backup/migration, compatibility, identity/parity, recovery, health, and other canonical gates.
- Before an Actions dispatch, check for an existing run and do not create a duplicate. Do not cancel/rerun an active release without authority. Use only the supported CLI/API with exact workflow/SHA and supported status/log interfaces. Never use Computer Use or browser UI to manage Actions or production; if the supported path is unavailable, stop and report it.
- The VPS is a runtime target and read-only source of installed-state evidence, not a source-editing workspace or build-artifact store. Never copy VPS source edits back. Under GPoint work, never touch MAXIBOT files, containers, database, domain, or release state.
- **LOCAL-FIRST — OWNER policy:** ordinary development, tests, local commits, instruction changes, and handoff preparation stay local. Automatic push, PR publication, merge, Actions dispatch/rerun, and other publication that can consume Actions minutes are prohibited without explicit OWNER authorization. Keep completed unpublished work `LOCAL_PENDING_RELEASE`. Preserve current Release Orchestrator V5, security checks, and OWNER approvals.
- Release policies are strictly repository-scoped. GPoint and MAXIBOT are independent projects with separate canonical Linear documents, authorization records, workflows, runners, migration systems, artifacts, and production environments. Never apply one project's release procedure or authorization to the other. Shared OWNER restrictions apply to both only where explicitly stated. For cross-project work, resolve and verify each project's policy independently.
- Keep production credentials, secrets, private user data, and environment files out of Git, Linear, and logs. Never assign the trusted GPoint production runner to untrusted PR work.
- Record server deployment, Mac Worker readiness, and real user/end-to-end acceptance separately; heartbeat is not a user response. Never create synthetic production Messages jobs. Preserve Worker compatibility gates.

## Autonomous Execution

Treat a complete OWNER-approved task or handoff as the contract. Check current repository state; plan, implement, test, review, correct, and document through acceptance. Resolve ordinary technical questions and reversible choices within scope. Do not reopen settled Compass/design decisions or request repeated approval for authorized work. Use applicable Superpowers without weakening gates.

Delegate bounded work when useful, request Luna/Medium when supported, integrate results, and remain accountable. Diagnose and fix ordinary failures. Stop for a material unresolved decision, scope/authority boundary, required protected-operation approval, conflicting canonical rule, or objective blocker.

Do not transfer secrets, personal data, sealed inputs, or private role-chat context except the exact minimum necessary and authorized. Updates do not block continued work. Autonomy grants no additional production, database, payment, security, or release authority.

## Role Selection

The root is role-neutral. Initialize exactly one role with `ROLE=DEV`, `ROLE=LAB`, or `ROLE=DEVOPS`; then read only its profile and required Linear context: [DEV](.codex/roles/dev.md), [LAB](.codex/roles/conversation-lab.md), or [DEVOPS](.codex/roles/devops.md). Without a role, ordinary research/auxiliary work is allowed, but no specialized workflow or role context loads. Titles do not select roles. Keep roles and histories separate; recommend a new chat when specialization changes unless OWNER directs otherwise. See [role initialization](docs/codex/role-bootstrap.md).

## Instruction Changes — LOCAL-FIRST

Keep approved instruction changes local with the code changes they govern. Publish
them only during an explicitly authorized release preparation, before Prepare,
or under a separate explicit OWNER authorization covering publication and any
possible CI cost. Never push, open/publish a PR, merge, or dispatch/rerun Actions
under this section alone. Preserve concurrent work, exact SHA evidence, branch
protection, CODEOWNERS, required checks, release authorization and production
gates. Linear mirrors are updated only through an authorized release/reporting
flow; a local instruction edit never implies publication or Deploy permission.

## Task Completion

Run `pnpm repo:clean:start` before work and `pnpm repo:clean:end` before completion. Leave the task worktree clean with a scoped local commit, or explicitly report `LOCAL_PENDING_RELEASE` and every remaining file. Never publish as a cleanup step. New chats use current project instructions.
