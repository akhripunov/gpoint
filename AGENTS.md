# GPoint release rules

## Source of truth

- GitHub `origin/main` is the only durable source of application source.
- Production changes flow `Mac/repository → exact Git SHA → GitHub → immutable artifacts → VPS`.
- Never copy source edits from the VPS back into GitHub.
- Never release a dirty checkout, a branch name, `HEAD` without SHA verification, `latest`, or an unverified mutable image tag.
- Production secrets and environment files stay outside Git.

## Release gates

Every release must record the current production SHA and database migration head before mutation, then verify the exact candidate SHA on GitHub, build from an isolated clean checkout, prove API/Worker/Bot/Mini App artifact parity, render effective production configuration, create a GPoint-only backup when migrations are pending, migrate with `prisma migrate deploy`, switch runtime, run health/smoke checks, and persist a complete manifest.

The canonical entry point is `pnpm release:gpoint --dry-run --candidate <40-char-sha>`. Apply mode is deliberately fail-closed and requires an explicit operator confirmation. A release lock covers migration, runtime apply, and production-ref finalization; a stale lock is never silently removed.

## VPS policy

The VPS is a runtime and artifact target, not a development workspace. Application fixes must be committed and pushed before deployment. MaxiBot files, containers, database, domain, and release state are outside GPoint scope and must not be touched.

## GPoint UI/design skill

- For GPoint UI/design, Pen, visual-reference, Golden, and design-to-React tasks, use the repo-local `gpoint-design` skill.
