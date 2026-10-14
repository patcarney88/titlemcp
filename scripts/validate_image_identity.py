"""Validate explicit Docker build identity without contacting a package registry."""

from __future__ import annotations

import re
import sys


def validate(version: str, revision: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.dev\d+)?(?:\+[a-zA-Z0-9][a-zA-Z0-9.-]*)?", version):
        raise ValueError("TITLEMCP_VERSION must be an explicit release or development version.")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("SOURCE_REVISION must be the full lowercase Git commit SHA.")


if __name__ == "__main__":
    validate(*sys.argv[1:])
