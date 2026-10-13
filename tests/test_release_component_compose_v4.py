"""Component tags default to legacy RELEASE_SHA unless trusted V4 sets overrides."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
FILES = (ROOT / "infra/production/docker-compose.yml",
         ROOT / "infra/production/docker-compose.gsales.yml")
APP = "gpoint-runtime"
AGATA = "gpoint-agata-runtime"
GSALES = "gpoint-g-sales-runtime"
ROLE = {APP: "GPOINT_APP_IMAGE_SHA",
        AGATA: "GPOINT_AGATA_IMAGE_SHA",
        GSALES: "GPOINT_GSALES_IMAGE_SHA"}
SHA = "a" * 40
OVER = "b" * 40


class ComposeV4(unittest.TestCase):
    def image_values(self):
        for f in FILES:
            for line in f.read_text().splitlines():
                if "image: gpoint-" not in line or "runtime:" not in line:
                    continue
                name = line.split("image: ", 1)[1]
                key = name.split(":", 1)[0]
                if key in ROLE:
                    yield f, key, name

    def test_all_runtime_images_have_legacy_fallback(self):
        values = list(self.image_values())
        self.assertEqual(len(values), 8)
        for f, kind, value in values:
            with self.subTest(f=f, kind=kind):
                expected = '$' + '{' + ROLE[kind] + ':-' + '$' + '{RELEASE_SHA}}'
                self.assertEqual(value, kind + ":" + expected)

    @unittest.skipUnless(shutil.which("docker"), "Docker compose not installed")
    def test_compose_fallback_and_scoped_overrides(self):
        for f, role, image in self.image_values():
            config = "services:\n  service:\n    image: " + image + "\n"
            for overrides, expected in (({}, SHA), ({ROLE[role]: OVER}, OVER)):
                env = dict(os.environ, RELEASE_SHA=SHA)
                env.pop(ROLE[role], None)
                env.update(overrides)
                result = subprocess.run(
                    ["docker", "compose", "-f", "-", "config", "--images"],
                    input=config, text=True, capture_output=True, env=env, check=True)
                with self.subTest(path=f, role=role, overrides=bool(overrides)):
                    self.assertEqual(result.stdout.strip(), role + ":" + expected)

    def test_shared_agata_image_same_tag_in_both_lanes(self):
        main = FILES[0].read_text()
        sales = FILES[1].read_text()
        tag = AGATA + ':' + '$' + '{GPOINT_AGATA_IMAGE_SHA:-' + '$' + '{RELEASE_SHA}}'
        self.assertEqual(main.count(tag), 2)
        self.assertEqual(sales.count(tag), 2)


if __name__ == "__main__":
    unittest.main()
