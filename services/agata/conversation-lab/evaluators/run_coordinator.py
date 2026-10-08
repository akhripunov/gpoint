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
_EVALUATORS_DIR = _LAB / "evaluators"
if str(_EVALUATORS_DIR) not in sys.path:
    sys.path.insert(0, str(_EVALUATORS_DIR))
from preinference_diagnostics import write_preinference_failure


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


V2_BASE_INSTRUCTIONS = (
    "You are AGATA, a conversational character. Respond in the scenario language. "
    "Use only the supplied Persona, context, memory, and this arm history. "
    "Do not invent facts. Follow safety and privacy boundaries."
)


def _naturalness_treatment() -> str:
    path = _LAB / "configs" / "naturalness-policy-v1.md"
    text = path.read_text(encoding="utf-8")
    marker = "## Candidate-visible policy\n"
    if marker not in text:
        raise PermissionError("Naturalness V1 candidate policy section is missing")
    treatment = text.split(marker, 1)[1].split("\n## Evidence map", 1)[0].strip()
    if not treatment:
        raise PermissionError("Naturalness V1 candidate treatment is empty")
    worker_dir = str(_LAB.parents[0] / "worker")
    if worker_dir not in sys.path:
        sys.path.insert(0, worker_dir)
    import lab_queue
    if hashlib.sha256(treatment.encode("utf-8")).hexdigest() != lab_queue.read_v2_dev_manifest().get("naturalness_policy_sha256"):
        raise PermissionError("Naturalness V1 policy does not match the Worker-bundled V2 treatment commitment")
    return treatment


def build_v2_dev_request(scenario: Mapping[str, object], configuration_id: str,
                         turn_index: int, history: list[Mapping[str, object]]) -> dict:
    """Build a Worker request only from an exact committed V2 DEV fixture."""
    from v2_provenance import validate_source_scenarios
    validate_source_scenarios([dict(scenario)])
    if configuration_id not in {"baseline", "naturalness-v1"}:
        raise PermissionError("V2 generator configuration is outside the frozen pair")
    users = _user_turns(scenario)
    if type(turn_index) is not int or turn_index not in range(4) or len(history) != turn_index * 2:
        raise ValueError("V2 generator turn/history boundary is invalid")
    request_history = []
    for index, row in enumerate(history):
        if not isinstance(row, Mapping) or row.get("role") not in {"user", "assistant"}:
            raise ValueError("V2 history has an invalid role")
        expected_role = "user" if index % 2 == 0 else "assistant"
        if row.get("role") != expected_role or not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError("V2 history is not a complete independent arm transcript")
        request_history.append({"role": expected_role, "content": row["text"]})
    character_context = scenario.get("character_context")
    if not isinstance(character_context, Mapping) or character_context.get("persona_ref") != "agata-v2-canonical":
        raise PermissionError("V2 DEV fixture does not use the canonical AGATA Persona")
    instructions = V2_BASE_INSTRUCTIONS
    if configuration_id == "naturalness-v1":
        instructions += "\n\nNaturalness V1 policy:\n" + _naturalness_treatment()
    context = scenario.get("context")
    memories = scenario.get("memory_fixture")
    if not isinstance(context, Mapping) or not isinstance(memories, list):
        raise PermissionError("V2 DEV context/memory fixture is malformed")
    return {
        "instructions": instructions,
        "persona": {"persona_ref": character_context["persona_ref"], "gender": "female"},
        "communication_settings": {"language": scenario.get("language"), "user_gender": scenario.get("user_gender")},
        "memories": copy.deepcopy(memories),
        "dialogue_summaries": [],
        "history": request_history,
        "user_message": users[turn_index],
    }


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


def run_v2_dev_scenario(
    scenario_id: str, *, run_id: str, ledger, promptfoo_executable: str,
    worker_queue: Path, private_receipt_directory: Path, env: Mapping[str, str],
    phase_state,
    eval_runner=None,
) -> list[dict]:
    """Run the fixed two-arm/four-turn V2 DEV pair through eight Promptfoo evals."""
    import lab_queue
    import promptfoo_runner
    import worker_client
    from v2_provenance import load_split

    diagnostic_invocation = ("pfv2-" + hashlib.sha256(
        f"{run_id}\0baseline\0{0}".encode()).hexdigest()[:59]) if isinstance(run_id, str) else ""
    def checked(function, stage, code, location):
        try:
            return function()
        except BaseException as exc:
            try:
                write_preinference_failure(
                    private_receipt_directory, invocation_id=diagnostic_invocation,
                    stage=stage, reason_code=code, exception=exc, source_location=location,
                )
            except (OSError, PermissionError, ValueError, FileExistsError):
                pass
            raise

    if not isinstance(run_id, str) or not lab_queue.ID_RE.fullmatch(run_id):
        raise ValueError("V2 run ID must be immutable and Worker-safe")
    _manifest, committed = checked(lambda: load_split("DEV"),
                                   "IMMUTABLE_V2_JOB_CONTRACT", "DEV_MANIFEST_INVALID",
                                   "run_coordinator.run_v2_dev_scenario")
    scenario = committed.get(scenario_id)
    if scenario is None:
        checked(lambda: (_ for _ in ()).throw(PermissionError("scenario is outside committed DEV")),
                "IMMUTABLE_V2_JOB_CONTRACT", "SCENARIO_NOT_IN_DEV_SPLIT",
                "run_coordinator.run_v2_dev_scenario")
    queue = checked(lambda: worker_client.canonical_lab_queue(env), "LAB_QUEUE_VALIDATION",
                    "CANONICAL_LAB_QUEUE_INVALID", "run_coordinator.run_v2_dev_scenario")
    if Path(worker_queue).resolve(strict=False) != queue.resolve(strict=False):
        checked(lambda: (_ for _ in ()).throw(PermissionError("noncanonical queue")),
                "LAB_QUEUE_VALIDATION", "CANONICAL_LAB_QUEUE_MISMATCH",
                "run_coordinator.run_v2_dev_scenario")
    approval_path = Path(env.get("AGATA_LAB_APPROVAL_FILE") or
                         (queue.parents[1] / "config" / "lab-execution-approval.json"))
    approval = checked(lambda: lab_queue.load_owner_approval(
        approval_path, expected_bundle_sha256=lab_queue.worker_bundle_sha256(),
    ), "APPROVAL_VALIDATION", "OWNER_APPROVAL_UNAVAILABLE", "run_coordinator.run_v2_dev_scenario")
    def validate_approval_scope():
        if (approval.get("phase") != "GENERATOR" or approval.get("run_id") != run_id
                or approval.get("max_calls") != 8
                or set(approval.get("experiment_ids", [])) != {"agata-v2-naturalness-dev"}
                or set(approval.get("scenario_ids", [])) != {scenario_id}
                or set(approval.get("configuration_ids", [])) != {"baseline", "naturalness-v1"}):
            raise PermissionError("Phase A approval scope does not match this V2 run")
    checked(validate_approval_scope, "APPROVAL_VALIDATION", "OWNER_APPROVAL_SCOPE_MISMATCH",
            "run_coordinator.run_v2_dev_scenario")
    checked(lambda: phase_state.assert_generator_active(
        run_id=run_id, scenario_id=scenario_id,
        worker_bundle_sha256=lab_queue.worker_bundle_sha256(), approval_id=approval["approval_id"],
    ), "PHASE_STATE_VALIDATION", "PHASE_A_NOT_ACTIVE",
       "run_coordinator.run_v2_dev_scenario")
    def validate_ledger():
        if (not ledger.durable or not ledger.verify_persisted()
                or ledger.snapshot()["limits"] != {
                    "conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15}):
            raise PermissionError("V2 run requires the verified durable Conversation Lab ledger")
    checked(validate_ledger, "GENERATOR_RESERVATION", "DURABLE_LEDGER_INVALID",
            "run_coordinator.run_v2_dev_scenario")
    fixtures = {
        "persona": {"persona_ref": scenario["character_context"]["persona_ref"], "gender": "female"},
        "context": scenario["context"], "memory": scenario["memory_fixture"],
    }
    arms = checked(lambda: compile_arms(scenario, fixtures, _naturalness_treatment(), run_id=run_id),
                   "PROVIDER_CONFIG", "V2_ARM_CONFIGURATION_INVALID",
                   "run_coordinator.run_v2_dev_scenario")
    results = []
    for arm in arms:
        private = arm["private"]
        configuration_id = private["configuration_id"]
        arm_id = f"{run_id}-{configuration_id}"
        diagnostic_invocation = "pfv2-" + hashlib.sha256(
            f"{run_id}\0{configuration_id}\0{0}".encode()).hexdigest()[:59]
        private["arm_id"] = arm_id
        arm_ledger_id = "arm-" + hashlib.sha256(arm_id.encode()).hexdigest()[:60]
        checked(lambda: ledger.reserve("arm", arm_ledger_id, configuration=configuration_id,
                                       metadata={"arm_id": arm_id, "scenario_id": scenario_id,
                                                 "run_id": run_id, "dataset_split": "DEV"}),
                "GENERATOR_RESERVATION", "ARM_RESERVATION_FAILED",
                "run_coordinator.run_v2_dev_scenario")
        worker_receipt_ids = []
        promptfoo_invocation_ids = []
        for turn_index in range(4):
            diagnostic_invocation = "pfv2-" + hashlib.sha256(
                f"{run_id}\0{configuration_id}\0{turn_index}".encode()).hexdigest()[:59]
            request = checked(lambda: build_v2_dev_request(scenario, configuration_id, turn_index, arm["history"]),
                              "IMMUTABLE_V2_JOB_CONTRACT", "V2_REQUEST_CONTRACT_INVALID",
                              "run_coordinator.run_v2_dev_scenario")
            invocation_id = "pfv2-" + hashlib.sha256(
                f"{run_id}\0{configuration_id}\0{turn_index}".encode()).hexdigest()[:59]
            private["pending_turn"] = turn_index
            kwargs = {
                "request": request, "run_id": run_id, "invocation_id": invocation_id,
                "arm_id": arm_id, "configuration_id": configuration_id,
                "scenario_id": scenario_id, "turn_index": turn_index,
                "history_receipt_ids": list(worker_receipt_ids),
                "promptfoo_executable": promptfoo_executable, "worker_queue": queue,
                "private_receipt_directory": Path(private_receipt_directory),
                "call_ledger": ledger, "env": env,
            }
            if eval_runner is not None:
                result = eval_runner(**kwargs)
            else:
                result = promptfoo_runner.run_promptfoo_v2_dev_eval(**kwargs)
            if not isinstance(result, Mapping) or not isinstance(result.get("output"), str):
                raise RuntimeError("Promptfoo did not return one Worker-backed V2 generator result")
            evidence = result.get("worker_receipt")
            if not isinstance(evidence, Mapping) or not isinstance(evidence.get("worker_job_id"), str):
                raise RuntimeError("V2 Promptfoo result is missing its exact Worker receipt identity")
            worker_receipt_ids.append(evidence["worker_job_id"])
            promptfoo_invocation_ids.append(invocation_id)
            record_assistant_reply(arm, turn_index, result["output"])
        ledger.complete("arm", arm_ledger_id, "success")
        results.append({
            "scenario_id": scenario_id, "model_id": configuration_id, "status": "success",
            "turns": [dict(row) for row in arm["history"]],
            "worker_receipt_ids": list(worker_receipt_ids),
            "promptfoo_invocation_ids": list(promptfoo_invocation_ids),
        })
    return results


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

    def refresh(self):
        """Reload durable events written by a Promptfoo provider child process."""
        lock_fd = self._refresh_under_lock()
        try:
            return self.snapshot()
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def verify_persisted(self):
        if not self.durable or self.journal_path is None or not self.journal_path.is_file():
            return False
        if self.journal_path.stat().st_mode & 0o777 != 0o600:
            return False
        recovered = CallLedger(*[self.limits[key] for key in ("arm", "generator", "judge")], journal_path=self.journal_path)
        current, saved = self.snapshot(), recovered.snapshot()
        return current.get("limits") == {"conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15} and all(current.get(key) == saved.get(key) for key in (
            "conversation_arms", "generator_invocations", "judge_invocations", "journal_sha256"))
