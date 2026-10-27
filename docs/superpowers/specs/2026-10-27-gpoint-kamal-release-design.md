# GPoint Native Kamal Release Design

## Intent

Complete GPoint ME-52 using the architecture in `Правила релизов NEW`: separate Workspace and Python CI, build immutable GHCR images, deploy application containers with stock Kamal 2, verify actual runtime, and keep the first Compose-to-Kamal transition separate from ordinary releases.

## Verified current state

- Read-only GitHub API confirms current `akhripunov/gpoint` `main` is `9ba6488580732897e4398939858243ad3ab34790`.
- Read-only Actions history confirms Native Release `40227519876` and Prepare `40227519840` for that SHA failed; no production Apply succeeded. The current production evidence remains the dated prior Compose/V5 baseline, not a fresh live read.
- Current `main` already contains a four-job Native workflow, pinned Kamal 2.12.0 Core/AGATA/Mini App configs, Caddy routing integration, and guarded first-cutover backup/restore/migration/resource/consumer checks. The workflow also builds a GSales image and combines the first-cutover procedure with the normal release workflow, which does not match NEW's intended ordinary release shape. OWNER has clarified the target edge proxy: Kamal Proxy serves public TLS/HTTP as in MAXIBOT; Caddy is retired from active routing after cutover and retained only for guarded recovery.
- The prior clean local ME-52 continuation `82540f59f63ef97b2ee41cf162f0e56d77b8c5cd` had local boundary tests, pinned Kamal CLI parsing, Docker Proxy integration, and handoff evidence recorded PASS, but it is not published or present in this checkout. Reconstruct only the verified changes needed on a clean current candidate; do not reuse that absent SHA as current source.

## Approaches

1. **Continue ME-52 (selected):** keep its four-job CI/image build and existing Kamal/cutover safeguards, repair the hosted failures, and separate the one-time cutover from the short ordinary release path. Replace active Caddy routing with public Kamal Proxy TLS/routing as OWNER directed. This preserves proven work and avoids a parallel architecture.
2. **Rebuild a new pipeline:** rejected because it would duplicate the existing Native/Kamal implementation and its project-specific safety tests.
3. **Replace V5 or cut over production immediately:** rejected because current backup, migration compatibility, production baseline, and release authorization are not freshly verified. Preserve the existing recovery path until a separately authorized cutover succeeds.

## Proposed behavior

- Keep four jobs in the ordinary workflow: independent Workspace/Playwright and Python/PostgreSQL CI, immutable Build, then a protected Deploy/Verify job. Push runs CI/Build only. Production Deploy is exact-SHA, manually admitted on `main`, protected by the GPoint `production` Environment and non-canceling production concurrency.
- Build only `linux/amd64` Core, AGATA, and Mini App images with exact source SHA, attempt-scoped immutable GHCR refs/digests, SBOM, and provenance. GSales remains stopped and is excluded from ordinary image build/deploy. Eros, Sensitive Detector, and ACL/pilot operations remain outside normal release.
- The separate one-time cutover flow uses stock Kamal 2.12.0 `--skip-push` and the existing GPoint-specific fresh inventory, migration parity, encrypted backup + isolated restore, disk/resource, strict SSH, public Kamal Proxy TLS/routes, Caddy retirement, consumer-pause, recovery, and full Verify gates. It must preserve PostgreSQL, Redis, volumes, Eros/private networks, and all three stopped GSales services. Compose/V5 Apply must not run concurrently against the same app containers.
- The ordinary Kamal flow contains no Compose pause, one-time backup/restore, Caddy mutation, or cutover recovery engine. It first proves the installed topology is already Kamal Proxy, then deploys only the exact immutable images with stock Kamal, verifies component versions and health, and records GitHub Deployment success only after all checks pass.
- Rollback uses stock Kamal only for applications and only after exact previous image and DB/schema compatibility are proven. PostgreSQL is never rolled back by Kamal. Partial independent app deployments are reconciled explicitly.
- Keep existing V5/Compose controllers and recovery code until the first cutover is successfully verified and the owner authorizes their retirement. Update local GPoint docs to distinguish candidate, cutover, ordinary Kamal release, and actual production state.

## Constraints and non-goals

This approval covers local GPoint implementation, review, tests, and local commits only. It does not authorize push, PR, merge, GitHub Actions dispatch/rerun, GHCR publication, production Apply/DB mutation, infrastructure or credential changes, GSales/Eros activation, Linear policy publication, or legacy retirement. Preserve exact-SHA, protected production approval, frozen paths, Eros/AGATA boundaries, backup/restore, recovery, and current V5 operations until cutover acceptance. Do not copy MAXIBOT-only rules or claim production migration from a local PASS.

## Acceptance

- A clean, scoped local candidate contains a short ordinary Kamal release and a distinct guarded first-cutover path, aligned with NEW and GPoint's component topology.
- Tests cover exact SHA/digest identity, no GSales build/start, workflow separation, strict cutover gates, Kamal Proxy TLS/routing, inactive Caddy, GSales stopped state, and truthful Deployment success.
- Candidate receives a scoped local commit and GPoint handoff evidence. Because no publication/hosted run or production operation is authorized here, it remains `LOCAL_PENDING_RELEASE` until those separate gates are satisfied.
