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
