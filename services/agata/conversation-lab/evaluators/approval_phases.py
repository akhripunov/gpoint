"""Fail-closed local phase state for sequential LAB generator and judge approvals."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Mapping

import lab_queue


class PhaseApprovalState:
    """Track one run's non-overlapping Phase A/Phase B approval transition.

    This records phase state only. It never creates an OWNER approval file or
    enables inference; the Worker still verifies the separately approved file.
    """
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict | None:
        if not self.path.exists():
            return None
        if (self.path.is_symlink() or not self.path.is_file()
                or self.path.stat().st_uid != os.getuid() or self.path.stat().st_mode & 0o077):
            raise PermissionError("LAB phase-state file permissions are unsafe")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") not in {1, 2}:
            raise PermissionError("LAB phase-state receipt is invalid")
        return value

    def _write(self, value: dict) -> None:
        parent = self.path.parent
        if (not parent.is_dir() or parent.is_symlink()
                or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077):
            raise PermissionError("LAB phase-state directory permissions are unsafe")
        fd, temporary_name = tempfile.mkstemp(prefix=".lab-phase-", dir=parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def begin_generator(self, *, run_id: str, scenario_id: str, worker_bundle_sha256: str,
                        approval_id: str, max_calls: int, authorization_sha256: str | None = None) -> None:
        if (not lab_queue.ID_RE.fullmatch(run_id) or not lab_queue.ID_RE.fullmatch(scenario_id)
                or not lab_queue.ID_RE.fullmatch(approval_id)
                or not isinstance(worker_bundle_sha256, str) or len(worker_bundle_sha256) != 64
                or max_calls != 8 or self._read() is not None):
            raise PermissionError("Phase A approval must be a fresh, exact eight-call run scope")
        if authorization_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", authorization_sha256):
            raise PermissionError("Combined run authorization fingerprint is invalid")
        self._write({
            "schema_version": 2 if authorization_sha256 is not None else 1,
            "run_id": run_id, "scenario_id": scenario_id,
            "worker_bundle_sha256": worker_bundle_sha256, "active_phase": "GENERATOR",
            "generator_approval_id": approval_id, "generator_max_calls": max_calls,
            **({"authorization_id": approval_id, "authorization_sha256": authorization_sha256}
               if authorization_sha256 is not None else {}),
            "generator_closed": False, "generator_receipts": [],
        })

    def assert_generator_active(self, *, run_id: str, scenario_id: str,
                                worker_bundle_sha256: str, approval_id: str,
                                authorization_sha256: str | None = None) -> None:
        state = self._read()
        if (state is None or state.get("active_phase") != "GENERATOR"
                or state.get("run_id") != run_id or state.get("scenario_id") != scenario_id
                or state.get("worker_bundle_sha256") != worker_bundle_sha256
                or state.get("generator_approval_id") != approval_id
                or state.get("generator_max_calls") != 8
                or (state.get("schema_version") == 2 and (
                    state.get("authorization_id") != approval_id
                    or state.get("authorization_sha256") != authorization_sha256))):
            raise PermissionError("Phase A approval state is not the active exact run scope")

    def close_generator(self, receipts: list[Mapping[str, object]], *, call_ledger_snapshot: Mapping[str, object],
                        promptfoo_receipts: list[Mapping[str, object]]) -> None:
        state = self._read()
        if (state is None or state.get("active_phase") != "GENERATOR"
                or state.get("generator_closed") is not False or not isinstance(receipts, list)
                or len(receipts) != 8 or len(promptfoo_receipts) != 8):
            raise PermissionError("Phase A can close only after eight successful Worker and Promptfoo receipts")
        generator = call_ledger_snapshot.get("generator_invocations") if isinstance(call_ledger_snapshot, Mapping) else None
        ledger_rows = generator.get("invocations") if isinstance(generator, Mapping) else None
        if (not isinstance(generator, Mapping) or generator.get("attempted") != 8
                or generator.get("outcomes") != {"success": 8} or not isinstance(ledger_rows, list)
                or len(ledger_rows) != 8):
            raise PermissionError("Phase A ledger must show exactly eight successful generator attempts")
        ledger_by_id = {row.get("invocation_id"): row for row in ledger_rows if isinstance(row, Mapping)}
        promptfoo_by_id = {row.get("promptfoo_invocation_id"): row for row in promptfoo_receipts
                           if isinstance(row, Mapping)}
        if len(ledger_by_id) != 8 or len(promptfoo_by_id) != 8:
            raise PermissionError("Phase A receipts contain duplicate invocation identities")
        rows = []
        identities = set()
        for receipt in receipts:
            identity = receipt.get("identity") if isinstance(receipt, Mapping) else None
            job_id = receipt.get("job_id") if isinstance(receipt, Mapping) else None
            if (not isinstance(identity, Mapping) or receipt.get("status") != "completed"
                    or receipt.get("slot_id") != "slot-03"
                    or not isinstance(receipt.get("response"), str) or not receipt["response"].strip()
                    or identity.get("experiment_id") != "agata-v2-naturalness-dev"
                    or identity.get("job_type") != "v2-dev-generator"
                    or identity.get("dataset_split") != "DEV"
                    or identity.get("run_id") != state["run_id"]
                    or identity.get("scenario_id") != state["scenario_id"]
                    or identity.get("configuration_id") not in {"baseline", "naturalness-v1"}
                    or type(identity.get("turn_index")) is not int
                    or identity["turn_index"] not in range(4)
                    or not isinstance(job_id, str) or not lab_queue.ID_RE.fullmatch(job_id)):
                raise PermissionError("Phase A receipt failed exact run, slot, turn, or success verification")
            key = (identity["configuration_id"], identity["turn_index"])
            if key in identities:
                raise PermissionError("Phase A contains a duplicate generator receipt")
            identities.add(key)
            invocation_id = identity.get("promptfoo_invocation_id")
            ledger_row = ledger_by_id.get(invocation_id)
            cli_row = promptfoo_by_id.get(invocation_id)
            ledger_meta = ledger_row.get("metadata", {}) if isinstance(ledger_row, Mapping) else {}
            if (ledger_row is None or ledger_row.get("outcome") != "success"
                    or ledger_meta.get("run_id") != state["run_id"]
                    or ledger_meta.get("scenario_id") != state["scenario_id"]
                    or ledger_meta.get("configuration") != identity.get("configuration_id")
                    or ledger_meta.get("turn_index") != identity.get("turn_index")
                    or ledger_meta.get("worker_job_id") != job_id
                    or cli_row is None or cli_row.get("promptfoo_success") is not True
                    or cli_row.get("result_count") != 1 or cli_row.get("run_id") != state["run_id"]
                    or cli_row.get("worker_job_id") != job_id):
                raise PermissionError("Phase A Promptfoo, ledger, and Worker evidence do not correlate")
            rows.append({"job_id": job_id, "receipt_sha256": hashlib.sha256(
                json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(), "configuration_id": identity["configuration_id"],
                         "turn_index": identity["turn_index"],
                         "promptfoo_invocation_id": invocation_id,
                         "promptfoo_receipt_sha256": hashlib.sha256(json.dumps(
                             cli_row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                         ).encode()).hexdigest()})
        expected = {(arm, turn) for arm in ("baseline", "naturalness-v1") for turn in range(4)}
        if identities != expected:
            raise PermissionError("Phase A must contain all four sequential turns for both arms")
        state["active_phase"] = None
        state["generator_closed"] = True
        state["generator_success_count"] = 8
        state["generator_receipts"] = sorted(rows, key=lambda row: (row["configuration_id"], row["turn_index"]))
        self._write(state)

    def begin_judges(self, *, run_id: str, scenario_id: str, worker_bundle_sha256: str,
                     approval_id: str, max_calls: int,
                     generator_receipts: list[Mapping[str, object]]) -> None:
        state = self._read()
        combined = state is not None and state.get("schema_version") == 2
        if (state is None or state.get("active_phase") is not None
                or state.get("generator_closed") is not True or state.get("run_id") != run_id
                or state.get("scenario_id") != scenario_id
                or state.get("worker_bundle_sha256") != worker_bundle_sha256
                or (approval_id != state.get("generator_approval_id") if combined
                    else approval_id == state.get("generator_approval_id"))
                or not lab_queue.ID_RE.fullmatch(approval_id) or max_calls != 3
                or len(generator_receipts) != 8):
            raise PermissionError("Phase B requires closed, verified Phase A and the scoped three-call authorization")
        supplied = {row.get("job_id"): hashlib.sha256(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest() for row in generator_receipts if isinstance(row, Mapping)}
        expected = {row["job_id"]: row["receipt_sha256"] for row in state["generator_receipts"]}
        if supplied != expected:
            raise PermissionError("Phase B generator receipt set does not match closed Phase A")
        if combined:
            if state.get("authorization_id") != approval_id:
                raise PermissionError("Phase B must use the same immutable run authorization")
        state.update({"active_phase": "JUDGES", "judge_approval_id": approval_id, "judge_max_calls": 3})
        self._write(state)

    def complete_run(self, *, judge_invocations: int, outcome_sha256: str) -> None:
        state = self._read()
        if (state is None or state.get("schema_version") != 2 or state.get("active_phase") != "JUDGES"
                or state.get("generator_closed") is not True or judge_invocations != 3
                or not isinstance(outcome_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", outcome_sha256)):
            raise PermissionError("combined LIVE run cannot complete without Phase A and all three judge calls")
        state.update({"active_phase": None, "terminal_status": "COMPLETED",
                      "judge_invocations": 3, "outcome_sha256": outcome_sha256})
        self._write(state)

    def fail_run(self, *, stage: str, reason_code: str) -> None:
        state = self._read()
        if (state is None or state.get("schema_version") != 2
                or stage not in {"GENERATOR", "PHASE_A_VERIFY", "BLIND_PACKET", "JUDGES"}
                or not isinstance(reason_code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", reason_code)):
            raise PermissionError("combined LIVE run failure state is invalid")
        if state.get("terminal_status") is not None:
            return
        state.update({"active_phase": None, "terminal_status": "FAILED",
                      "failure": {"stage": stage, "reason_code": reason_code}})
        self._write(state)
