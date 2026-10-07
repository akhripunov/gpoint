"""Atomic private receipts and rubric-bound OWNER lock/unblind helpers for Lab V2."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import copy
from pathlib import Path
from typing import Mapping


MODEL = "gpt-6-luna"
REASONING = "medium"
CALL_LIMITS = {"conversation_arms": 8, "generator_provider_invocations": 32, "judge_provider_invocations": 15}


def fingerprint(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_sha(value, name):
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be a lowercase SHA256")


def run_identity(*, batch_id, candidate_sha256, scenario_commitments, naturalness_policy_sha256):
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("immutable batch identity is required")
    _require_sha(candidate_sha256, "candidate SHA256")
    _require_sha(naturalness_policy_sha256, "Naturalness Policy SHA256")
    if not isinstance(scenario_commitments, Mapping) or not scenario_commitments or any(not isinstance(key, str) or not key.strip() for key in scenario_commitments):
        raise ValueError("scenario commitments must be an ID-to-SHA256 mapping")
    for digest in scenario_commitments.values():
        _require_sha(digest, "scenario commitment")
    identity = {"schema_version": 2, "batch_id": batch_id, "candidate_sha256": candidate_sha256,
                "scenario_commitments": dict(sorted(scenario_commitments.items())),
                "naturalness_policy_sha256": naturalness_policy_sha256}
    return fingerprint(identity)


def build_private_receipt(*, batch_id, candidate_sha256, scenario_commitments, naturalness_policy_sha256,
                          blind_packet, rubric,
                          counters, call_ledger, private_mapping, randomization, judge_receipts, sanity_findings, aggregate_status):
    identity = run_identity(batch_id=batch_id, candidate_sha256=candidate_sha256,
                            scenario_commitments=scenario_commitments,
                            naturalness_policy_sha256=naturalness_policy_sha256)
    _validate_rubric(rubric)
    if not isinstance(counters, Mapping) or set(counters) != set(CALL_LIMITS):
        raise ValueError("three independent call counters are required")
    normalized_counters = {}
    for name, limit in CALL_LIMITS.items():
        value = counters[name]
        if type(value) is not int or not 0 <= value <= limit:
            raise ValueError(f"{name} exceeds its bounded call ledger")
        normalized_counters[name] = value
    if not isinstance(private_mapping, Mapping) or private_mapping.get("private") is not True or not isinstance(private_mapping.get("mappings"), list):
        raise ValueError("private blind arm mapping is required")
    if private_mapping.get("mapping_sha256") != fingerprint(private_mapping["mappings"]):
        raise ValueError("private blind arm mapping hash does not match")
    if (not getattr(call_ledger, "durable", False) or not callable(getattr(call_ledger, "verify_persisted", None))
            or not call_ledger.verify_persisted()):
        raise ValueError("verified append-only call-ledger journal is required")
    ledger_snapshot = call_ledger.snapshot()
    if ledger_snapshot.get("limits") != {"conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15}:
        raise ValueError("call-ledger limits differ from the approved V2 budget")
    arms = ledger_snapshot.get("conversation_arms")
    generators = ledger_snapshot.get("generator_invocations")
    judges = ledger_snapshot.get("judge_invocations")
    if any(not isinstance(row, Mapping) or not isinstance(row.get("invocations"), list) for row in (arms, generators, judges)):
        raise ValueError("call-ledger invocation evidence is incomplete")
    evidence_counts = {"conversation_arms": len(arms["invocations"]),
                       "generator_provider_invocations": len(generators["invocations"]),
                       "judge_provider_invocations": len(judges["invocations"])}
    if normalized_counters != evidence_counts:
        raise ValueError("call counters do not match durable invocation evidence")
    if not isinstance(ledger_snapshot.get("journal_sha256"), str) or len(ledger_snapshot["journal_sha256"]) != 64:
        raise ValueError("call-ledger journal hash is missing")
    for stage in (arms, generators, judges):
        invocations = stage["invocations"]
        ids = [entry.get("invocation_id") for entry in invocations]
        if len(ids) != len(set(ids)) or stage.get("attempted") != len(invocations):
            raise ValueError("call-ledger invocation IDs/counts are inconsistent")
    arm_pairs = {(entry.get("metadata", {}).get("scenario_id"), entry.get("configuration"))
                 for entry in arms["invocations"]}
    map_pairs = {(entry.get("scenario_id"), entry.get("model_id")) for entry in private_mapping["mappings"]}
    if arm_pairs != map_pairs or len(arm_pairs) != len(arms["invocations"]):
        raise ValueError("durable arm reservations do not match the private A/B mapping")
    arm_configurations = [entry.get("configuration") for entry in arms["invocations"]]
    if any(value not in {"baseline", "naturalness-v1"} for value in arm_configurations):
        raise ValueError("arm ledger has an unknown configuration")
    if len(arm_configurations) != len(private_mapping["mappings"]):
        raise ValueError("arm ledger count does not match private mapping")
    if arm_configurations.count("baseline") != arm_configurations.count("naturalness-v1"):
        raise ValueError("Baseline and Candidate arm counts must be balanced")
    if arm_configurations.count("baseline") > 4:
        raise ValueError("conversation-arm budget exceeds four per configuration")
    arm_ids = {entry.get("metadata", {}).get("arm_id") for entry in arms["invocations"]}
    generator_entries = generators["invocations"]
    turn_pairs = [(entry.get("metadata", {}).get("arm_id"), entry.get("metadata", {}).get("turn_index")) for entry in generator_entries]
    if any(arm_id not in arm_ids or type(turn_index) is not int or not 0 <= turn_index < 4 for arm_id, turn_index in turn_pairs):
        raise ValueError("generator ledger entries must map to a valid arm and one of its four turns")
    if len(turn_pairs) != len(set(turn_pairs)):
        raise ValueError("generator ledger contains a repeated turn attempt")
    for arm in arms["invocations"]:
        metadata = arm["metadata"]
        related = [entry for entry in generator_entries if entry["metadata"].get("arm_id") == metadata.get("arm_id")]
        turns = sorted(entry["metadata"]["turn_index"] for entry in related)
        if turns != list(range(len(turns))) or len(turns) > 4:
            raise ValueError("generator turns for an arm are not a unique sequential prefix")
        if arm["outcome"] == "success" and (turns != [0, 1, 2, 3] or any(entry["outcome"] != "success" for entry in related)):
            raise ValueError("completed arm must have four successful recorded generator invocations")
    for stage in (arms, generators, judges):
        if any(not isinstance(entry.get("invocation_id"), str) or not entry["invocation_id"].strip()
               or entry.get("outcome") not in {None, "success", "timeout", "quota_rejected", "provider_error", "ambiguous", "failed", "refused"}
               for entry in stage["invocations"]):
            raise ValueError("call-ledger entry is malformed")
    orders = randomization.get("x_y_orders") if isinstance(randomization, Mapping) else None
    control_id = randomization.get("owner_control_scenario_id") if isinstance(randomization, Mapping) else None
    if (not isinstance(randomization, Mapping) or type(randomization.get("seed")) is not int
            or not isinstance(control_id, str) or control_id not in scenario_commitments
            or not isinstance(orders, list)
            or any(not isinstance(order, list) or len(order) != 2 or set(order) != {"X", "Y"} for order in orders)):
        raise ValueError("persisted seed, OWNER control selection, and X/Y order are required")
    if not isinstance(judge_receipts, list) or len(judge_receipts) != normalized_counters["judge_provider_invocations"]:
        raise ValueError("judge receipts exceed their bounded budget")
    if any(not isinstance(row, Mapping) or not isinstance(row.get("invocation_id"), str) for row in judge_receipts):
        raise ValueError("each judge receipt must link to a durable judge invocation")
    judge_by_id = {row["invocation_id"]: row for row in judge_receipts}
    if len(judge_by_id) != len(judge_receipts) or set(judge_by_id) != {row["invocation_id"] for row in judges["invocations"]}:
        raise ValueError("judge receipts do not match the durable judge invocation set")
    if not isinstance(sanity_findings, list) or not isinstance(aggregate_status, str) or not aggregate_status:
        raise ValueError("sanity and deterministic aggregate status are required")
    body = {
        "schema_version": 2,
        "run_identity_sha256": identity,
        "batch_id": batch_id,
        "candidate_sha256": candidate_sha256,
        "scenario_commitments": dict(sorted(scenario_commitments.items())),
        "naturalness_policy_sha256": naturalness_policy_sha256,
        "blind_packet_sha256": fingerprint(blind_packet),
        "rubric_sha256": fingerprint(rubric),
        "requested_model": MODEL,
        "requested_reasoning": REASONING,
        "call_counters": normalized_counters,
        "call_ledger": copy.deepcopy(dict(ledger_snapshot)),
        "private_mapping": dict(private_mapping),
        "randomization": dict(randomization),
        "judge_receipts": judge_receipts,
        "sanity_findings": sanity_findings,
        "aggregate_status": aggregate_status,
        "owner_review": {"ratings_locked": False, "unblinded": False, "ratings_lock_sha256": None},
        "private": True,
    }
    body["receipt_sha256"] = fingerprint(body)
    return body


def verify_private_receipt(receipt):
    if not isinstance(receipt, Mapping) or receipt.get("private") is not True:
        return False
    body = dict(receipt)
    digest = body.pop("receipt_sha256", None)
    return isinstance(digest, str) and digest == fingerprint(body)


def _reseal(receipt):
    body = copy.deepcopy(dict(receipt))
    body.pop("receipt_sha256", None)
    body["receipt_sha256"] = fingerprint(body)
    return body


def write_private_receipt(path, receipt):
    """Atomically create a mode-0600 immutable receipt without replacing an existing run."""
    if not verify_private_receipt(receipt):
        raise ValueError("private receipt integrity check failed")
    destination = Path(path)
    if not destination.parent.is_dir():
        raise FileNotFoundError("private receipt directory must already exist")
    fd, temporary_name = tempfile.mkstemp(prefix=".agata-receipt-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
        os.chmod(destination, 0o600)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def _events_root(receipt_path):
    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    if not verify_private_receipt(receipt):
        raise ValueError("private receipt integrity check failed")
    parent = Path(receipt_path).parent / "events"
    parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)
    root = parent / receipt["receipt_sha256"]
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    return root, receipt["receipt_sha256"]


def _write_review_event(receipt_path, name, event):
    root, receipt_sha256 = _events_root(receipt_path)
    if event.get("receipt_sha256") != receipt_sha256:
        raise ValueError("review event does not belong to this private receipt")
    destination = root / name
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(event, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return destination


def load_review_events(receipt_path):
    parent = Path(receipt_path).parent / "events"
    if not parent.exists():
        return []
    root, receipt_sha256 = _events_root(receipt_path)
    events = []
    for path in sorted(root.glob("*.json")):
        event = json.loads(path.read_text(encoding="utf-8"))
        digest = event.pop("event_sha256", None)
        if (not isinstance(digest, str) or digest != fingerprint(event)
                or event.get("receipt_sha256") != receipt_sha256):
            raise ValueError("OWNER review event integrity check failed")
        event["event_sha256"] = digest
        events.append(event)
    names = [event.get("event") for event in events]
    if names not in ([], ["ratings-locked"], ["ratings-locked", "unblinded"]):
        raise ValueError("OWNER review event sequence is invalid")
    return events


def persist_owner_lock(receipt_path, packet, rubric, owner_ratings, lock):
    receipt_path = Path(receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    record_owner_lock(receipt, packet, rubric, owner_ratings, lock)
    if load_review_events(receipt_path):
        raise FileExistsError("OWNER rating lock event already exists")
    event = {"schema_version": 1, "event": "ratings-locked", "receipt_sha256": receipt["receipt_sha256"],
             "packet_sha256": fingerprint(packet), "rubric_sha256": fingerprint(rubric),
             "ratings_lock_sha256": lock["lock_sha256"]}
    event["event_sha256"] = fingerprint(event)
    return _write_review_event(receipt_path, "00-ratings-locked.json", event)


def persist_owner_unblind(receipt_path, packet, rubric, owner_ratings, lock, private_mapping):
    receipt_path = Path(receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    events = load_review_events(receipt_path)
    if len(events) != 1 or events[0].get("ratings_lock_sha256") != lock.get("lock_sha256"):
        raise ValueError("a persisted exact OWNER ratings lock is required before unblind")
    _, revealed = record_owner_unblind(
        receipt, packet, rubric, owner_ratings, lock, private_mapping, receipt_path=receipt_path)
    event = {"schema_version": 1, "event": "unblinded", "receipt_sha256": receipt["receipt_sha256"],
             "ratings_lock_sha256": lock["lock_sha256"], "unblind_result_sha256": fingerprint(revealed)}
    event["event_sha256"] = fingerprint(event)
    _write_review_event(receipt_path, "01-unblinded.json", event)
    return revealed


def _load_ratings():
    path = Path(__file__).with_name("ratings.py")
    spec = importlib.util.spec_from_file_location("agata_receipt_ratings", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("OWNER ratings validator unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_rubric(rubric):
    path = Path(__file__).with_name("judges.py")
    spec = importlib.util.spec_from_file_location("agata_receipt_judges", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("OWNER rubric validator unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.validate_rubric(rubric)


def lock_owner_ratings(packet, rubric, owner_ratings, *, cycle_id):
    _validate_rubric(rubric)
    ratings = _load_ratings()
    return ratings.lock_ratings(packet, owner_ratings, cycle_id=cycle_id, rubric=rubric)


def record_owner_lock(receipt, packet, rubric, owner_ratings, lock):
    if not verify_private_receipt(receipt):
        raise ValueError("private run receipt integrity check failed")
    if not verify_owner_lock(packet, rubric, owner_ratings, lock):
        raise ValueError("OWNER lock does not match the exact packet, rubric, and ratings")
    if receipt.get("blind_packet_sha256") != fingerprint(packet) or receipt.get("rubric_sha256") != fingerprint(rubric):
        raise ValueError("OWNER lock packet/rubric differ from the private run receipt")
    result = copy.deepcopy(dict(receipt))
    review = result.get("owner_review", {})
    if review.get("ratings_locked") is True or review.get("unblinded") is True:
        raise ValueError("OWNER review state is write-once")
    result["owner_review"] = {"ratings_locked": True, "unblinded": False, "ratings_lock_sha256": lock["lock_sha256"]}
    return _reseal(result)


def verify_owner_lock(packet, rubric, owner_ratings, lock):
    _validate_rubric(rubric)
    ratings = _load_ratings()
    return ratings.verify_lock(lock, packet, owner_ratings, rubric=rubric)


def unblind_owner_ratings(packet, rubric, owner_ratings, lock, private_mapping, *, receipt_path):
    events = load_review_events(receipt_path)
    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    if len(events) != 1 or not _verify_lock_event(events[0], lock, receipt["receipt_sha256"]):
        raise ValueError("persisted OWNER rating-lock event for this receipt is required before unblind")
    _validate_rubric(rubric)
    ratings = _load_ratings()
    if not ratings.verify_lock(lock, packet, owner_ratings, rubric=rubric):
        raise ValueError("packet, rubric, and complete OWNER ratings must be locked before unblinding")
    if not isinstance(private_mapping, Mapping) or private_mapping.get("private") is not True:
        raise ValueError("private arm mapping required")
    return ratings.unblind(packet, lock, owner_ratings, private_mapping, rubric=rubric)


def record_owner_unblind(receipt, packet, rubric, owner_ratings, lock, private_mapping, *, receipt_path):
    if not verify_private_receipt(receipt):
        raise ValueError("private run receipt integrity check failed")
    if receipt.get("blind_packet_sha256") != fingerprint(packet) or receipt.get("rubric_sha256") != fingerprint(rubric):
        raise ValueError("unblind packet/rubric differ from the private run receipt")
    persisted_receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    if not verify_private_receipt(persisted_receipt) or persisted_receipt.get("receipt_sha256") != receipt.get("receipt_sha256"):
        raise ValueError("unblind receipt does not match the persisted private receipt")
    events = load_review_events(receipt_path)
    if len(events) != 1 or not _verify_lock_event(events[0], lock, receipt["receipt_sha256"]):
        raise ValueError("persisted OWNER rating-lock event for this receipt is required before unblind")
    revealed = unblind_owner_ratings(packet, rubric, owner_ratings, lock, private_mapping,
                                     receipt_path=receipt_path)
    result = copy.deepcopy(dict(receipt))
    result["owner_review"] = {"ratings_locked": True, "unblinded": True,
                               "ratings_lock_sha256": lock["lock_sha256"]}
    result["unblind_result_sha256"] = fingerprint(revealed)
    return _reseal(result), revealed


def _verify_lock_event(event, lock, receipt_sha256):
    if (not isinstance(event, Mapping) or event.get("event") != "ratings-locked"
            or event.get("receipt_sha256") != receipt_sha256
            or event.get("ratings_lock_sha256") != lock.get("lock_sha256")):
        return False
    body = dict(event)
    digest = body.pop("event_sha256", None)
    return isinstance(digest, str) and digest == fingerprint(body)
