import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts/verify_native_eros_image.py"
SPEC = importlib.util.spec_from_file_location("verify_native_eros_image", PATH)
VERIFY = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(VERIFY)

CANDIDATE = "a" * 40
UPSTREAM = VERIFY.UPSTREAM_SHA
DIGEST = "sha256:" + "1" * 64


def image():
    return {
        "Os": "linux",
        "Architecture": "amd64",
        "RepoDigests": [VERIFY.EROS_IMAGE + "@" + DIGEST],
        "Config": {"Labels": {
            "org.opencontainers.image.source": "https://github.com/etherfunlab/eros-engine",
            "org.opencontainers.image.revision": UPSTREAM,
            "com.gpoint.source.revision": CANDIDATE,
            "com.gpoint.eros.upstream-revision": UPSTREAM,
            "com.gpoint.eros.patch-set.sha256": "none",
        }},
    }


def test_accepts_exact_stock_eros_digest_platform_upstream_and_candidate_labels():
    VERIFY.verify_image(image(), digest=DIGEST, candidate_sha=CANDIDATE)


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(Architecture="arm64"),
    lambda row: row.update(RepoDigests=[]),
    lambda row: row["Config"]["Labels"].update({"org.opencontainers.image.revision": "b" * 40}),
    lambda row: row["Config"]["Labels"].update({"com.gpoint.source.revision": "b" * 40}),
    lambda row: row["Config"]["Labels"].update({"com.gpoint.eros.patch-set.sha256": "patched"}),
])
def test_rejects_nonstock_or_mismatched_runtime_identity(mutation):
    row = image()
    mutation(row)
    with pytest.raises(ValueError):
        VERIFY.verify_image(row, digest=DIGEST, candidate_sha=CANDIDATE)


def test_rejects_missing_or_malformed_digest():
    with pytest.raises(ValueError, match="digest"):
        VERIFY.verify_image(image(), digest="latest", candidate_sha=CANDIDATE)


def test_rejects_empty_sbom_and_provenance_without_exact_upstream_source():
    with pytest.raises(ValueError, match="SBOM"):
        VERIFY.verify_attestations({"packages": []}, {})

    provenance = {"SLSA": {"predicate": {
        "buildDefinition": {"externalParameters": {"configSource": {
            "uri": VERIFY.UPSTREAM_REPOSITORY,
            "digest": {"sha1": "b" * 40},
        }}, "resolvedDependencies": [{
            "uri": VERIFY.UPSTREAM_REPOSITORY,
            "digest": {"sha1": "b" * 40},
        }]}
    }}}
    with pytest.raises(ValueError, match="exact SHA"):
        VERIFY.verify_attestations({"packages": [{"name": "eros", "SPDXID": "SPDXRef-Eros"}]}, provenance)
