"""Run named unittest files serially with no inherited credentials or real networking."""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def deny_network(*args, **kwargs):
    raise AssertionError("Real network access is disabled in the offline regression runner")


def main() -> int:
    os.environ.clear()
    os.environ["PATH"] = "/usr/bin:/bin"
    sys.path.insert(0, str(ROOT))
    for source in sorted(ROOT.joinpath("packages").rglob("src")):
        if "templates" not in source.parts:
            sys.path.insert(0, str(source))
    from title_mcp.settings import TitleMCPSettings

    TitleMCPSettings.model_config["env_file"] = None
    socket.getaddrinfo = deny_network
    socket.create_connection = deny_network
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    suite = unittest.TestSuite()
    for index, argument in enumerate(sys.argv[1:]):
        filename = (ROOT / argument).resolve()
        if not filename.is_relative_to(ROOT) or not filename.is_file():
            raise ValueError(f"Test file is outside the repository: {filename}")
        sys.path.insert(0, str(filename.parent))
        spec = importlib.util.spec_from_file_location(f"offline_regression_{index}", filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    if suite.countTestCases() == 0:
        raise ValueError("Name at least one test file")
    return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
