# Local Postgres and Docker acceptance

See the [sanitized 2026-10-14 results](RESULTS-2026-10-14.md) for the completed run
and its exact tested source revision.

Run explicitly from a local OrbStack workstation with Python 3.12:

```bash
python scripts/run_local_acceptance.py
```

This is separate from the default offline unit suite. It builds the repository's
Dockerfile, then uses its installed packages against a disposable Postgres 16
database. `postgres:16-alpine` must already be cached; the harness does not attach
to an existing database. Building can download public Python images and pinned
PyPI dependencies, using an empty Docker configuration without registry credentials.
The build context contains only the Dockerfile's exact inputs, with automatic Git
metadata probing disabled; an input digest and explicit commit identity are retained.

The runtime containers have an internal Docker network, no published ports and
generated test credentials. Each has a 384 MiB memory limit, no container swap,
half a CPU and 128-process limit. Postgres uses temporary memory-backed data,
16 MiB shared buffers and 40 maximum connections. The image build runs first,
with an eight-minute timeout and a 1 GiB per-process data-segment limit; the
existing BuildKit daemon itself is not memory-capped by that limit.

Checks cover real SQL round trips, stale/concurrent revisions, historical JSON,
review-versus-worker races, multiple-engine planning, bounded recovery and
durable sanitized failure state. Runtime checks use real HTTP and MCP sessions,
verify authentication and installed entry points, create synthetic workflows,
then stop/restart the application to verify persisted queue recovery and review
state. No connector or paid-provider queries are made.

Logs and `result.json` go to `../acceptance-results/<run-id>/` beside the checkout.
The runner removes its containers, temporary database state, network and tagged
image, and reads back cleanup. It leaves shared Docker caches and pre-existing
workloads untouched. It does not publish images, push commits or deploy.
