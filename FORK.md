# Pat Carney's TitleMCP version

This is Pat Carney's personal development fork of
[titlemcp/titlemcp](https://github.com/titlemcp/titlemcp), maintained at
[patcarney88/titlemcp](https://github.com/patcarney88/titlemcp).

The starting source is upstream commit
`5ea3bdeaa6cb5389aa1affed031fb8866007c9a3`, also identified in the supplied
`titlemcp-main.zip` archive. Changes address the October 2026 technical review.
Deployments, package publication and live provider access are separate actions.
GitHub Actions is disabled in this fork until explicitly enabled.

## License status

The upstream root `LICENSE` contains an MIT permission grant attributed to Razi
Title, Inc. The core package's `LICENSE` says all rights reserved, and its package
metadata declares `LicenseRef-Proprietary`. These statements conflict.

At Pat's direction, this fork preserves all upstream license files, copyright
notices and package license metadata. This document does not replace those terms
or claim that their conflict has been resolved. Alignment remains pending
authoritative clarification from the relevant copyright owner. No replacement
license has been selected for the upstream code.

## Execution limits

HTTP access is private and single tenant. Human review requires a separately
authenticated trusted host. Workflow actions remain planned until staff or a
real executor performs them. Local regression tests use fake providers and do
not establish live portal or production readiness.

## Local archives and verification

Core source archives without Git metadata build as `0.0.0+archive`. That fallback
is deliberately not a release or a compatible version claim for all addons.
For addon contract tests, use `scripts/run_offline_tests.py` or
`scripts/ci_packages.py`; these import every local dependency from this checkout
and do not substitute published jurisdiction packages. If installing a composed
archive environment, explicitly set `SETUPTOOLS_SCM_PRETEND_VERSION` to a reviewed
compatible local version (for example `0.3.7+archive`) for the core build first.
Retain the archive SHA and source commit separately. Do not publish archive builds.

Docker builds similarly require explicit version and full source commit arguments.
The dependency constraints cover Python 3.12/Linux; the base image is not digest-pinned.
