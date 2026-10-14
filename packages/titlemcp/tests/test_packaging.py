from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

import title_mcp

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_source_archive_without_git_builds_an_explicit_nonrelease_wheel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "packages/titlemcp"
            shutil.copytree(
                PACKAGE_ROOT,
                source,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "dist", "build", ".ruff_cache", ".venv"
                ),
            )
            output = root / "wheels"
            output.mkdir()
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from hatchling.build import build_wheel; import sys; "
                    "print(build_wheel(sys.argv[1]))",
                    str(output),
                ],
                cwd=source,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            wheels = list(output.glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            self.assertIn("0.0.0+archive", wheels[0].name)
            with zipfile.ZipFile(wheels[0]) as wheel:
                metadata = next(name for name in wheel.namelist() if name.endswith("/METADATA"))
                self.assertIn("Version: 0.0.0+archive", wheel.read(metadata).decode())

    def test_project_package_name(self) -> None:
        with (PACKAGE_ROOT / "pyproject.toml").open("rb") as pyproject:
            metadata = tomllib.load(pyproject)

        self.assertEqual(metadata["project"]["name"], "titlemcp")

    def test_version_comes_from_the_release_tag(self) -> None:
        with (PACKAGE_ROOT / "pyproject.toml").open("rb") as pyproject:
            metadata = tomllib.load(pyproject)

        # Nothing in the tree states a version: a release is a tag, and a number
        # written here would have to be edited for every one of them.
        self.assertNotIn("version", metadata["project"])
        self.assertIn("version", metadata["project"]["dynamic"])
        self.assertEqual(metadata["tool"]["hatch"]["version"]["source"], "vcs")
        self.assertTrue(title_mcp.__version__)

    def test_jurisdiction_extra_points_to_package(self) -> None:
        with (PACKAGE_ROOT / "pyproject.toml").open("rb") as pyproject:
            metadata = tomllib.load(pyproject)

        extras = metadata["project"]["optional-dependencies"]
        self.assertIn("us-oh-franklin", extras)
        self.assertIn(
            "titlemcp-us-oh-franklin-recorder>=0.1.0",
            extras["us-oh-franklin"],
        )

    def test_only_titlemcp_console_scripts_are_exported(self) -> None:
        with (PACKAGE_ROOT / "pyproject.toml").open("rb") as pyproject:
            metadata = tomllib.load(pyproject)

        scripts = metadata["project"]["scripts"]
        self.assertIn("titlemcp-server", scripts)
        self.assertIn("titlemcp-ollama", scripts)
        self.assertNotIn("title-mcp-server", scripts)
        self.assertNotIn("title-mcp-ollama", scripts)

    def test_package_exports_typed_marker(self) -> None:
        self.assertTrue((PACKAGE_ROOT / "src/title_mcp/py.typed").is_file())


if __name__ == "__main__":
    unittest.main()
