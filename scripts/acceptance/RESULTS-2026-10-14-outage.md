# Local database-outage acceptance — 2026-10-14

**Outcome: passed.** The extended local acceptance harness tested the product
image built from source commit
[`c7901274a5b1173388e4aff6df074479ee24db17`](https://github.com/patcarney88/titlemcp/commit/c7901274a5b1173388e4aff6df074479ee24db17).
Application code was unchanged from `e841634a9`. The harness additions were local
edits during the run and are identified by the separate digest below. Committing
these checks and this summary does not represent another acceptance run.

## Real database outages

Only the disposable test Postgres container was paused/unpaused and disconnected
from/reconnected to its internal Docker network. Its temporary data was retained;
the application was not restarted during either outage/restoration sequence.

| Phase | Authenticated `/readyz` | Maximum HTTP response time |
| --- | --- | --- |
| Healthy before faults | 200, three consecutive probes | 0.004106 seconds |
| Postgres paused | 503, three consecutive probes | 1.012503 seconds |
| After unpause | 200, three consecutive probes | 0.004668 seconds |
| Postgres disconnected | 503, three consecutive probes | 1.018833 seconds |
| After reconnect | 200, three consecutive probes | 0.005231 seconds |

All 15 readiness observations met the two-second HTTP response budget with the
application's readiness deadline configured to one second. The failure bodies
matched the exact generic error response. In every phase, public `/healthz`
returned 200 and anonymous/invalid-credential `/readyz` requests returned 401.
After each restoration, readiness recovered within the probe's ten-second wait
limit and MCP readback confirmed the original pending workflow's identity,
status and revision were unchanged. These timings describe this local run;
they are not a production latency or availability guarantee.

## Other verification

- 16 focused offline tests passed separately, including harness rejection of
  false-green readiness, leaked response details, lost authorization, stalled
  responses and failed restoration, plus existing repository-readiness tests.
- All eight real Postgres acceptance cases passed in 0.720 seconds: round trips,
  concurrent revisions, deleted records, historical JSON, review races/audit
  preservation, competing engines, bounded recovery and durable safe failures.
- OrbStack Linux ARM64; Python 3.12.15; Postgres 16.15; non-root UID 100;
  all 49 pinned runtime/build dependency versions verified.
- Real Streamable HTTP MCP session, 23 advertised tools, installed Franklin
  connector discovery, mandatory review, port override and HTTPS catalog passed.
- A separate subsequent application restart recovered seven queued/running
  workflows and preserved pending/rejected reviews; tasks remained planned.
- Graceful stop exited 0 without OOM termination. App and database were each
  capped at 384 MiB and half a CPU, with container swap disabled and no host ports.
- One resource snapshot showed 64.98 MiB application and 71.41 MiB Postgres,
  approximately 136 MiB combined; this is not a peak measurement.
- Cleanup and independent readback confirmed no task containers, network or
  tagged image remained. All four baseline containers remained running.

## Provenance and limits

- Acceptance-harness SHA-256: `cd81cbf894b67cc7a9d38d6cbda70dffdb5e8769cc337e011be30daaf52022ba`.
- Build-input SHA-256: `72e2bffbdf87cbd3dab75de1a03c68d5d77975e78225e4b8b863244e4f792a30`.
- Tested local image: `sha256:bc16a5a81bc4940ffd1a60fc2fcd348bcdf01c057b951e8f7c70a7e01b383fe5`.
- Installed version: `0.3.7+acceptance.c7901274a5b1173388e4aff6df074479ee24db17`.

The build used the minimal source-input context, disabled automatic Git probing
and an empty Docker credential configuration. Public package downloads occurred;
the base image remains a floating tag. The image was never published.

This summary excludes credentials, connection strings, host paths, workflow/run
identifiers, container names and raw logs. Raw evidence remains local. Database
crash/persistent-volume recovery, failover, sustained load, other architectures
and live-provider behavior remain untested. This result does not close the other
production release gates. See the [harness instructions](README.md) to repeat it.

GitHub Actions remains disabled. No deployment or live credentials were used.
