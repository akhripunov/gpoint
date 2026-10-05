# AGATA Resolver Timeout Recovery Implementation Plan

> **For agentic workers:** Native inline implementation in this session. Follow the TDD steps below and verify each command before proceeding.

**Goal:** Keep the existing N=1 AGATA Worker polling loop responsive when macOS `getaddrinfo` stalls, while retaining normal HTTPS hostname validation and bounded resolver resources.

**Architecture:** Reuse one synchronous asyncio event loop for HTTP requests instead of closing an `asyncio.run()` loop after every request. Give that loop one dedicated default-executor thread so a stuck libc resolver cannot block loop shutdown or multiply into an unbounded thread pool; timed-out requests return through the existing transient-network/backoff path.

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
2. Repeated timeout attempts must not start more than one resolver thread while libc DNS is blocked.
3. A cancellation must not replay or duplicate a gateway request; retain one request attempt and existing server idempotency behavior.
4. Successful requests after resolver recovery must reuse the loop safely and clear transient status through existing behavior.
5. TLS verification, SNI/hostname, `trust_env`, HTTP request shape, and credentials must remain unchanged.

---

### Task 1: Bounded resolver timeout and loop reuse

**Files:**
- Modify: `services/agata/tests/test_mac_worker_http.py`
- Modify: `services/agata/worker/gpoint_mac_worker.py`
- Create: `docs/superpowers/plans/2026-10-05-agata-resolver-timeout-recovery.md`

**Interfaces:**
- Consumes: `worker.api_request(path, body=None, target="GPOINT")`; existing `TransientNetworkError` and `REQUEST_TIMEOUT` contract.
- Produces: private `_run_http(coro)` helper used by both `api_request` and `unauthenticated_health_probe`; one reusable loop with one resolver executor worker.

- [x] Write `test_reg_h_dns_resolution_timeout_is_bounded_and_single_threaded`: in a subprocess, replace `socket.getaddrinfo` with a blocking event, call `api_request("health")` three times with a short overall timeout, require three `TransientNetworkError` results, exactly one resolver call, and successful reuse of the loop.
- [x] Run the test against the unmodified implementation. Expected: the child exceeds the parent watchdog because `asyncio.run()` waits for executor shutdown. Observed: the child exceeded the 3-second watchdog; the failure named the blocked shutdown.
- [x] Implement `_run_http` with a reusable event loop and `ThreadPoolExecutor(max_workers=1)`; use it for the API request and health probe without changing request arguments or certificate validation.
- [x] Re-run the targeted regression and existing HTTP worker tests. Expected: bounded return; all targeted tests pass. Observed: 8/8 HTTP module tests PASS.
- [x] Run relevant Worker pool, HTTP, targets, lock, and transition tests plus the full Conversation Lab offline suite and Naturalness fixture dry-run; run `git diff --check`. Observed: Worker suite 98 passed; Conversation Lab unittest suite 100 passed; dry-run FIXTURE_ONLY (16 dev, 0 holdout, 0 live, no files); syntax and diff checks pass.
- [ ] Commit the verified fix and plan after final verification and independent review.
