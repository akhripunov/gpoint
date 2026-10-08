# AGATA Worker Dedicated Slots and Conversation Lab Completion — Design

> **OWNER TASK / approved scope — 2026-10-08.** This design records the requirements in the owner task and reconciles them with the existing AGATA Worker V3 and Conversation Lab contracts.

## Goal

Provide one AGATA Worker Pool with three fixed lane slots—GPOINT, GSALES, and LAB—so a remote VPS failure is isolated to its production lane and does not prevent local LAB queue work. Continue the existing Conversation Lab implementation through all offline checks and leave live-only quality gates explicit.

## Architecture

- Preserve one Pool supervisor, one canonical Worker installation, the existing queue/job/receipt contracts, one Codex process-management path, and the existing GPOINT/GSALES behavior.
- Bind `slot-01` to GPOINT, `slot-02` to GSALES, and `slot-03` to LAB. Reject any slot/job lane mismatch before executing an offer. Keep separate per-slot state and ownership locks, lane-specific error/backoff state, existing per-lane queues, and receipts carrying the slot identity. Keep job locks as the duplicate-claim authority.
- Poll and recover each lane independently. A remote GPOINT or GSALES timeout must not prevent the scheduler from checking the local LAB queue. Production priority applies when production work is observable; LAB does not require an unavailable VPS queue snapshot to accept or execute an already approved local job.
- Keep a shared provider execution guard at the currently verified safe level (`N=1`). Three fixed slots do not authorize three simultaneous Codex inference processes. Increasing effective concurrency requires separate bounded live provider evidence/OWNER approval.
- Keep LAB approval, model, subscription-auth, budget, receipt, and no-retry checks fail-closed. A permanent LAB lane is not inference authorization.
- Use the existing installed Worker as the execution authority. No second Worker Pool/Core, standalone Codex path, new queue system, LaunchAgent, or production service is introduced. The single supervisor starts a lane-pinned execution process for the lane’s reserved slot. Maintain lane runtime pointers under the same canonical runtime root so a LAB executor update can switch/restart only slot-03; the common supervisor and GPOINT/GSALES executors remain on their installed versions. Full supervisor/runtime/LaunchAgent changes still use canonical rollback-protected controls.
- Conversation Lab remains Promptfoo → AGATA adapter → existing local LAB queue/slot → Worker/Codex subscription → Worker receipt → adapter → Promptfoo. Existing blind, persona, OWNER-lock, dataset-isolation, and optimizer gates remain authoritative.

## Scope and non-goals

This task may change local Worker source/tests and Lab tooling/tests. It does not modify production GPoint/GSales API behavior, database/schema, production prompts, VPS, credentials, network configuration, Incy/VPN, or installed runtime without a fresh safe lifecycle gate. It does not perform live inference, judge calls, A/B, optimizer calls, paid/API-key use, or open the sealed holdout. Any live-only claim remains `APPROVAL_PENDING` until a fresh, explicit bounded approval is supplied.

## Acceptance

- Each fixed slot accepts only its assigned lane; state and errors are attributable by lane/slot.
- A GPOINT/GSALES gateway failure does not prevent local LAB queue polling or degrade healthy lanes by aggregation alone.
- LAB missing/expired approval causes no inference; exact model/auth/budget/no-retry and receipt rules remain intact.
- Offline tests prove fixed binding, isolation, local LAB independence, crash/error recovery, N=1 serialization, and N>1 only with provider capability verified.
- Full guarded Conversation Lab and Worker regression suites pass; no live call occurs.
- Documentation distinguishes implemented, installed, verified, and approval-pending states.
