from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from ci_packages import external_requirements, projects, selected_packages  # noqa: E402


class CISelectionTests(unittest.TestCase):
    def test_platform_change_covers_all_dependent_jurisdictions(self):
        packages = projects()
        selected = selected_packages(["packages/platforms/publicsearch/src/client.py"], packages)
        self.assertIn("titlemcp-platform-publicsearch", selected)
        for state in ("co", "il", "oh", "pa", "sc", "tx"):
            self.assertIn(f"titlemcp-us-{state}-recorder", selected)
        self.assertNotIn("titlemcp-us-va-tax", selected)

    def test_core_or_shared_check_change_covers_every_package(self):
        packages = projects()
        for filename in (
            "packages/titlemcp/src/title_mcp/domain/recorder.py",
            "scripts/run_offline_tests.py",
            ".github/workflows/ci.yml",
        ):
            self.assertEqual(selected_packages([filename], packages), set(packages))

    def test_county_only_change_stays_scoped_and_local_dependencies_are_not_downloaded(self):
        packages = projects()
        self.assertEqual(
            selected_packages(["packages/jurisdictions/us/pa/recorder/tests/test_a.py"], packages),
            {"titlemcp-us-pa-recorder"},
        )
        self.assertFalse(any(dep.startswith("titlemcp") for dep in external_requirements(packages)))
