# GPoint first-cutover owner implementation plan

> Use Superpowers execution, tests before fixes, and independent review.

**Goal:** survive loss of the GitHub SSH observer without competing mutators.
**Architecture:** one temporary systemd service owns the existing serial
cutover phases; SSH is submit/observe only.
**Tech stack:** Python 3, systemd-run, flock, Docker, pinned stock Kamal 2.12.0.
**Spec:** ../specs/2026-10-31-kamal-cutover-owner-design.md

## Constraints and review focus

- Exact SHA/run/attempt, immutable baseline and bundle hashes; root-owned 0700
  staging and 0600 secrets; no symlinks or reusable attempt namespace.
- Never start legacy alongside an unknown/live candidate or Proxy.
- Preserve PostgreSQL/Redis/data/media, stopped GSales, absent Eros and private
  detector invariants. No production builds or permanent release controller.
- Credentials expire, are localhost-only and removed without losing other keys.
- Observer loss must not terminate owner; owner crash must fail closed.

## Tasks

1. Reproduce unsafe recovery after route-proof/Proxy-stop failure, fix it,
   verify no legacy/Caddy start in those failures. Add fresh DB/private and
   global consumer guards before recovery mutations.
2. Implement an attempt-bound server owner and temporary localhost credential
   lifecycle with test-first local process coverage: ordered phases, recovery
   exactly once after command exit, observer death, tampering, duplicate
   owner, failed recovery, cleanup. Keep production entry fail closed.
3. Package existing phase scripts and identity/env under an immutable manifest;
   change first-cutover workflow to submit/observe a systemd service. Preserve
   all existing preflight/backup/verify and Deployment evidence gates.
4. Run isolated Docker fault injection for observer loss across stops and
   handoff; independent whole-scope review; fix findings; clean scoped local
   commits and handoff checker. Do not publish a partial fix.
5. Once all gates pass, resolve exact-SHA publication/protected authorization,
   run new hosted CI/preflight, freshly verify VPS and backup/restore, execute
   one first cutover, then verify ordinary Deploy and protected Kamal rollback.
   Retire legacy entrypoints only after verified cutover and authorized cleanup.

The existing ME-52/ME-54 issues hold handoff evidence; no separate handoff doc.
