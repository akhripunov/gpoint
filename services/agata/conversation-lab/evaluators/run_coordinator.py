"""AGATA-owned parity, independent-history, and provider-call coordination."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Mapping


_LAB = Path(__file__).resolve().parents[1]
_RUNNER_PATH = _LAB / "evaluators" / "promptfoo_runner.py"


def _promptfoo_runner():
    folder = str(_LAB / "evaluators")
    if folder not in sys.path:
        sys.path.insert(0, folder)
    spec = importlib.util.spec_from_file_location("agata_run_coordinator_promptfoo_runner", _RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError("Promptfoo invocation contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _user_turns(scenario: Mapping[str, object]) -> list[str]:
    turns = scenario.get("dialogue_turns")
    if not isinstance(turns, list) or len(turns) != 8:
        raise ValueError("bounded V2 arms require exactly four user/assistant turn pairs")
    users = []
    for index, turn in enumerate(turns):
        expected = "user" if index % 2 == 0 else "assistant"
        if not isinstance(turn, Mapping) or turn.get("role") != expected:
            raise ValueError("scenario turns must alternate user then assistant response slot")
        if expected == "user":
            text = turn.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("scenario user turns must contain non-empty text")
            users.append(text)
        elif turn.get("response_slot") is not True:
            raise ValueError("assistant turns must be empty response slots, not source text")
    return users


def compile_arms(scenario, fixtures, treatment, *, run_id):
    """Compile an anonymous pair with equal initial fixtures and private mapping.

    Scenario and fixtures are caller-supplied, allowing V2 development paths to
    avoid legacy frozen-pilot loaders and their compromised -01 data.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("immutable run_id is required")
    if not isinstance(scenario, Mapping) or not isinstance(fixtures, Mapping):
        raise TypeError("scenario and fixtures must be mappings")
    if not isinstance(treatment, (str, Mapping)) or not treatment:
        raise ValueError("Naturalness treatment must be non-empty")
    users = _user_turns(scenario)
    if not isinstance(scenario.get("scenario_id"), str) or not scenario["scenario_id"].strip():
        raise ValueError("scenario_id is required")
    if not isinstance(fixtures.get("persona"), Mapping) or not isinstance(fixtures.get("context"), Mapping):
        raise ValueError("fixed Persona and context fixtures are required")
    if not isinstance(fixtures.get("memory"), list):
        raise ValueError("fixed memory fixture must be a list")

    shared = {
        "scenario_id": scenario["scenario_id"],
        "scenario_sha256": stable_hash(scenario),
        "language": scenario.get("language", "ru"),
        "persona": copy.deepcopy(fixtures["persona"]),
        "context": copy.deepcopy(fixtures["context"]),
        "memory": copy.deepcopy(fixtures["memory"]),
        "user_turns": copy.deepcopy(users),
    }
    arms = []
    for configuration, arm_label in (("baseline", "A"), ("naturalness-v1", "B")):
        model_input = copy.deepcopy(shared)
        model_input["treatment"] = copy.deepcopy(treatment) if configuration == "naturalness-v1" else None
        arms.append({
            "private": {
                "run_id": run_id,
                "scenario_id": scenario["scenario_id"],
                "arm_id": f"{run_id}:{arm_label}",
                "configuration_id": configuration,
                "next_turn": 0,
                "pending_turn": None,
            },
            "model_input": model_input,
            "history": [],
        })
    return arms


def build_turn_request(arm, turn_index, *, working_dir):
    """Build exactly one Promptfoo provider request for the next arm turn."""
    if not isinstance(arm, dict) or set(arm) != {"private", "model_input", "history"}:
        raise ValueError("invalid private arm structure")
    private, model_input, history = arm["private"], arm["model_input"], arm["history"]
    if private.get("configuration_id") not in {"baseline", "naturalness-v1"}:
        raise ValueError("arm mapping is invalid")
    if type(turn_index) is not int or turn_index != private.get("next_turn") or not 0 <= turn_index < 4:
        raise ValueError("turn must be the next unattempted turn in the four-turn arm")
    if private.get("pending_turn") is not None:
        raise ValueError("an invocation is already pending; retries are forbidden")
    if not isinstance(history, list) or len(history) != turn_index * 2:
        raise ValueError("arm history is not at the expected turn boundary")
    user_turns = model_input["user_turns"]
    for index in range(turn_index):
        expected = (
            {"role": "user", "text": user_turns[index]},
            {"role": "assistant", "text": history[index * 2 + 1].get("text") if isinstance(history[index * 2 + 1], Mapping) else None},
        )
        if history[index * 2] != expected[0] or history[index * 2 + 1] != expected[1]:
            raise ValueError("arm history does not match its own prior turns")
        if not isinstance(expected[1]["text"], str) or not expected[1]["text"].strip():
            raise ValueError("arm history contains an empty assistant reply")

    prompt_payload = {
        "persona": model_input["persona"],
        "context": model_input["context"],
        "memory": model_input["memory"],
        "prior_history_for_this_arm_only": copy.deepcopy(history),
        "current_user_turn": user_turns[turn_index],
        "naturalness_treatment": model_input["treatment"],
    }
    prompt = (
        "Respond as the fixed synthetic AGATA Persona to the current user turn. "
        "Use only the provided context, memory, and this arm's history. Do not invent facts.\n"
        + json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    request = _promptfoo_runner().build_provider_request(prompt, working_dir=working_dir)
    private["pending_turn"] = turn_index
    return request


def record_assistant_reply(arm, turn_index, reply):
    """Advance only the selected arm after one unambiguous provider result."""
    if not isinstance(arm, dict) or not isinstance(reply, str) or not reply.strip():
        raise ValueError("a non-empty assistant reply is required")
    private, history = arm["private"], arm["history"]
    if private.get("pending_turn") != turn_index or private.get("next_turn") != turn_index:
        raise ValueError("assistant reply has no matching pending invocation")
    user_text = arm["model_input"]["user_turns"][turn_index]
    history.extend([{"role": "user", "text": user_text}, {"role": "assistant", "text": reply}])
    private["next_turn"] += 1
    private["pending_turn"] = None


class CallLedger:
    """Independent, fail-closed ceilings for arms, generator calls, and judges."""
    _STAGES = {"arm": "conversation_arms", "generator": "generator_invocations", "judge": "judge_invocations"}
    _OUTCOMES = {"success", "timeout", "quota_rejected", "provider_error", "ambiguous", "failed", "refused"}
    APPROVED_LIMITS = {"arm": 8, "generator": 32, "judge": 15}

    def __init__(self, max_arms=8, max_generator_invocations=32, max_judge_invocations=15, *, journal_path=None):
        if any(type(limit) is not int or limit < 0 for limit in (max_arms, max_generator_invocations, max_judge_invocations)):
            raise ValueError("call ceilings must be non-negative integers")
        self.limits = {"arm": max_arms, "generator": max_generator_invocations, "judge": max_judge_invocations}
        self._entries = {stage: {} for stage in self._STAGES}
        self._by_configuration = {"baseline": 0, "naturalness-v1": 0}
        self.journal_path = Path(journal_path) if journal_path is not None else None
        self.durable = self.journal_path is not None
        if self.journal_path is not None:
            if not self.journal_path.parent.is_dir():
                raise FileNotFoundError("private call-ledger directory must already exist")
            if not self.journal_path.exists():
                fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            self._recover_journal()

    def _append_event(self, event):
        if self.journal_path is None:
            return
        if not self.journal_path.parent.is_dir():
            raise FileNotFoundError("private call-ledger directory must already exist")
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "ab", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)

    def _recover_journal(self):
        if not self.journal_path.exists():
            return
        os.chmod(self.journal_path, 0o600)
        for raw in self.journal_path.read_bytes().splitlines():
            try:
                event = json.loads(raw)
                stage, invocation_id = event["stage"], event["invocation_id"]
                if event["event"] == "reserve":
                    self._apply_reserve(stage, invocation_id, event.get("configuration"), event.get("metadata"), recovering=True)
                elif event["event"] == "complete":
                    if stage not in self._STAGES or invocation_id not in self._entries[stage]:
                        raise ValueError("completion without reservation")
                    entry = self._entries[stage][invocation_id]
                    if entry["outcome"] is not None or event.get("outcome") not in self._OUTCOMES:
                        raise ValueError("duplicate/invalid completion")
                    entry["outcome"] = event["outcome"]
                else:
                    raise ValueError("unknown journal event")
            except (KeyError, TypeError, json.JSONDecodeError, OverflowError) as exc:
                raise ValueError("call-ledger journal is corrupt; live execution is disabled") from exc

    def _refresh_under_lock(self):
        if not self.durable:
            return None
        lock_path = self.journal_path.with_name(self.journal_path.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self._entries = {stage: {} for stage in self._STAGES}
        self._by_configuration = {"baseline": 0, "naturalness-v1": 0}
        self._recover_journal()
        return fd

    def _apply_reserve(self, stage, invocation_id, configuration, metadata, *, recovering=False):
        if stage not in self._STAGES or not isinstance(invocation_id, str) or not invocation_id.strip():
            raise ValueError("invalid ledger reservation")
        entries = self._entries[stage]
        if invocation_id in entries or len(entries) >= self.limits[stage]:
            raise OverflowError("call ledger duplicate or budget exhausted")
        metadata = dict(metadata or {})
        if stage == "arm":
            if configuration not in self._by_configuration:
                raise ValueError("arm reservations require baseline or naturalness-v1 configuration")
            if self._by_configuration[configuration] >= self.limits[stage] // 2:
                raise OverflowError(f"{configuration} conversation-arm budget exhausted")
            arm_id = metadata.get("arm_id")
            scenario_id = metadata.get("scenario_id")
            if not isinstance(arm_id, str) or not arm_id or not isinstance(scenario_id, str) or not scenario_id:
                raise ValueError("arm reservation requires arm_id and scenario_id")
            if any(entry.get("metadata", {}).get("arm_id") == arm_id for entry in entries.values()):
                raise ValueError("arm_id already reserved; retries are forbidden")
            self._by_configuration[configuration] += 1
        elif stage == "generator":
            arm_id, turn_index = metadata.get("arm_id"), metadata.get("turn_index")
            if metadata.get("experiment_id") == "promptfoo-worker-compat":
                if (configuration is not None or arm_id != "promptfoo-worker-compat"
                        or metadata.get("scenario_id") != "promptfoo-compat-001"
                        or type(turn_index) is not int or turn_index != 0
                        or metadata.get("model") != "gpt-6-luna"
                        or metadata.get("reasoning_effort") != "medium"):
                    raise ValueError("technical compatibility invocation must match its single frozen LAB fixture")
                entries[invocation_id] = {"outcome": None, "configuration": None, "metadata": metadata}
                return
            arm = next((entry for entry in self._entries["arm"].values() if entry["metadata"].get("arm_id") == arm_id), None)
            if arm is None or type(turn_index) is not int or not 0 <= turn_index < 4:
                raise ValueError("generator reservation must bind a valid arm and turn")
            prior_entries = [entry for entry in entries.values() if entry["metadata"].get("arm_id") == arm_id]
            if any(entry["outcome"] != "success" for entry in prior_entries):
                raise ValueError("failed or ambiguous generator turn blocks continuation and retry")
            prior = sorted(entry["metadata"]["turn_index"] for entry in prior_entries)
            if prior != list(range(turn_index)) or arm["outcome"] is not None:
                raise ValueError("generator turns must be sequential and cannot retry a failed turn")
            if arm.get("configuration") != metadata.get("configuration") or arm["metadata"].get("scenario_id") != metadata.get("scenario_id"):
                raise ValueError("generator invocation does not match its reserved arm")
        elif stage == "judge":
            judge_id = metadata.get("judge_id")
            if not isinstance(judge_id, str) or not judge_id:
                raise ValueError("judge reservation requires a stable judge_id")
            if any(entry["metadata"].get("judge_id") == judge_id for entry in entries.values()):
                raise ValueError("judge_id already reserved; retries are forbidden")
            if configuration is not None:
                raise ValueError("configuration is only valid for arm reservations")
        elif configuration is not None:
            raise ValueError("configuration is only valid for arm reservations")
        entries[invocation_id] = {"outcome": None, "configuration": configuration, "metadata": metadata}

    def reserve(self, stage, invocation_id, *, configuration=None, metadata=None):
        lock_fd = self._refresh_under_lock()
        try:
            self._apply_reserve(stage, invocation_id, configuration, metadata)
            self._append_event({"event": "reserve", "stage": stage, "invocation_id": invocation_id,
                                "configuration": configuration, "metadata": dict(metadata or {})})
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def complete(self, stage, invocation_id, outcome):
        lock_fd = self._refresh_under_lock()
        try:
            if stage not in self._STAGES or invocation_id not in self._entries[stage]:
                raise ValueError("completion has no reserved invocation")
            if outcome not in self._OUTCOMES:
                raise ValueError("unsupported terminal invocation outcome")
            entry = self._entries[stage][invocation_id]
            if entry["outcome"] is not None:
                raise ValueError("invocation already has a terminal outcome")
            if stage == "arm" and outcome == "success":
                arm_id = entry["metadata"]["arm_id"]
                related = [row for row in self._entries["generator"].values() if row["metadata"].get("arm_id") == arm_id]
                turns = sorted(row["metadata"]["turn_index"] for row in related)
                if turns != [0, 1, 2, 3] or any(row["outcome"] != "success" for row in related):
                    raise ValueError("an arm can complete successfully only after four successful turns")
            self._append_event({"event": "complete", "stage": stage, "invocation_id": invocation_id, "outcome": outcome})
            entry["outcome"] = outcome
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def snapshot(self):
        result = {"limits": {
            "conversation_arms": self.limits["arm"],
            "generator_invocations": self.limits["generator"],
            "judge_invocations": self.limits["judge"],
        }}
        for stage, key in self._STAGES.items():
            entries = list(self._entries[stage].values())
            outcomes = {}
            for entry in entries:
                if entry["outcome"] is not None:
                    outcomes[entry["outcome"]] = outcomes.get(entry["outcome"], 0) + 1
            result[key] = {
                "attempted": len(entries),
                "completed": sum(entry["outcome"] is not None for entry in entries),
                "in_flight": sum(entry["outcome"] is None for entry in entries),
                "outcomes": outcomes,
                "invocations": [{**dict(entry), "metadata": dict(entry.get("metadata", {}))} for entry in entries],
            }
            for invocation_id, entry in self._entries[stage].items():
                result[key]["invocations"][list(self._entries[stage]).index(invocation_id)]["invocation_id"] = invocation_id
                result[key]["invocations"][list(self._entries[stage]).index(invocation_id)]["stage"] = stage
        result["conversation_arms"]["by_configuration"] = copy.deepcopy(self._by_configuration)
        result["durable"] = self.durable
        if self.journal_path is not None and self.journal_path.exists():
            result["journal_sha256"] = hashlib.sha256(self.journal_path.read_bytes()).hexdigest()
        return result

    def verify_persisted(self):
        if not self.durable or self.journal_path is None or not self.journal_path.is_file():
            return False
        if self.journal_path.stat().st_mode & 0o777 != 0o600:
            return False
        recovered = CallLedger(*[self.limits[key] for key in ("arm", "generator", "judge")], journal_path=self.journal_path)
        current, saved = self.snapshot(), recovered.snapshot()
        return current.get("limits") == {"conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15} and all(current.get(key) == saved.get(key) for key in (
            "conversation_arms", "generator_invocations", "judge_invocations", "journal_sha256"))
