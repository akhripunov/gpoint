# AGATA Resolver Timeout Recovery Implementation Plan

> **For agentic workers:** Native inline implementation in this session. Follow the TDD steps below and verify each command before proceeding.

**Goal:** Keep the existing N=1 AGATA Worker polling loop responsive when macOS `getaddrinfo` stalls, while retaining normal HTTPS hostname validation and bounded resolver resources.

**Architecture:** Reuse one synchronous asyncio event loop for HTTP requests instead of closing an `asyncio.run()` loop after every request. Replace the loop's hostname resolver with a single daemon thread and a fail-fast in-flight gate: a stuck libc resolver cannot block request handling or process shutdown, and another lookup is not queued until the active lookup returns. Timed-out requests return through the existing transient-network/backoff path.

**Tech Stack:** Python, asyncio, httpx, concurrent.futures, unittest.

**Spec:** Linear `AGATA Workers` (5730ceec-da5e-4390-9981-a1b0b0031d8f) and current OWNER instruction in this Conversation Lab task.

## Global Constraints

- Do not change INCY, VPN, global DNS, hosts, global routing, credentials, TLS/SNI verification, gateway hostname, protocol, Core contract, or production.
- Preserve one Worker Pool, GPOINT/GSALES/LAB lanes, N=1, existing model configuration, and existing bounded poll backoff.
- Do not install/update the Worker bundle or restart LaunchAgent until the code fix, review, fresh queue/job/lock preflight, and rollback are ready.
- LAB remains disabled, approval gate closed, queue empty; no real inference.
- Use the exact installed Worker source lineage; do not modify GSales checkout.

## Review Focus

1. A stalled `getaddrinfo` must not make a request exceed its configured overall timeout or freeze subsequent Worker status/poll handling.
2. Repeated timeout attempts must not start or queue another resolver while libc DNS is blocked.
3. A normal process exit must not wait for a blocked resolver thread.
4. A cancellation must not replay or duplicate a gateway request; retain one request attempt and existing server idempotency behavior.
5. Successful requests after resolver recovery must reuse the loop safely and clear transient status through existing behavior.
6. TLS verification, SNI/hostname, `trust_env`, HTTP request shape, and credentials must remain unchanged.

---

### Task 1: Bounded resolver timeout and loop reuse

**Files:**
- Modify: `services/agata/tests/test_mac_worker_http.py`
- Modify: `services/agata/worker/gpoint_mac_worker.py`
- Create: `docs/superpowers/plans/2026-10-05-agata-resolver-timeout-recovery.md`

**Interfaces:**
- Consumes: `worker.api_request(path, body=None, target="GPOINT")`; existing `TransientNetworkError` and `REQUEST_TIMEOUT` contract.
- Produces: private `_run_http(coro)` helper used by both `api_request` and `unauthenticated_health_probe`; one reusable loop with a single daemon resolver thread and no queued resolver work.

- [x] Write `test_reg_h_dns_stall_exits_process_without_unbounded_resolver_queue` and `test_reg_i_dns_resolver_recovers_after_blocked_lookup_returns`: block `socket.getaddrinfo`, require repeated API timeouts without another lookup, ordinary child exit while the first resolver remains blocked, then recovery after releasing the resolver fixture.
- [x] Run the shutdown test against the first implementation. Observed RED: API calls timed out, but the child exceeded the 3-second process-exit watchdog because `ThreadPoolExecutor`'s atexit hook waits for the resolver.
- [x] Implement `_run_http` with a reusable event loop plus `_bounded_getaddrinfo`: one daemon thread, no executor queue, fail-fast while a lookup is outstanding; use it for API request and health probe without changing request arguments or certificate validation.
- [x] Run targeted regressions; both bounded ordinary shutdown and lookup recovery pass. Existing HTTP and Worker tests pass.
- [x] Run relevant Worker pool, HTTP, targets, lock, transition, runtime migration and LAB tests plus the full Conversation Lab offline suite and Naturalness fixture dry-run; run `git diff --check`. Latest results: Worker suite 99 passed; Conversation Lab suite 100 passed; dry-run FIXTURE_ONLY (16 dev, 0 holdout, 0 live, no writes); syntax/diff checks pass.
- [ ] Commit the verified fix and plan after final verification and independent review.
