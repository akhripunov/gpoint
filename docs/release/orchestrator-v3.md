# Release Orchestrator V3 — Impact-Based CI (local candidate)

Status: **LOCAL_PENDING_RELEASE**. V2 production is unchanged by this work.
No GitHub push, GitHub Actions or production deployment is authorized by this document.

## Scope implemented

The single canonical GPoint Prepare/Deploy pipeline now computes an immutable-
candidate impact plan from a **verified current production baseline**, using the
canonical gpoint-release-ledger branch. It does not use the last commit or an
unverified source branch.

The selector is deliberately conservative. UI-only classification requires
every changed path relative to production to be inside
`apps/miniapp/src/**`, `apps/miniapp/public/**`, or
`apps/miniapp/index.html`. Any mixed change, including Worker, backend,
contract, shared library, migration, security, deployment tooling, CI
configuration, Dockerfile, lockfile, or an unknown file results in FULL.

UI-only reuse applies to two **source-regression suites only**:
- The independent GSales PostgreSQL regression suite.
- The broad AGATA PostgreSQL source-regression suite.

A UI-only release **still runs** all workspace build, typecheck and unit tests,
release tests, AGATA baseline migration and non-bypass HTTP smoke, disposable
PostgreSQL setup, three exact-SHA Docker builds, GSales migration/health,
Prisma/Alembic artifact migrations, parity, immutable image archive, containerd
roundtrip rehearsal, automatic preflight and owner approval.

The receipt uses `COVERED_UNCHANGED` rather than pretending skipped regression
tests ran. `release-bundle.mjs` refuses generic `SKIPPED`, wrong SHA,
unsupported changed paths and missing/incorrect evidence. Prepare independently
recomputes changed paths before omitting tests. Just before publishing the
immutable bundle it rechecks the canonical production baseline and fails if
the baseline moved.

If the production ledger is missing, not COMPLETE, in recovery, the baseline is
unreachable, or the diff is not provably narrow, V3 uses **FULL** rather than
risking an incorrect skip. A malformed claimed UI plan causes a hard failure.

## Not yet implemented (requires separate verification)

- Content-addressed reuse of unchanged Docker images, since existing canonical
  exact-SHA image IDs and revision labels cannot safely be repointed to old
  images. The current full exact-SHA artifact builds remain mandatory.
- Automatically skipping other components' tests based on a complete dependency
  graph. This initial allowlist is intentionally limited to low-risk Mini App
  source/public changes.
- Measured hosted CI benchmark proving a specific minute reduction; no hosted
  run has been initiated under the LOCAL-FIRST policy.

## Evidence / commands

- `python3 -m unittest discover -s tests -p test_release_impact_v3.py`
- `node --test scripts/release-bundle.test.mjs scripts/release-authorization.test.mjs scripts/release-runner-readiness.test.mjs scripts/release-orchestrator.test.mjs scripts/release-evidence.test.mjs`
- `python3 -m py_compile scripts/release_impact_v3.py scripts/prepare-release.py`
- `git diff --check`

The current candidate's own changes to release tooling force FULL on its first
authorized Prepare. Future genuinely UI-only changes can qualify for reuse
*only after* V3 is in successful production, with a canonical COMPLETE ledger
and immutable baseline. No special manual FAST/FULL owner switch exists.

Rollout must respect exact-SHA approval, CI cost authorization, preflight,
Worker transition restrictions, latest production ledger, rollback/recovery
gates and existing GPoint/MaxiBot separation. MAXIBOT workflows are not changed.
