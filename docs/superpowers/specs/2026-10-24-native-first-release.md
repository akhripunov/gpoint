# GPoint Native First Release

## Accepted shape

The release workflow uses four jobs: `workspace` and `python` run in parallel,
`build` depends on both, and `deploy` depends on the build. The native workflow
uses GitHub-hosted Ubuntu runners. Candidate branch pushes run CI and publish
candidate-bound GHCR images; the production job is eligible only for a manual
dispatch on `main` and validates the requested exact SHA.

The build publishes the existing GPoint runtime, AGATA, and GSales image roles
under exact SHA tags, with OCI digests, SBOM, and provenance. Eros remains
inactive. GSales must remain stopped; building its image does not start it.

## Safety boundary for this candidate

This change authorizes only the isolated technical branch, nonproduction CI,
and image Build. The workflow's deploy job is read-only preflight and reports
`PRODUCTION_APPLY=NOT_AUTHORIZED`. It does not mutate the VPS, database, or
GitHub deployment state. No production cutover or V5 removal is included.

Before a later production workflow is enabled, the deploy path must prove the
complete migration set and compatibility against current production heads,
backup integrity and isolated restore, available resources, retained rollback
images, exact image digests, SSH host identity, service health, and GSales
stopped state. It must not start Eros or switch real users without explicit
task authorization.

## Candidate checks

- Workspace build, typecheck, package tests, Prisma migration on PostgreSQL 16,
  and Playwright.
- AGATA and GSales pytest suites against PostgreSQL 16.
- BuildKit publishes immutable linux/amd64 images only after both test jobs pass.
- A later Deploy/Verify must check API, Bot, Worker, Mini App, required AGATA
  components, database heads, exact SHA/digests, and the stopped GSales set.
