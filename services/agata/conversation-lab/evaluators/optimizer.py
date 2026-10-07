"""Validation-only Promptfoo Optimize adapter builder with explicit split allowlists."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Mapping


SHA256 = re.compile(r"^[0-9a-f]{64}$")
TRAINING_SPLITS = ("DEV",)
VALIDATION_SPLITS = ("VALIDATION",)
FORBIDDEN_SPLITS = {"SEALED_HOLDOUT"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _manifest_splits(split_manifest):
    if not isinstance(split_manifest, Mapping) or split_manifest.get("schema_version") != 2:
        raise ValueError("optimizer requires replacement split manifest V2")
    if not isinstance(split_manifest.get("dataset_id"), str) or not split_manifest["dataset_id"].strip():
        raise ValueError("V2 dataset identity is required")
    legacy = split_manifest.get("legacy_holdout_v1")
    read_incident = isinstance(legacy, Mapping) and legacy.get("scenario_content_read") is True
    incident_recorded = (isinstance(legacy, Mapping) and isinstance(legacy.get("access_incident"), str)
                         and bool(legacy["access_incident"].strip())
                         and isinstance(legacy.get("v2_fixture_provenance"), str)
                         and bool(legacy["v2_fixture_provenance"].strip()))
    if (not isinstance(legacy, Mapping) or legacy.get("status") != "COMPROMISED_EXCLUDED"
            or legacy.get("optimizer_access") is not False
            or legacy.get("scenario_content_used_for_v2") is not False
            or legacy.get("scenario_content_read") not in (False, True)
            or (read_incident and not incident_recorded)):
        raise PermissionError("compromised Benchmark V1 holdout must remain excluded")
    allowlist = split_manifest.get("optimizer_allowlist")
    if not isinstance(allowlist, Mapping):
        raise PermissionError("optimizer split allowlist is missing")
    if (allowlist.get("training_splits") != list(TRAINING_SPLITS) or
            allowlist.get("validation_splits") != list(VALIDATION_SPLITS) or
            not FORBIDDEN_SPLITS.issubset(set(allowlist.get("forbidden_splits", [])))):
        raise PermissionError("optimizer allowlist must permit DEV/VALIDATION only")
    splits = split_manifest.get("splits")
    if not isinstance(splits, Mapping) or set(splits) != {"DEV", "VALIDATION", "SEALED_HOLDOUT"}:
        raise ValueError("V2 split manifest must define DEV, VALIDATION, and SEALED_HOLDOUT")
    ids_by_split = {}
    all_ids = set()
    for split in ("DEV", "VALIDATION"):
        value = splits[split]
        if not isinstance(value, Mapping):
            raise ValueError("split entry must be an object")
        ids = value.get("scenario_ids")
        hashes = value.get("scenario_hashes")
        if not isinstance(ids, list) or not isinstance(hashes, Mapping) or len(set(ids)) != len(ids):
            raise ValueError("split IDs and hash commitments are malformed")
        if set(ids) != set(hashes):
            raise ValueError("every split scenario ID requires one commitment")
        if any(not isinstance(item, str) or not item.strip() or item.endswith("-01") for item in ids):
            raise PermissionError("V2 allowlists cannot include compromised -01 scenarios")
        if any(not isinstance(digest, str) or not SHA256.fullmatch(digest) for digest in hashes.values()):
            raise ValueError("scenario commitments must be lowercase SHA256 hashes")
        if all_ids & set(ids):
            raise ValueError("V2 split scenario IDs must be disjoint")
        all_ids.update(ids)
        ids_by_split[split] = list(ids)
    sealed = splits["SEALED_HOLDOUT"]
    if not isinstance(sealed, Mapping):
        raise ValueError("sealed holdout must be represented only by an opaque empty entry")
    # Never enumerate sealed IDs or hashes here: the optimizer adapter must not receive them.
    if sealed.get("scenario_ids") != [] or sealed.get("scenario_hashes") != {}:
        raise PermissionError("sealed holdout identities and commitments are outside optimizer access")
    if sealed.get("plaintext_committed") is not False or sealed.get("path") or sealed.get("plaintext_path"):
        raise PermissionError("sealed holdout content or paths cannot enter optimizer configuration")
    if not ids_by_split["DEV"] or not ids_by_split["VALIDATION"]:
        raise ValueError("V2 DEV and VALIDATION allowlists must be populated before optimizer preparation")
    return ids_by_split


def build_optimizer_config(split_manifest, *, policy_hash, candidate_limit):
    if not isinstance(policy_hash, str) or not SHA256.fullmatch(policy_hash):
        raise ValueError("starting Naturalness Policy SHA256 is required")
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= 100:
        raise ValueError("candidate_limit must be between 1 and 100")
    ids_by_split = _manifest_splits(split_manifest)
    config = {
        "schema_version": 1,
        "adapter": "promptfoo-optimize-validation-only",
        "dataset_id": split_manifest["dataset_id"],
        "training_split": "DEV",
        "training_scenario_ids": ids_by_split["DEV"],
        "validation_split": "VALIDATION",
        "validation_scenario_ids": ids_by_split["VALIDATION"],
        "starting_naturalness_policy_sha256": policy_hash,
        "candidate_limit": candidate_limit,
        "max_model_calls": 0,
        "sealed_holdout_included": False,
        "execution_enabled": False,
        "lifecycle_state": "OPTIMIZER_ADAPTER_IMPLEMENTED",
    }
    config["config_sha256"] = _digest(config)
    return config


def authorize_optimizer_run(config, approval):
    """Validate a bounded approval envelope; return an inert authorization artifact only."""
    if not isinstance(config, Mapping) or config.get("adapter") != "promptfoo-optimize-validation-only":
        raise PermissionError("unsupported optimizer adapter config")
    expected_keys = {"schema_version", "adapter", "dataset_id", "training_split", "training_scenario_ids",
                     "validation_split", "validation_scenario_ids", "starting_naturalness_policy_sha256",
                     "candidate_limit", "max_model_calls", "sealed_holdout_included", "execution_enabled",
                     "lifecycle_state", "config_sha256"}
    if set(config) != expected_keys:
        raise PermissionError("optimizer config contains unexpected or missing fields")
    body = dict(config)
    digest = body.pop("config_sha256", None)
    if not isinstance(digest, str) or digest != _digest(body):
        raise PermissionError("optimizer config hash does not match its frozen contents")
    training_ids, validation_ids = config.get("training_scenario_ids"), config.get("validation_scenario_ids")
    if (config.get("schema_version") != 1 or config.get("training_split") != "DEV" or config.get("validation_split") != "VALIDATION" or
            not isinstance(training_ids, list) or not training_ids or not isinstance(validation_ids, list) or not validation_ids or
            len(set(training_ids)) != len(training_ids) or len(set(validation_ids)) != len(validation_ids) or
            set(training_ids) & set(validation_ids) or any(not isinstance(item, str) or item.endswith("-01") for item in training_ids + validation_ids) or
            not isinstance(config.get("starting_naturalness_policy_sha256"), str) or not SHA256.fullmatch(config["starting_naturalness_policy_sha256"])):
        raise PermissionError("optimizer config violates the frozen V2 training/validation allowlist")
    if config.get("sealed_holdout_included") is not False or config.get("execution_enabled") is not False:
        raise PermissionError("optimizer config is not safely inert")
    if not isinstance(approval, Mapping) or approval.get("approved") is not True:
        raise PermissionError("a separate bounded OWNER optimizer approval is required")
    if not isinstance(approval.get("approval_id"), str) or not approval["approval_id"].strip():
        raise PermissionError("approval identity is required")
    if approval.get("dataset_id") != config.get("dataset_id") or approval.get("split") != "DEV":
        raise PermissionError("approval dataset/split is outside the optimizer training allowlist")
    candidate_count = approval.get("max_candidate_count")
    model_calls = approval.get("max_model_calls")
    if type(candidate_count) is not int or not 1 <= candidate_count <= config["candidate_limit"]:
        raise PermissionError("approved candidate count exceeds the prepared limit")
    if type(model_calls) is not int or not 1 <= model_calls <= 10000:
        raise PermissionError("optimizer model-call budget must be explicitly bounded to 1..10000")
    starting_hash = approval.get("starting_naturalness_policy_sha256")
    if starting_hash != config.get("starting_naturalness_policy_sha256"):
        raise PermissionError("approval Naturalness Policy hash does not match the frozen config")
    return {
        **copy.deepcopy(dict(config)),
        "authorization": {
            "approval_id": approval["approval_id"],
            "split": "DEV",
            "max_candidate_count": candidate_count,
            "max_model_calls": model_calls,
        },
        "authorized": True,
        "execution_enabled": False,
        "lifecycle_state": "OPTIMIZER_RUN_AUTHORIZED",
    }
