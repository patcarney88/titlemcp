from __future__ import annotations

import importlib
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from title_mcp.mcp.server import create_mcp_server
from title_mcp.platform import TitleMCPPlatform
from title_mcp.settings import TitleMCPSettings

ROOT = Path(__file__).resolve().parents[3]


class PluginActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_documented_opt_in_registers_each_package_and_discovery_is_explicit(self):
        projects = sorted((ROOT / "packages/jurisdictions").rglob("pyproject.toml"))
        checked = 0
        for project in projects:
            metadata = tomllib.loads(project.read_text())
            plugins = metadata["project"].get("entry-points", {}).get("title_mcp.plugins", {})
            for target in plugins.values():
                with self.subTest(package=metadata["project"]["name"]):
                    module, attribute = target.split(":")
                    cls = getattr(importlib.import_module(module), attribute)
                    ep = SimpleNamespace(load=lambda cls=cls: cls)
                    settings = TitleMCPSettings(
                        load_entry_point_plugins=True,
                        load_entry_point_adapters=False,
                        load_entry_point_capabilities=False,
                        load_entry_point_sources=False,
                        load_entry_point_toolsets=False,
                        load_entry_point_vendors=False,
                    )
                    platform = TitleMCPPlatform(settings=settings)
                    before = {s.source_id for s in platform.sources.all()}
                    try:
                        with patch("title_mcp.plugins.loader.entry_points", return_value=[ep]):
                            await platform.initialize()
                        registered = {s.source_id for s in platform.sources.all()}
                        self.assertGreater(len(registered), len(before))
                        server = create_mcp_server(platform=platform)
                        result = (
                            await server.call_tool("list_title_capabilities", {})
                        ).structured_content
                        self.assertEqual(set(result["registered_source_ids"]), registered)
                        self.assertTrue(result["plugin_loading_enabled"])
                        self.assertIn(
                            "TITLE_MCP_LOAD_ENTRY_POINT_PLUGINS=true",
                            project.with_name("README.md").read_text(),
                        )
                    finally:
                        await platform.shutdown()
                    checked += 1
        self.assertEqual(checked, 11)

    async def test_disabled_plugins_are_not_loaded(self):
        platform = TitleMCPPlatform(settings=TitleMCPSettings(load_entry_point_plugins=False))
        try:
            with patch("title_mcp.platform.load_plugins") as load:
                await platform.initialize()
                load.assert_not_called()
        finally:
            await platform.shutdown()
