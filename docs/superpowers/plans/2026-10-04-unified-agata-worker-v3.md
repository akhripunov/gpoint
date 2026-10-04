# Unified AGATA Worker V3 implementation plan

## Goal and boundaries

Integrate the verified G Sales worker lane, the existing GPoint lane, and Conversation Lab as three isolated lanes over one Mac Codex executor. Preserve serial generation, the global worker lock, lane-specific trust boundaries, and the existing Naturalness V1 frozen inputs. This task prepares code and an offline-only V2-to-V3 transition package. It does not install or stop a worker, run real inference, deploy, migrate, send Telegram messages, or invoke paid APIs.

Inputs are the verified G Sales candidate `eef5dc8eadf8dc9b58ea739ac4c884864add49a1` and Naturalness V1 candidate `1b21e8f8eb5d1bc42826baaa54559672969b3f40`. Work is isolated in the dedicated `codex/unified-agata-worker-v3` managed worktree; PR #11 and PR #14 remain unchanged.

## Design

Use a local filesystem spool under the worker's mode-0700 support directory for the LAB lane. Submission writes a bounded, schema-validated JSON request with exclusive creation and atomic rename; worker consumption atomically claims it. Restrictive file permissions, strict ID/type/size/origin checks, experiment/config/job identity, and local receipts form the local-only ingress boundary. No public route, GPoint gateway, tenant, production credentials, Postgres, or external corpus is involved.

Extend the existing single process/scheduler with an explicit LAB lane that invokes the same `run_inference` executor used by GPOINT/GSALES. Keep the one global lock around the executor. Use weighted round-robin opportunities (GPOINT and GSALES get greater weight while LAB remains guaranteed a slot) and separate per-lane failure/backoff state. A claimed LAB job is never automatically retried: on startup any orphaned running claim is closed with an ambiguous/interrupted receipt. Timeout/cancel terminates the Codex process group, releases the slot, and records a terminal state.

Preserve Naturalness V1's frozen source manifest, Baseline A, Policy V1 B, synthetic persona/memory/context, per-candidate histories, blind packet/private mapping, and all existing approval gates. The adapter remains fixture-only. Only a separately approved future command may submit LAB jobs through the actual shared executor; tests in this change mock the executor.

## Steps

1. Add regression tests first for strict LAB submission validation, queue bounds/deduplication, atomic claim, receipt permissions, weighted fairness, one-executor serialization, lane/context isolation, failure recovery, timeout/cancel, crash-without-retry, and unchanged approval gates. Tests must not read `-01` Naturalness scenarios, call Codex, make network requests, access production services, or perform sends.
2. Implement the local spool and integrate LAB with the shared scheduler/executor. Keep GPOINT and GSALES transport contracts unchanged; ensure LAB exceptions only affect LAB state.
3. Add safe transition preparation and rollback tooling/documentation: immutable source/artifact hashes, secrets untouched, preflight/idle checks, global lock, watchdog, LaunchAgent identity, queues, and authenticated polling checks. Tests use temporary directories and fake status; no installed paths are mutated.
4. Run worker, G Sales, and Conversation Lab offline regressions with network/subprocess guards. Review complete diffs and security invariants; obtain independent review and resolve all P1/P2 findings.
5. Update only existing Linear docs (AGATA Canonical Architecture, AGATA Conversation Lab, G Sales Canonical Architecture), preserving owner Handoff content and journals. Push exact-SHA feature branch and create a Draft PR without changing PR #11 or PR #14. Leave production V2 untouched.

## Verification and completion criteria

- One shared executor and one lock; no overlapping Codex execution.
- GPOINT/GSALES/LAB are independently routed and serialized; no lane shares credentials, persona, memory, history, output, or persistence with another.
- LAB has only local ingress; malformed, oversized, duplicate, unauthorized, or over-capacity work fails closed.
- Restart after a claimed job never retries it; timeout/cancel and lane failures do not break other lanes.
- Existing Naturalness offline tests remain fixture-only and the `-01` holdout is not accessed by new tests or generation.
- Worker, G Sales, and Conversation Lab offline regressions pass; installation preparation passes its offline tests; independent review has no unresolved P1/P2 findings.
- Exact-SHA Draft PR and permitted Linear updates exist; V2/production/runtime and sending state are unchanged.
