"""Select changed packages and reverse dependencies; run checkout tests serially.

Only external requirements are installed. Local package imports always come from
this checkout via run_offline_tests.py, never published jurisdiction distributions.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def projects(root: Path = ROOT) -> dict[str, tuple[Path, dict]]:
    return {
        data["project"]["name"]: (path.parent, data["project"])
        for path in sorted((root / "packages").rglob("pyproject.toml"))
        if "templates" not in path.parts
        for data in [tomllib.loads(path.read_text())]
    }


def dependency_name(requirement: str) -> str:
    return re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def selected_packages(changed: list[str] | None, packages: dict, root: Path = ROOT) -> set[str]:
    if changed is None:
        return set(packages)
    selected: set[str] = set()
    for filename in changed:
        if filename.startswith(("scripts/", ".github/", "packages/titlemcp/")) or filename in {
            "pyproject.toml",
            "Dockerfile",
            "constraints-docker.txt",
        }:
            return set(packages)
        for name, (directory, _) in packages.items():
            if filename.startswith(directory.relative_to(root).as_posix() + "/"):
                selected.add(name)
    while True:
        dependents = {
            name
            for name, (_, data) in packages.items()
            if any(dependency_name(dep) in selected for dep in data.get("dependencies", []))
        }
        if dependents <= selected:
            return selected
        selected |= dependents


def external_requirements(packages: dict) -> list[str]:
    # Include test-image coverage and Postgres client import support, without a live service.
    requirements = {"pillow>=10.0", "asyncpg>=0.30.0"}
    for _, data in packages.values():
        requirements.update(
            dep for dep in data.get("dependencies", []) if dependency_name(dep) not in packages
        )
    return sorted(requirements)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="Git ref to compare; omission runs every package")
    parser.add_argument("--install", action="store_true", help="Install external test dependencies")
    parser.add_argument("--list", action="store_true", help="Print selection without running tests")
    args = parser.parse_args()
    packages = projects()
    changed = None
    if args.base and set(args.base) != {"0"}:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", args.base, "HEAD", "--"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        changed = result.stdout.splitlines()
    selected = selected_packages(changed, packages)
    tests = sorted(
        str(path.relative_to(ROOT))
        for name in selected
        for path in packages[name][0].joinpath("tests").glob("test_*.py")
    )
    print(json.dumps({"packages": sorted(selected), "test_files": tests}), flush=True)
    if args.list or not tests:
        return 0
    if args.install:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", *external_requirements(packages)],
            check=True,
            timeout=300,
        )
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_offline_tests.py"), *tests],
        cwd=ROOT,
        timeout=180,
        check=False,
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
