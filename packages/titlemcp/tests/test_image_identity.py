from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from validate_image_identity import validate  # noqa: E402


class ImageIdentityTests(unittest.TestCase):
    def test_explicit_release_and_personal_versions_are_valid(self):
        for version in ("0.3.7", "0.3.7+fork.abcdef", "0.3.8.dev1+gabcdef"):
            validate(version, "a" * 40)

    def test_missing_or_untraceable_identity_fails_closed(self):
        for version, revision in (
            ("", "a" * 40),
            ("latest", "a" * 40),
            ("0.3.7", "main"),
            ("0.3.7", "abcdef"),
            ("0.3.7", ""),
        ):
            with self.subTest(version=version, revision=revision):
                with self.assertRaises(ValueError):
                    validate(version, revision)
