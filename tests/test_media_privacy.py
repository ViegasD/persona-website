"""Offline regression checks for the repository's MinIO anonymous policy."""

import json
import unittest
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "docker" / "minio"


class MediaPrivacyTests(unittest.TestCase):
    def test_setup_replaces_bucket_wide_download_policy(self):
        setup = (CONFIG / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("set -eu", setup)
        self.assertIn("mc anonymous set-json", setup)
        self.assertIn("/etc/persona-minio/public-assets.policy.json", setup)
        self.assertNotIn("mc anonymous set download", setup)

    def test_only_public_assets_are_anonymously_readable(self):
        path = CONFIG / "public-assets.policy.json"
        self.assertTrue(path.is_file(), "A scoped anonymous policy must replace bucket-wide download")
        policy = json.loads(path.read_text(encoding="utf-8").replace("__BUCKET__", "persona-images"))
        self.assertEqual(policy["Version"], "2012-10-17")
        resources = []
        for statement in policy["Statement"]:
            self.assertEqual(statement["Effect"], "Allow")
            self.assertEqual(statement["Principal"], {"AWS": ["*"]})
            self.assertEqual(statement["Action"], ["s3:GetObject"])
            self.assertNotIn("Condition", statement)
            resources.extend(statement["Resource"])
        self.assertCountEqual(resources, [
            "arn:aws:s3:::persona-images/characters/*",
            "arn:aws:s3:::persona-images/templates/*",
        ])
        for key in ("characters/example/reference.webp", "templates/birthday/scene.png"):
            self.assertTrue(any(fnmatchcase(f"arn:aws:s3:::persona-images/{key}", r) for r in resources))
        for key in (
            "web/orders/12/video-1.mp4",
            "web/orders/12/thumbnail-1.jpg",
            "web/composite-frames/example.png",
            "sessions/12/videos/result.mp4",
            "composite-frames/example.png",
            "characters-private/example.png",
            "templates-private/example.png",
        ):
            self.assertFalse(any(fnmatchcase(f"arn:aws:s3:::persona-images/{key}", r) for r in resources), key)


if __name__ == "__main__":
    unittest.main()
