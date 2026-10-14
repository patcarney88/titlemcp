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
