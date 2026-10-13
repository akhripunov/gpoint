import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("manifest", ROOT / "scripts/release_component_manifest_v4.py")
v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)
A, B = "a"*40, "b"*40


def image(source, digit):
    return {"sourceSha": source, "imageId": "sha256:" + digit * 64, "platform": "linux/amd64"}


def previous():
    return {"sourceSha": A, "releaseState": "COMPLETE", "recoveryRequired": False,
            "images": {"app": image(A, "1"), "agata": image(A, "2"), "gsales": image(A, "3")}}


def impact(component):
    return {"candidateSha": B, "productionBaselineSha": A,
            "baselineProvenance": "immutable-COMPLETE",
            "maySkipV3Gates": False, "mayDeploySelectively": False,
            "component": component, "classification": "COMPONENT_CANDIDATE"}


class ManifestTests(unittest.TestCase):
    def test_static_inherits_all_images(self):
        proof = v4.compose_manifest(B, A, impact("ui"), previous(), {},
                                     static={"candidateSha": B, "kind": "gpoint-miniapp-static"})
        self.assertEqual(proof["route"], "STATIC_UI_CANDIDATE")
        self.assertTrue(v4.verify_manifest(proof, B, A))
        self.assertTrue(all(x["kind"] == "inherited" for x in proof["images"].values()))
        self.assertFalse(proof["mayDeploySelectively"])

    def test_agata_produces_one_image(self):
        proof = v4.compose_manifest(B, A, impact("agata"), previous(),
                                     {"agata": image(B, "4")})
        self.assertEqual(proof["images"]["agata"]["kind"], "new")
        self.assertEqual(proof["images"]["app"]["sourceSha"], A)
        self.assertEqual(proof["images"]["gsales"]["sourceSha"], A)

    def test_full_requires_three_fresh(self):
        p = dict(impact(None), classification="FULL")
        with self.assertRaises(ValueError):
            v4.compose_manifest(B, A, p, previous(), {})
        built = {k: image(B, v) for k, v in (("app", "4"), ("agata", "5"), ("gsales", "6"))}
        proof = v4.compose_manifest(B, A, p, previous(), built)
        self.assertEqual(proof["route"], "FULL")

    def test_refuses_unverified_parent(self):
        p = previous()
        p["releaseState"] = "FAILED"
        with self.assertRaises(ValueError):
            v4.compose_manifest(B, A, impact("agata"), p, {"agata": image(B, "4")})

    def test_refuses_image_scope_error(self):
        with self.assertRaises(ValueError):
            v4.compose_manifest(B, A, impact("agata"), previous(),
                                {"app": image(B, "4")})

    def test_refuses_cross_sha_and_non_digest(self):
        for a in (image(A, "4"), {"sourceSha": B, "imageId": "bad", "platform": "linux/amd64"}):
            with self.subTest(a=a):
                with self.assertRaises(ValueError):
                    v4.compose_manifest(B, A, impact("agata"), previous(), {"agata": a})

    def test_refuses_bypass_flag(self):
        p = dict(impact("ui"), maySkipV3Gates=True)
        with self.assertRaises(ValueError):
            v4.compose_manifest(B, A, p, previous(), {}, static={"candidateSha": B, "kind": "gpoint-miniapp-static"})

    def test_refuses_inherited_candidate_label(self):
        prev = previous()
        prev["images"]["app"]["sourceSha"] = B
        with self.assertRaises(ValueError):
            v4.compose_manifest(B, A, impact("agata"), prev, {"agata": image(B, "4")})

if __name__ == "__main__":
    unittest.main()
