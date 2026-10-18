# GPoint release performance controls

The canonical release flow remains Mac checkout → exact candidate SHA → remote
GitHub verification → isolated build/artifact checks → VPS apply → post-switch
reconciliation → immutable production ref. These optimizations do not bypass
any release gate.

- Noisy remote Docker builds write full logs on the VPS and return bounded
  summaries plus the exact command exit status. Build and switch stages emit
  timing markers; failures retain stderr context.
- Runtime images use the exact `gpoint-runtime:<commit>` tag. A verified image
  already present on the VPS is reused; the runtime Dockerfile copies lockfiles
  and package manifests before source and uses a BuildKit pnpm cache mount.
- The sensitive-detector identity is a SHA-256 over its local Dockerfile and
  configuration inputs. Its image tag is independent of the GPoint commit and
  is rebuilt/recreated only when that identity changes.
- Migration status is checked before migration apply. An up-to-date database
  records the no-migration decision and skips `migrate deploy`.
- API, Worker, and Bot are recreated for a changed runtime image. The detector
  is omitted from `compose up` when its verified source-identity image is
  unchanged. Database, Redis, and shared infrastructure are never recreated.
- A nonzero compose result after a candidate switch is never treated as an
  ordinary pre-switch failure. Actual runtime identity, health, and Mini App
  SHA are reconciled first; only verified gates may create the immutable ref.

Do not run a new production apply solely to benchmark these controls. Release
manifests retain stage timings and artifact-reuse decisions for future
comparison.

## GitHub artifact transport

Prepare artifacts are named by exact candidate, GitHub run ID and attempt.
Already compressed image archives use `compression-level: 0` for the outer ZIP.
Deploy reads fresh GitHub main/run/artifact metadata before consulting the Mac
cache and verifies the ZIP against GitHub's SHA-256 digest on every cache hit.
Expired artifacts, changed attempts, unavailable digests and mismatched source
SHAs block reuse. The inner receipt, archive checksum and image parity gates
still run. The project cache holds at most three ZIPs / 1 GiB (a selected ZIP
larger than that bound is retained alone); no cache lives inside the checkout.

On the VPS, the exact app and AGATA image IDs, platform and source labels must
match the verified receipt before upload/load can be omitted. Upload and load
have separate timing markers. Dependency caches accelerate package downloads;
all tests and migration rehearsals still execute.

GitHub Prepare uses separate stable BuildKit GHA cache scopes per image
(`gpoint-runtime`, `gpoint-agata-runtime`, and `gpoint-g-sales-runtime`). Keep
candidate-SHA metadata late in the main runtime Dockerfile, immediately before
the Mini App build that embeds it. The final image still receives the exact
revision label and immutable candidate tag; upstream dependency and service
build layers can then be reused across release-only commits.

Deploy saves runtime evidence before a separate Ubuntu publication job writes
to Linear. A publication failure remains a failed workflow and a visible
blocker, but never changes the recorded result of the runtime attempt. Reruns
have independent events and evidence artifacts; retry only the publication job
when runtime evidence already confirms completion. Production authorization
and the canonical release lock remain mandatory.

## Avoiding repeated release cycles

The Prepare image round-trip now targets the production Docker 29.1.3
containerd/overlayfs store instead of another classic store. It runs the same
import, native identity capture, parity and reuse command builders as Deploy,
and deliberately rejects the historical config-as-native proof regression.
The images are built once; rehearsal only imports that archive. Its measured
duration is recorded in the receipt; this improves failure detection and is
not a promise of a faster successful Prepare.

Safe pre-switch failures keep their evidence in Actions artifacts and Linear
without a bookkeeping commit to main. Repeating that exact prepared candidate
can reuse the verified artifact. Partial/unknown mutations still persist
recovery state; no automatic retry or rollback is introduced.

Baseline from 2026-10-02: Prepare run 36967671335 took 331 seconds including
publication; Deploy run 36968491432 took 261 seconds including publication.
Canonical apply took 200.1 seconds (upload 41.1s, load 41.4s). Compare future
whole-workflow durations and repeated-cycle count, not just apply time. The
new rehearsal's CI duration must be measured before claiming speed gains.

## Prepare V4 Phase 2 — bounded Docker image builds

On 2026-10-15, the latest comparable full Prepare before this local change was
run 38714817433: 528 seconds for Prepare and 21 seconds for the separate
publication job (549 hosted job-seconds total; 10 rounded billed minutes).
Baseline run 38695939791 took 652 hosted job-seconds (12 rounded billed
minutes). This is one post-change observation, not a repeatable performance
claim. Its 103-second reduction is 15.8% by hosted job-seconds.

Prepare spends 199.4 seconds in the three sequential Docker builds in that
run: GPoint 142.4s, AGATA 38.8s and GSales 18.2s. Phase 2 locally schedules
those same exact-SHA, linux/amd64 builds with a maximum concurrency of two;
tags, revision labels, BuildKit scopes, tests and migration rehearsals stay
unchanged. An ideal build schedule is about 142.4s, saving 57.0s inside that
stage. The actual end-to-end critical path in run #38714817433 shows that this
would save **0 seconds from Prepare completion** by itself: the required GSales
PostgreSQL regression finished 1.341s after the last post-build gate. The later
image archive/export work waits for both. Docker concurrency could also add
resource contention. Do not count this as hosted-minute savings.

The full Docker rehearsal is pending. The isolated Mac currently has another
active disposable GPoint database test container and uses a shared BuildKit
cache, so running concurrent image builds now would interfere with unrelated
work and would not provide a clean measurement. No GitHub Actions run was
started for this change. The next authorized canonical Prepare can validate
image correctness and record actual wall time, BuildKit cache hits, and hosted
runner usage. Do not claim the theoretical saving until those measurements
exist.

Recent hosted logs also show that workspace pnpm installation reused all 325
packages from the Actions cache, while Docker dependency layers downloaded
322 packages during the first runs after the Dockerfile graph changed. Because
the feature-branch rehearsal could not seed the main cache and there has been
no later comparable main run, this does not yet prove a persistent Docker
cache miss. Preserve the existing per-image cache scopes and test suite; use a
future owner-authorized normal main Prepare to determine whether the new graph
now hits cache.

## Prepare V4 Phase 2 — critical path and next optimization

The exact timeline for Prepare #38714817433 (run URL above; hosted Prepare job
116355346773) is:

| Gate | UTC interval | Duration | Critical-path result |
| -- | -- | --: | -- |
| Prepare hosted job | 09:43:00–09:51:48 | 528s | Job duration |
| Main regression and Docker step | 09:43:53–09:50:57 | 424s | One workflow step containing overlapping work |
| GSales PostgreSQL child | 09:44:43.825–09:49:56.448 | 312.619s | Last dependency before archive/export |
| Sequential Docker builds | 09:45:49.287–09:49:08.710 | 199.423s | Not the end-to-end bottleneck |
| Post-build gates | through 09:49:55.107 | — | Finished 1.341s before GSales PostgreSQL |
| Archive export + deploy artifact roundtrip | 09:49:56.468–09:50:56.418 | 59.950s | Starts only after GSales regression joins |
| Separate hosted publication job | 09:51:51–09:52:12 | 21s | Adds one rounded hosted minute |

Within the 312.619s GSales child, the available pytest summaries account for
about 60.88s: 74 GSales tests (16.15s), the AGATA GSales-specific PostgreSQL
test (1.02s), the AGATA baseline migration test (5.60s), and 355 AGATA Core
tests (38.11s). The remaining approximately 251.74s has no phase attribution
in this run. The parent buffers child output until completion, and the runner
did not emit internal setup/migration timings, so this remainder cannot safely
be assigned to PostgreSQL startup, migrations, fixture setup, or tests.

The four broad AGATA/PostgreSQL invocations in the GSales child use fresh
databases. They are not yet proven redundant with the main AGATA suite: their
database role/setup differs. Do not remove them until a test-by-test and
environment comparison proves the same assertions remain covered under the
same non-bypass role, tenant bindings, migration head and RLS state. GSales
specific tenant/source and guarded migration checks remain mandatory.

The local follow-up adds bounded `GS_POSTGRES_PHASE` markers for disposable
container startup/readiness, role/binding setup, AGATA migrations, tenant/RLS
probes, guarded GSales migration replay, each pytest suite, Core database setup,
and cleanup. These report only phase name, status, and monotonic duration; a
nonzero pytest exit is recorded as `FAIL`. This is instrumentation only: no
Docker benchmark has been run, and no time saving is claimed. It will let the
next owner-authorized normal Prepare identify which of the unmeasured phases
is worth optimizing. No additional Actions run is authorized by this report.

### Cache and component reuse findings

Run #38714817433 imported each image's BuildKit cache manifest and reported
3/0/2 cached BuildKit steps for GPoint/AGATA/GSales. The GPoint `pnpm install`
layer itself ran and downloaded 322 packages; the separate workspace install
reused 325 packages and downloaded zero. The Docker pnpm store is a BuildKit
cache mount, distinct from the GitHub workspace pnpm cache. The observed miss
is consistent with the first run after PR #84 changed the Dockerfile dependency
layer graph; no later comparable main build exists to prove whether the new
install layer now hits. No lockfile or dependency drift was found in the
release-only candidate. Keep the exact lockfile, projected manifests and
lifecycle inputs in the cache key; do not add a cache path that can bypass
dependency identity.

The existing `STATIC_UI_ONLY` route can avoid Docker/PostgreSQL work only for a
complete, ledger-backed diff confined to approved Mini App source paths.
V4 component impact for backend components remains advisory. Canonical receipt,
bundle verification, preflight and VPS apply still require exact-candidate
source identity for all three images. Reusing an old AGATA or GSales image under
a new candidate therefore needs a versioned parent-artifact/provenance contract
across Prepare, immutable receipt, bundle verifier, preflight, production
manifest and rollback proof. That chain is not currently wired end to end;
selective backend reuse must fail closed to FULL until it is. No safe time
saving for that change is measured yet.

### Ranked local optimization backlog

| Priority | Change | Baseline / possible saving | Confidence | Risk and validation |
| -- | -- | -- | -- | -- |
| 1 | Attribute and then reduce the GSales regression's unmeasured setup/migration time | 312.619s total; about 251.74s un-attributed. A saving beyond about 58s can expose the Docker scheduler's 57s build reduction; current whole-Prepare saving is 0s. | High that this is the next measurement target; low on savings before phase data | Keep all migrations, tenant/RLS probes and tests. Use the new phase markers, then optimize only a measured slow phase and retain regression tests. |
| 2 | Prove stable Docker dependency-layer reuse after PR #84 | 322 package downloads in the first observed new graph; 0 later-run savings measured | Medium that this run is a cold graph transition; low on duration saved | One ordinary future canonical main Prepare needs no extra rehearsal; verify install layer is cached while lockfile, projected manifests, lifecycle scripts and install commands still invalidate it. |
| 3 | Extend component-aware immutable reuse beyond UI-only | Three builds total 199.423s; a serial component build could avoid some builds, but current max-two scheduler already overlaps them and the end-to-end critical path is gated elsewhere | Low until a cross-stage provenance contract exists | Require exact image digest/source/build inputs, parent immutable receipt, migration compatibility and rollback evidence through all consumers. Current behavior remains FULL. |
| 4 | Further optimize Docker build parallelism | 57.0s ideal build-stage reduction; 0s Prepare completion reduction in the measured run | High for build-stage math; high for zero end-to-end effect absent another gate change | Already in local PR #85. Rehearse only as part of a later owner-authorized combined candidate; measure resource contention and billed minutes. |

At the measured 528s hosted Prepare job plus 21s publication, the two jobs
round separately to 10 billed minutes. The proposed scheduler alone still
rounds to 10 because the regression remains on the critical path. The newly
instrumented runner has no measured speedup. A future performance candidate
needs an owner-approved Actions rehearsal to prove runtime, cache behavior, and
rounded billing; production remains unchanged.
