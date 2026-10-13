#!/usr/bin/env python3
"""Offline immutable per-component candidate manifest for V4 planning.

This does NOT fetch/trust remote attestations or permit partial deploy. The
caller must separately verify prior immutable COMPLETE, native image digests,
source dependency safety, platform, and live production state before apply.
"""
from __future__ import annotations
import copy
import re

SHA = re.compile(r"^[a-f0-9]{40}$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
ROLES = ("app", "agata", "gsales")
IMAGES = {"app": "gpoint-runtime", "agata": "gpoint-agata-runtime",
          "gsales": "gpoint-g-sales-runtime"}
PLATFORM = "linux/amd64"


def _image_valid(image):
    if not isinstance(image, dict):
        return False
    return (bool(SHA.fullmatch(image.get("sourceSha") or "")) and
            bool(DIGEST.fullmatch(image.get("imageId") or "")) and
            image.get("platform") == PLATFORM)


def compose_manifest(candidate, baseline, impact, previous, built, static=None):
    """Compose evidence inputs; cannot bestow trust on caller-supplied data."""
    if not SHA.fullmatch(candidate or "") or not SHA.fullmatch(baseline or ""):
        raise ValueError("candidate/baseline must be exact SHAs")
    if candidate == baseline or not isinstance(impact, dict):
        raise ValueError("invalid candidate or impact proof")
    if (impact.get("candidateSha") != candidate or
        impact.get("productionBaselineSha") != baseline or
        impact.get("baselineProvenance") != "immutable-COMPLETE" or
        impact.get("maySkipV3Gates") is not False or
        impact.get("mayDeploySelectively") is not False):
        raise ValueError("impact is not verified advisory input")
    if not isinstance(previous, dict) or previous.get("sourceSha") != baseline or \
            previous.get("releaseState") != "COMPLETE" or \
            previous.get("recoveryRequired") is not False:
        raise ValueError("unverified parent COMPLETE release")
    originals = previous.get("images")
    if not isinstance(originals, dict) or set(originals) != set(ROLES) or \
            not all(_image_valid(x) for x in originals.values()):
        raise ValueError("incomplete previous immutable image inventory")

    component = impact.get("component")
    if impact.get("classification") != "COMPONENT_CANDIDATE":
        affected = set(ROLES)
        route = "FULL"
    elif component == "ui":
        affected = set()
        route = "STATIC_UI_CANDIDATE"
    elif component == "agata":
        affected = {"agata"}
        route = "AGATA_CANDIDATE"
    else:
        raise ValueError("unknown component")
    if set(built) != affected:
        raise ValueError("new images do not match affected roles")
    if route == "STATIC_UI_CANDIDATE":
        if not isinstance(static, dict) or static.get("candidateSha") != candidate or \
                static.get("kind") != "gpoint-miniapp-static":
            raise ValueError("missing exact source UI artifact")
    elif static is not None:
        raise ValueError("unexpected standalone UI artifact")

    images = {}
    for role in ROLES:
        if role in affected:
            item = built[role]
            if not _image_valid(item) or item["sourceSha"] != candidate:
                raise ValueError("new image source SHA or identity invalid: " + role)
            images[role] = {**item, "kind": "new", "name": IMAGES[role]}
        else:
            item = originals[role]
            images[role] = {**item, "kind": "inherited", "name": IMAGES[role]}
    data = {
        "schema": 1, "candidateSha": candidate, "previousSourceSha": baseline,
        "route": route, "images": images, "static": copy.deepcopy(static),
        "immutableParentRequired": True,
        "requiresLiveNativeImageProof": True,
        "requiresV3ExactShaApproval": True,
        "requiresV3PreflightAndRollback": True,
        "mayDeploySelectively": False,
    }
    verify_manifest(data, candidate, baseline)
    return data


def verify_manifest(proof, candidate, baseline):
    if (proof.get("candidateSha") != candidate or proof.get("previousSourceSha") != baseline or
        proof.get("schema") != 1 or proof.get("mayDeploySelectively") is not False or
        proof.get("immutableParentRequired") is not True or
        proof.get("requiresLiveNativeImageProof") is not True or
        proof.get("requiresV3ExactShaApproval") is not True or
        proof.get("requiresV3PreflightAndRollback") is not True):
        raise ValueError("manifest gates absent")
    role_map = proof.get("images")
    if not isinstance(role_map, dict) or set(role_map) != set(ROLES):
        raise ValueError("manifest must contain three roles")
    route = proof.get("route")
    expected_new = {
        "FULL": set(ROLES),
        "STATIC_UI_CANDIDATE": set(),
        "AGATA_CANDIDATE": {"agata"},
    }.get(route)
    if expected_new is None:
        raise ValueError("unknown release route")
    for role, item in role_map.items():
        if not _image_valid(item) or item.get("name") != IMAGES[role]:
            raise ValueError("bad image identity")
        if (role in expected_new) != (item.get("kind") == "new"):
            raise ValueError("new/inherited role mismatch")
        if role in expected_new and item["sourceSha"] != candidate:
            raise ValueError("new image must match exact candidate SHA")
        if role not in expected_new and item["sourceSha"] == candidate:
            raise ValueError("inherited image is not a previous release")
    if route == "STATIC_UI_CANDIDATE":
        s = proof.get("static")
        if not isinstance(s, dict) or s.get("candidateSha") != candidate or \
                s.get("kind") != "gpoint-miniapp-static":
            raise ValueError("missing candidate static artifact")
    elif proof.get("static") is not None:
        raise ValueError("unexpected static artifact")
    return True


if __name__ == "__main__":
    raise SystemExit("Use compose_manifest only after trusted immutable input verification")
