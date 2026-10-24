# ME-36 — stock Eros integration

## Owner decision

Use the unmodified upstream `etherfunlab/eros-engine` source at
`d1a140083321687212fb7b17c09cc0c3c9639f5e` (v2.2.1). Do not apply Rust
patches, maintain a fork, replace the installed GPOINT Mac Product Worker, or
add an EROS Worker/lane. The files in `patches/` are retained as historical
audit evidence only. `apply-patches.sh` now fails closed without changing its
input.

Build a local stock image from a clean exact upstream checkout:

```sh
services/eros-engine/build-stock-image.sh \
  /path/to/clean-eros-engine-checkout \
  gpoint-eros:stock-d1a140083321687212fb7b17c09cc0c3c9639f5e
```

The script verifies the upstream commit and clean Git tree before invoking the
upstream Dockerfile. It labels the local image with source, revision and
`patch-set=none`; the local E2E runner rejects unverified images. This is a
local build only and does not publish an image.

## Existing Worker adapter

Stock Eros supports OpenAI-compatible provider endpoints in its model config.
GPoint supplies a provider endpoint outside Eros. The adapter places a
short-lived, signed GPoint identity in the stock `audit.user` field. The GPoint
gateway validates tenant, user, persona and relationship claims before queueing
work on the existing GPOINT lane. A stable request ID is derived for each
distinct provider request so stock Eros calls made during one user turn do not
collide in GPoint idempotency. GPOINT AGATA work keeps priority, so Eros can
wait if that lane stays busy. The configured Codex Local/Product Worker route
must be confirmed in a permitted real integration run; this change does not
alter its installed configuration.

## Upstream constraints and safety gates

- Stock server startup currently requires the `OPENROUTER_API_KEY` environment
  variable to exist before loading its model config, even for a custom-only
  route. The local Compose E2E sets it to an empty value; every configured task
  routes to the internal GPOINT endpoint and the Docker network has no egress.
  No credential or hosted provider is used. Any deployed configuration still
  needs an OWNER-approved supported provider setup.
- Eros SQLx migrations use the default `public._sqlx_migrations` ledger. Keep
  Eros in a dedicated database; do not share the GPoint or AGATA database.
- Historical patch audit: `0001` changed provider startup/config behavior;
  stock Eros boots for this synthetic E2E with the required variable empty and
  all model tasks routed to GPOINT. `0002` moved the SQLx ledger; that is
  unnecessary when Eros owns a dedicated database. `0003` implemented the
  GPOINT bridge inside Eros; it is replaced by the external GPoint provider
  adapter. `0004` attempted to fence a delayed memory write after archive; it
  remains an unverified stock-engine race, not an applied patch. None of these
  patches is used in the pinned stock image.
- Stock Eros's relationship archive endpoint soft-deletes sessions and removes
  some relationship memory, but deliberately retains transcript history.
  GPoint currently has no Eros user-data erasure path. The new GPoint
  relationship mapping uses restrictive user/creator foreign keys so deleting
  either parent cannot silently discard the only Eros instance pointer. This
  intentionally blocks GPoint deletion while a mapping exists; it does not
  erase Eros data. OWNER has accepted a strictly synthetic, single-user VPS
  pilot in a dedicated disposable Eros database. The missing complete erasure
  adapter is a hard gate before any real-user access; the pilot must contain no
  real user data and must be removable by deleting the isolated database.
  The Mac-hosted Ollama model stays on the Mac.
- Stock `classify_session` performs model/extraction and embedding before an
  unconditional memory upsert; archive deletes relationship memory. A delayed
  write crossing archive is a plausible data-resurrection risk, not a proven
  failure. It needs a synthetic reproduction and an upstream-compatible
  lifecycle solution before data deletion can be relied on. Transcript
  retention after archive itself is expected behavior.
- The pilot allowlist is synthetic and contains exactly one test user and one
  test creator. No normal user is routed to Eros, and no AutoWrite/memory-policy
  change is made here.

## Local verification boundary

`run-local-e2e.sh` uses two disposable PostgreSQL databases, internal-only
Docker networking, synthetic identities/data, and a deterministic local Worker
fixture. The run passed a 15-turn dialogue, server age/consent gates, GPoint API
history, stock Eros extraction and durable recall, a 14-message gap, provider
context, existing GPOINT queue routing, and persistence of history and durable
memory rows after Eros restart. It also verified that a second JWT subject sees
no first-user sessions and receives HTTP 403 for a foreign session. The
GPoint inference queue's tenant RLS integration test also passes using the
`NOBYPASSRLS` `gpoint_worker_test` role. Eros itself has no per-user database
RLS policies, so its user isolation is verified at the stock HTTP API boundary,
not with Eros database RLS. The GPoint API has no connection or SQL grants to
the Eros database; product read receipts live in the GPoint database and store
only user/creator/session/message IDs plus `readAt`, never message bodies. The
post-restart recall check passed.

A separate disposable-catalog probe against the pinned stock image found all 34
ordinary and partitioned `engine` tables with RLS enabled, none with `FORCE ROW
LEVEL SECURITY`, and no policies. The actual `eros_runtime` role has
`BYPASSRLS` and can see the seeded persona; a role with equivalent table grants
but `NOBYPASSRLS` sees zero seeded persona rows, while `/healthz` still returns
`ok`. This is not per-user isolation: it is an RLS configuration that requires
the runtime role to bypass policy enforcement. The runner now checks every
ordinary and partitioned table, confirms the runtime-visible row count, and
reports this result explicitly. It remains a hard VPS-pilot security blocker.
This is a stock-engine integration E2E with a fixture provider, not real Codex
inference. It does not prove Mac Product Worker availability, VPS resource
needs, or production performance.

The pinned stock source compiled with the isolated `rust:1.98` build image; its
offline `print-openapi` command and full local Compose E2E passed. Evidence:
`LOCAL_E2E_STOCK_EROS_IMAGE=PASS`, `LOCAL_E2E_15_TURN_DIALOGUE=PASS`,
`LOCAL_E2E_MEMORY_EXTRACTION=PASS`, `LOCAL_E2E_DURABLE_MEMORY=PASS`,
`LOCAL_E2E_MEMORY_RECALL_RESPONSE=PASS`,
`LOCAL_E2E_PROVIDER_CONTEXT=PASS`, `LOCAL_E2E_RESTART_HISTORY=PASS`,
`LOCAL_E2E_STOCK_API_USER_ISOLATION=PASS`,
`LOCAL_E2E_GPOINT_QUEUE_RLS_TENANT_ISOLATION=PASS`,
`LOCAL_E2E_MEMORY_RECALL=PASS`, and `LOCAL_E2E_CLEANUP=PASS`.
`OPENROUTER_API_KEY` was empty, with no credential; the inference test network
had no egress. Real inference was not run. The GPoint queue tenant RLS test
passed with a non-bypass worker role; Eros has no per-user RLS, and the API
fixture's `BYPASSRLS` receipt access is not a production isolation check.

The deterministic fixture run is not a real Codex Local E2E. A later bounded
attempt made one Codex CLI invocation and stopped on its first network error;
no model response was received and no follow-up inference was sent. On
2026-10-23, read-only host diagnostics found `chatgpt.com` resolving to
`240.0.0.48` and `api.openai.com` to `240.0.2.128`, both in the non-public
240.0.0.0/4 range. The configured resolver was classified as private and the
route used a `utun` interface. An unauthenticated HEAD to `chatgpt.com` returned
403; the request to `api.openai.com` timed out during connection/TLS. This
identifies a current network-path blocker but not the specific resolver,
tunnel, or policy responsible. No DNS, VPN, route, or Worker settings were
changed. A new limited OWNER grant is required after the network path is
confirmed healthy.

The adapter provisions the persona with stock `seed-personas` and uses stock
`POST /comp/chat/start` to create or resume a user instance. GPoint stores the
creator-to-genome and user/creator-to-instance mappings in its own database;
runtime persona and relationship operations no longer query Eros tables. The
synthetic E2E follows the same path. A stored instance mapping is passed back
as `instance_id`; upstream requires that instance to still be active and does
not reactivate it. A missing mapping uses `genome_id`, which can create or
reactivate an archived instance. Since stock session listing omits archived
sessions and genome IDs, an initial rollout must use a fresh, dedicated Eros
database with no historical instances. Existing Eros history cannot be safely
mapped through the public API alone. Each enabled creator profile also needs
its `erosGenomeId` provisioned by `seed-personas` before routing is enabled;
there is no stock genome-provisioning HTTP endpoint or safe runtime fallback.

The stock API has no operation for selected-message read receipts or unread
counts, and `POST /comp/chat/{session_id}/read` marks the whole session. The
GPoint adapter therefore indexes assistant message IDs returned by the stock
history API and stores UI read timestamps in GPoint-owned metadata. The send
route records each returned assistant ID; history and inbox reads reconcile
IDs from the latest history page. Read acknowledgement checks that every
requested ID was previously observed for the authenticated GPoint user,
creator, and Eros session. This preserves visible-message-level semantics
without direct Eros SQL or a broad Eros DB role. Reconciliation is bounded to
the pages the UI/inbox reads; historical sessions or messages outside those
pages are not backfilled and are not claimed to have exact unread counts.

Stock Eros has no per-user database RLS policies. Its public HTTP handlers
scope session and message operations to the authenticated Eros user, which the
adapter maps deterministically from the GPoint account. The local API-isolation
test verifies that HTTP boundary. Removing receipt SQL closes the direct
cross-service database bypass, but it does not fix Eros runtime `BYPASSRLS` or
provide per-user database isolation. This is accepted only for the isolated
single-synthetic-user pilot; it remains a hard gate before any real-user
access.

Before the synthetic pilot, independently verify stock image SHA/provenance,
dedicated database ownership and backup/restore, isolated network boundaries,
the exact provider route, GPOINT Worker compatibility, current VPS resource
headroom, and AGPL-3.0 distribution/source obligations. Real-user access also
requires a tested deletion path. Mac resource measurements are directional
evidence and cannot replace a VPS load measurement.

## Isolated VPS pilot profile

`infra/production/docker-compose.eros-pilot.yml` is an opt-in overlay for the
existing V5 Compose project. It adds a second instance of the existing GPoint
API image for the synthetic scope so that the running API container is not
recreated. The pilot API uses the existing GPoint database and queue, with an
exact one-user/one-creator allowlist; Eros gets a separate `eros_pilot`
PostgreSQL instance and volume. The pilot API is the only service connected to
both GPoint's existing internal networks and the pilot networks. Eros can reach
the pilot API and its own database, but cannot reach Ollama directly. The API
keeps the existing `EROS_OLLAMA_EMBEDDING_URL=http://ollama:11434/api/embed`
contract. A static `/29` internal Docker network maps `ollama` only inside the
pilot API container to a relay bound on the Docker bridge gateway. The relay
accepts only the pilot API's embedding POST and the verifier's read-only model
metadata request, checks the pinned Qwen3 digest, and forwards to VPS loopback
port 11435. It does not publish a host port. Caddy continues to return 404 for
the public embeddings proxy route.

On the Mac, Ollama remains loopback-only. The operator starts
`open-mac-ollama-tunnel.sh`, which requires an existing pinned SSH host identity
and opens a Mac-initiated reverse forward from VPS `127.0.0.1:11435` to Mac
`127.0.0.1:11434`; `GatewayPorts=no` is required. No Worker or sshd setting is
changed. The exact model digest is
`ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d`, the
Q8_0 Qwen3 Embedding 0.6B artifact. A one-shot verifier checks the digest and
512-dimensional response before Eros starts. Chat and extraction continue
through the GPoint API and existing GPOINT Worker route. The stock Eros image
remains the candidate-bound V5 GHCR image with `patch-set=none`.

The Compose profile caps Eros at 384 MiB, PostgreSQL at 256 MiB, the duplicate
synthetic-scope GPoint API at 256 MiB, and startup helpers/relay at another
448 MiB. These are ceilings, not actual consumption. The isolated V5 pilot
target requires at least 1,408 MiB of fresh `MemAvailable` before starting and
5 GiB free disk; this conservative startup bound is intentionally above the
historical VPS snapshot of about 766 MiB available RAM. A fresh measurement
must pass before Apply. After Apply, the target records actual Eros,
PostgreSQL, API and relay memory from `docker stats`. Mac Ollama memory is
outside the VPS budget. See ME-36 for the V5 gate and approval.

The V5 `eros-pilot` mode is a separate workflow job and protected GitHub
Environment. It uses the exact successful Prepare receipt and digest-pulls only
the candidate GPoint API image needed for synthetic routing and the stock Eros
image. Pilot IDs and credentials come from the protected
`EROS_PILOT_ENV_B64` Environment secret; it accepts only `EROS_PILOT_*` keys,
requires one synthetic tenant/user/creator identity, and writes the file to the
VPS with mode 0600. Missing parameters block before Apply. The job uses the
unique Compose project `gpoint-eros-pilot` and an explicit six-service allowlist.
It cannot enter the normal GPoint production executor or its Prisma/AGATA/GSales
migration path. A failed first install stops only those pilot services and
retains the separate PostgreSQL volume for recovery.

### Pilot backup and rollback

Back up only the dedicated pilot database. Keep the archive and a mode-0600
copy of `/opt/gpoint/env/eros-pilot.env` under the mode-0700
`/opt/gpoint/backups/eros-pilot` directory. The env copy preserves the exact
pilot image digest and credentials; do not put it in Git, CI artifacts or
Linear. From the VPS, set `PILOT_SHA` to the deployed source SHA and use this
Compose command for every operation (run the snippets in Bash):

```bash
set -euo pipefail
: "${PILOT_SHA:?set the exact deployed profile source SHA}"
profile_sha=$(sed -n 's/^EROS_PILOT_SOURCE_SHA=//p' /opt/gpoint/env/eros-pilot.env | head -1)
test "$PILOT_SHA" = "$profile_sha"
printf '%s\n' "$PILOT_SHA" | grep -Eq '^[a-f0-9]{40}$'
compose=(docker compose -p gpoint-eros-pilot
  --env-file /opt/gpoint/env/production.env
  --env-file /opt/gpoint/env/eros-pilot.env
  -f /opt/gpoint/compose/docker-compose.yml
  -f "/opt/gpoint/releases/${PILOT_SHA}/source/infra/production/docker-compose.eros-pilot.yml"
  --profile eros-pilot)
```

Create and validate a custom-format snapshot before changing the pilot image or
schema:

```bash
set -euo pipefail
umask 077
install -d -m 700 /opt/gpoint/backups/eros-pilot
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="/opt/gpoint/backups/eros-pilot/eros-pilot-${stamp}.dump"
env_snapshot="/opt/gpoint/backups/eros-pilot/eros-pilot-env-${stamp}.env"
install -m 600 /opt/gpoint/env/eros-pilot.env "$env_snapshot"
"${compose[@]}" exec -T postgres pg_dump -U eros_pilot_admin -d eros_pilot -Fc > "$backup"
"${compose[@]}" exec -T postgres pg_restore --list - < "$backup" > /dev/null
sha256sum "$backup" > "${backup}.sha256"
chmod 600 "$backup" "${backup}.sha256"
```

For rollback, explicitly select a saved archive and its matching mode-0600
environment snapshot. Verify the archive checksum before restoring. The saved
`EROS_PILOT_SOURCE_SHA` binds the profile to the matching source bundle, and
`EROS_PILOT_IMAGE` preserves the immutable Eros image. The commands below
assume the standard pilot roles from `eros-pilot-init.sh` already exist in
this PostgreSQL cluster. Restore names are timestamped, so remnants from a
failed earlier attempt remain untouched:

```bash
set -euo pipefail
: "${PILOT_BACKUP:?set an explicit backup path}"
: "${PILOT_ENV_SNAPSHOT:?set the matching saved pilot env path}"
case "$PILOT_BACKUP" in /opt/gpoint/backups/eros-pilot/*.dump) ;; *) exit 2 ;; esac
case "$PILOT_ENV_SNAPSHOT" in /opt/gpoint/backups/eros-pilot/eros-pilot-env-*.env) ;; *) exit 2 ;; esac
backup_stamp=${PILOT_BACKUP##*/eros-pilot-}
backup_stamp=${backup_stamp%.dump}
env_stamp=${PILOT_ENV_SNAPSHOT##*/eros-pilot-env-}
env_stamp=${env_stamp%.env}
test -n "$backup_stamp" && test "$backup_stamp" = "$env_stamp"
test "$(stat -c '%a' "$PILOT_ENV_SNAPSHOT")" = 600
sha256sum -c "${PILOT_BACKUP}.sha256"
PILOT_SHA=$(sed -n 's/^EROS_PILOT_SOURCE_SHA=//p' "$PILOT_ENV_SNAPSHOT" | head -1)
printf '%s\n' "$PILOT_SHA" | grep -Eq '^[a-f0-9]{40}$'
rollback_compose=(docker compose -p gpoint-eros-pilot
  --env-file /opt/gpoint/env/production.env
  --env-file "$PILOT_ENV_SNAPSHOT"
  -f /opt/gpoint/compose/docker-compose.yml
  -f "/opt/gpoint/releases/${PILOT_SHA}/source/infra/production/docker-compose.eros-pilot.yml"
  --profile eros-pilot)
backup="$PILOT_BACKUP"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
restore_db="eros_pilot_restore_${stamp}"
failed_db="eros_pilot_failed_${stamp}"

# Create a clean target and restore the archive with its original object owners.
"${rollback_compose[@]}" exec -T postgres psql -U eros_pilot_admin -d postgres \
  -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${restore_db} OWNER eros_pilot_admin"
"${rollback_compose[@]}" exec -T postgres psql -U eros_pilot_admin -d postgres \
  -v ON_ERROR_STOP=1 -c "GRANT CONNECT, CREATE ON DATABASE ${restore_db} TO eros_migrator"
"${rollback_compose[@]}" exec -T postgres psql -U eros_pilot_admin -d postgres \
  -v ON_ERROR_STOP=1 -c "GRANT CONNECT ON DATABASE ${restore_db} TO eros_runtime"
"${rollback_compose[@]}" exec -T postgres pg_restore -U eros_pilot_admin \
  -d "${restore_db}" < "$backup"

# Confirm the restore has a populated stock migration ledger before cutover.
"${rollback_compose[@]}" exec -T postgres psql -U eros_pilot_admin -d "${restore_db}" \
  -v ON_ERROR_STOP=1 -Atc \
  'SELECT count(*) FROM public._sqlx_migrations' | grep -Eq '^[1-9][0-9]*$'
# Keep the current Eros database active until the restore ledger check passes.

# Cut over by renaming databases; Eros remains stopped if any step fails.
"${rollback_compose[@]}" stop eros
"${rollback_compose[@]}" exec -T postgres psql -U eros_pilot_admin -d postgres \
  -v ON_ERROR_STOP=1 -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('eros_pilot', '${restore_db}') AND pid <> pg_backend_pid()"
"${rollback_compose[@]}" exec -T postgres psql -U eros_pilot_admin -d postgres \
  -v ON_ERROR_STOP=1 -c "ALTER DATABASE eros_pilot RENAME TO ${failed_db}"
"${rollback_compose[@]}" exec -T postgres psql -U eros_pilot_admin -d postgres \
  -v ON_ERROR_STOP=1 -c "ALTER DATABASE ${restore_db} RENAME TO eros_pilot"
"${rollback_compose[@]}" up -d --no-deps eros
"${rollback_compose[@]}" ps eros
# After health is green, verify synthetic history and recall through the pilot
# API. If that check fails, stop Eros and rename the failed restored DB aside;
# rename ${failed_db} back to eros_pilot and restore the prior env snapshot.
```

The `stamp` contains only UTC digits and `T`/`Z`, so generated database
identifiers are safe to interpolate in these SQL statements. Confirm Eros
health and perform a synthetic history/recall check before considering rollback
complete. Keep the failed database and snapshot until that check passes. Do not
use `down -v`, stop the Mac tunnel as a substitute for database rollback, or
touch the GPoint database. A restore
or cutover failure leaves Eros stopped and both database copies available for
diagnosis.

The local rehearsal used the exact stock image and disposable PostgreSQL on an
internal-only Docker network: it applied the stock migrations, created a
synthetic marker, ran `pg_dump -Fc`, restored into a second database, verified
the original row and `eros_migrator` ownership, then swapped database names and
verified the restored row again. Both databases, container and network were
removed afterward. This validates database snapshot/restore/name-swap logic;
the full VPS Compose/image rollback is still a pre-deploy gate.
