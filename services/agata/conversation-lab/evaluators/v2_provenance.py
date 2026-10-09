"""Prove blind dialogue packets are bound to the committed V2 DEV/VALIDATION set."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Mapping

LAB = Path(__file__).resolve().parents[1]
DATA = LAB / "data/golden/synthetic"
MANIFEST = LAB / "configs/splits-v2.json"
LAB_RUN_ASSETS = (
    "evaluators/approval_phases.py", "evaluators/aggregator.py",
    "evaluators/blind.py", "evaluators/judge_coordinator.py", "evaluators/judges.py",
    "evaluators/live_e2e.py", "evaluators/live_e2e_cli.py", "evaluators/naturalness.py",
    "evaluators/persona_sanity.py", "evaluators/preinference_diagnostics.py",
    "evaluators/promptfoo_judge_provider.py", "evaluators/promptfoo_runner.py",
    "evaluators/promptfoo_worker_provider.py", "evaluators/run_coordinator.py", "evaluators/v2_provenance.py",
    "evaluators/worker_client.py", "configs/naturalness-policy-v1.md",
    "configs/owner-preference-rubric-v1.json", "configs/splits-v2.json",
    "data/golden/synthetic/v2-dev-scenarios.jsonl", "tooling/promptfoo/package.json",
    "tooling/promptfoo/package-lock.json",
)


def stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def current_source_sha() -> str:
    """Read exact source identity only when the Lab checkout has no local drift."""
    repo_root = LAB.parents[2]
    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=all", "--",
         str(LAB.relative_to(repo_root))],
        capture_output=True, text=True, timeout=3, check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise PermissionError("Conversation Lab working tree is not clean")
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "HEAD^{commit}"],
        capture_output=True, text=True, timeout=3, check=False,
    )
    source_sha = result.stdout.strip()
    if result.returncode != 0 or len(source_sha) != 40 or any(ch not in "0123456789abcdef" for ch in source_sha):
        raise PermissionError("exact Conversation Lab source identity is unavailable")
    return source_sha


def lab_run_bundle_sha256() -> str:
    """Hash only the explicit V2 LIVE run allowlist; never read legacy V1 inputs."""
    digest = hashlib.sha256()
    for relative in LAB_RUN_ASSETS:
        path = LAB / relative
        if path.is_symlink() or not path.is_file():
            raise PermissionError("V2 LIVE bundle asset is missing or unsafe")
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def live_run_context(scenario_id: str) -> dict:
    """Return current exact commitments needed to validate one LIVE authorization."""
    manifest, scenarios = load_split("DEV")
    scenario = scenarios.get(scenario_id)
    if scenario is None:
        raise PermissionError("LIVE scenario is not in committed DEV")
    worker_manifest = json.loads((LAB.parents[0] / "worker/lab_v2_dev_manifest.json").read_text(encoding="utf-8"))
    policy_path = LAB / "configs/naturalness-policy-v1.md"
    policy_text = policy_path.read_text(encoding="utf-8")
    marker = "## Candidate-visible policy\n"
    if marker not in policy_text or "\n## Evidence map" not in policy_text.split(marker, 1)[1]:
        raise PermissionError("Naturalness Policy candidate section is unavailable")
    treatment = policy_text.split(marker, 1)[1].split("\n## Evidence map", 1)[0].strip()
    if not treatment:
        raise PermissionError("Naturalness Policy candidate section is empty")
    policy_sha256 = hashlib.sha256(treatment.encode("utf-8")).hexdigest()
    if worker_manifest.get("naturalness_policy_sha256") != policy_sha256:
        raise PermissionError("Naturalness Policy differs from Worker commitment")
    rubric_path = LAB / "configs/owner-preference-rubric-v1.json"
    split_payload = MANIFEST.read_bytes()
    return {
        "source_sha": current_source_sha(),
        "lab_bundle_sha256": lab_run_bundle_sha256(),
        "naturalness_policy_sha256": policy_sha256,
        "rubric_sha256": hashlib.sha256(rubric_path.read_bytes()).hexdigest(),
        "dataset_id": manifest["dataset_id"],
        "dataset_split": "DEV",
        "dataset_manifest_sha256": hashlib.sha256(split_payload).hexdigest(),
    }


def load_split(split: str) -> tuple[dict, dict[str, dict]]:
    if split not in {"DEV", "VALIDATION"}:
        raise PermissionError("only replacement V2 DEV/VALIDATION sources are available to judges")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2:
        raise PermissionError("V2 source manifest is unavailable")
    legacy = manifest.get("legacy_holdout_v1", {})
    if (legacy.get("status") != "COMPROMISED_EXCLUDED"
            or legacy.get("optimizer_access") is not False
            or legacy.get("scenario_content_used_for_v2") is not False):
        raise PermissionError("legacy V1 isolation state is not safe")
    split_data = manifest.get("splits", {}).get(split)
    if not isinstance(split_data, dict):
        raise PermissionError("selected V2 split is not committed")
    ids, hashes = split_data.get("scenario_ids"), split_data.get("scenario_hashes")
    if not isinstance(ids, list) or not isinstance(hashes, dict) or set(ids) != set(hashes):
        raise PermissionError("V2 split commitments are malformed")
    filename = "v2-dev-scenarios.jsonl" if split == "DEV" else "v2-validation-scenarios.jsonl"
    scenarios: dict[str, dict] = {}
    for line in (DATA / filename).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        scenario_id = row.get("scenario_id") if isinstance(row, dict) else None
        if scenario_id in scenarios or scenario_id not in hashes or stable_hash(row) != hashes[scenario_id]:
            raise PermissionError("V2 scenario content does not match its committed split hash")
        scenarios[scenario_id] = row
    if set(scenarios) != set(ids):
        raise PermissionError("V2 split payload does not exactly match its committed IDs")
    return manifest, scenarios


def validate_source_scenarios(scenarios: list[dict]) -> dict:
    """Bind explicit V2 source objects to one manifest split without opening legacy data."""
    ids = [row.get("scenario_id") for row in scenarios if isinstance(row, dict)]
    if len(ids) != len(scenarios) or not ids or len(set(ids)) != len(ids):
        raise PermissionError("V2 source scenarios must have unique identities")
    split = "DEV" if all(item.startswith("v2-dev-") for item in ids) else (
        "VALIDATION" if all(item.startswith("v2-validation-") for item in ids) else None
    )
    if split is None:
        raise PermissionError("scenario identity does not name a committed V2 split")
    _manifest, committed = load_split(split)
    commitments = {}
    for row in scenarios:
        scenario_id = row["scenario_id"]
        source = committed.get(scenario_id)
        if source is None or row != source:
            raise PermissionError("scenario is not an exact committed V2 source fixture")
        commitments[scenario_id] = stable_hash(source)
    return {"dataset_split": split, "scenario_commitments": commitments}


def validate_blind_packet(packet: Mapping[str, object], private_mapping: Mapping[str, object], split: str) -> dict:
    """Check source commitments, generated user-turn parity and private blind mapping."""
    _manifest, sources = load_split(split)
    scenarios = packet.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise PermissionError("judge packet has no V2 scenarios")
    if (private_mapping.get("private") is not True
            or private_mapping.get("dataset_split") != split
            or private_mapping.get("blind_packet_sha256") != stable_hash(packet)):
        raise PermissionError("blind packet is not bound to its private V2 provenance receipt")
    commitments = private_mapping.get("scenario_commitments")
    rows = private_mapping.get("mappings")
    if (not isinstance(commitments, dict) or not isinstance(rows, list)
            or private_mapping.get("mapping_sha256") != stable_hash(rows)):
        raise PermissionError("private V2 scenario commitments or arm mappings are missing")
    packet_ids = [row.get("scenario_id") for row in scenarios if isinstance(row, dict)]
    if len(packet_ids) != len(scenarios) or len(packet_ids) != len(set(packet_ids)):
        raise PermissionError("blind packet scenario identities are invalid")
    if set(commitments) != set(packet_ids):
        raise PermissionError("private commitments do not exactly cover the blind packet")
    by_pair: dict[str, dict[str, dict]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PermissionError("private arm mapping is malformed")
        pair = by_pair.setdefault(row.get("scenario_id"), {})
        label = row.get("label")
        if label not in {"X", "Y"} or label in pair:
            raise PermissionError("private X/Y arm mapping has duplicate or invalid labels")
        pair[label] = row
    for display in scenarios:
        scenario_id = display["scenario_id"]
        source = sources.get(scenario_id)
        if source is None or commitments.get(scenario_id) != stable_hash(source):
            raise PermissionError("judge packet is outside the committed replacement V2 split")
        source_users = [turn["text"] for turn in source["dialogue_turns"] if turn["role"] == "user"]
        for field in ("category", "context", "character_context", "memory_fixture"):
            if display.get(field) != source.get(field):
                raise PermissionError("blind packet context does not match its V2 source scenario")
        candidates = display.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 2:
            raise PermissionError("V2 blind judging requires exactly two candidate arms")
        mapped = by_pair.get(scenario_id, {})
        if set(mapped) != {"X", "Y"} or {item.get("model_id") for item in mapped.values()} != {"baseline", "naturalness-v1"}:
            raise PermissionError("private V2 mapping must bind X/Y to exactly the approved arms")
        for candidate in candidates:
            label = candidate.get("label") if isinstance(candidate, dict) else None
            map_row = mapped.get(label)
            if (map_row is None or map_row.get("scenario_hash") != stable_hash(source)
                    or map_row.get("blind_candidate_sha256") != stable_hash(candidate)
                    or not isinstance(map_row.get("result_hash"), str)
                    or len(map_row["result_hash"]) != 64):
                raise PermissionError("blind candidate is not bound to its committed V2 run mapping")
            turns = candidate.get("turns")
            if (not isinstance(turns, list) or len(turns) != len(source["dialogue_turns"])
                    or any(turn.get("role") != expected["role"] for turn, expected in
                           zip(turns, source["dialogue_turns"], strict=True))):
                raise PermissionError("blind transcript structure does not match its V2 scenario")
            actual_users = [turn.get("text") for turn in turns if turn.get("role") == "user"]
            if actual_users != source_users:
                raise PermissionError("blind user turns do not match the committed V2 scenario")
    if set(by_pair) != set(packet_ids):
        raise PermissionError("private arm mappings contain out-of-packet scenarios")
    return {"dataset_split": split, "scenario_commitments": dict(commitments),
            "blind_packet_sha256": stable_hash(packet)}


def validate_calibration_pair(pair: Mapping[str, object], split: str) -> dict:
    """Validate one completed pair against a real V2 scenario commitment."""
    _manifest, sources = load_split(split)
    source_id = pair.get("source_scenario_id")
    source = sources.get(source_id)
    if source is None or pair.get("scenario_commitment") != stable_hash(source):
        raise PermissionError("calibration pair is not bound to the selected V2 source scenario")
    if pair.get("dataset_version") != "V2" or pair.get("dataset_split") != split:
        raise PermissionError("calibration pair has a mismatched V2 split identity")
    candidates = pair.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 2:
        raise PermissionError("calibration requires a complete Baseline/Candidate blind pair")
    source_users = [turn["text"] for turn in source["dialogue_turns"] if turn["role"] == "user"]
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("label") not in {"X", "Y"}:
            raise PermissionError("calibration pair labels must be blind X/Y")
        turns = candidate.get("turns")
        if (not isinstance(turns, list) or len(turns) != len(source["dialogue_turns"])
                or any(turn.get("role") != expected["role"] for turn, expected in
                       zip(turns, source["dialogue_turns"], strict=True))):
            raise PermissionError("calibration transcript does not match its committed V2 source")
        if [turn.get("text") for turn in turns if turn.get("role") == "user"] != source_users:
            raise PermissionError("calibration user turns differ from the committed V2 source")
    return {"source_scenario_id": source_id, "scenario_commitment": stable_hash(source),
            "dataset_split": split}
