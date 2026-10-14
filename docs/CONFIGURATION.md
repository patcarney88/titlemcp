# Configuration

TitleMCP uses `pydantic-settings` and reads environment variables with the
`TITLE_MCP_` prefix. When the server is started from the repository root, `.env`
is loaded automatically by the settings layer.

## Server

```env
TITLE_MCP_LOG_LEVEL=INFO
TITLE_MCP_LOG_JSON=false
TITLE_MCP_MCP_TRANSPORT=stdio
TITLE_MCP_MCP_HOST=127.0.0.1
TITLE_MCP_MCP_PORT=8000
TITLE_MCP_MCP_ACCESS_TOKEN=
TITLE_MCP_MCP_PUBLIC_URL=
TITLE_MCP_INSPECTOR_URL=
TITLE_MCP_INSPECTOR_BACKEND_URL=
TITLE_MCP_MCP_DNS_REBINDING_PROTECTION=true
TITLE_MCP_MCP_ALLOWED_HOSTS=
TITLE_MCP_MCP_ALLOWED_ORIGINS=
```

Common transports:

- `stdio`: normal MCP client mode.
- `streamable-http`: HTTP deployment mode.
- `sse`: server-sent events mode.

Both HTTP transports require `Authorization: Bearer <instance-credential>` on
every application request, including the catalog and `/readyz`. Set
`TITLE_MCP_MCP_ACCESS_TOKEN` to a unique, high-entropy credential with at least 32
visible ASCII characters. A blank value denies application requests; `/healthz`
remains public. Query-string credentials are not accepted. STDIO uses its local
process boundary and does not use this HTTP credential.

This mode is private and single tenant: use a separate instance, credential,
state repository and connector-credential set for each trust domain. Every
authorized agent in an instance can access its orders and paid tools. Use TLS
and private ingress; this mode does not provide OAuth, user sign-in, per-order
permissions or a multi-tenant authorization service.

Human review is mandatory for the current workflows. Agents cannot submit
review decisions over MCP or disable review through request/configuration flags.
A trusted host must authenticate and authorize a human separately, then pass an
`AuthenticatedPrincipal` with `PrincipalKind.HUMAN` and
`ReviewPermission.REVIEW_WORKFLOW` to `WorkflowService.submit_review` (or the
engine). Bind `reviewer` to that authenticated subject; never construct the
principal from unverified client claims. No human sign-in UI or HTTP review
endpoint is supplied. Legacy approvals without authenticated reviewer attribution
require a fresh decision. Approved tasks remain `waiting_on_external` with
`execution_mode=planned` until an executor or staff actually performs them.

The Docker image runs `streamable-http` on `0.0.0.0:8000`. When a hosting
platform sets `PORT`, the image listens there instead of on
`TITLE_MCP_MCP_PORT`. See [Deploying](DEPLOY.md).

The HTTP discovery response at `/` advertises an absolute MCP endpoint as
`mcp.url`. By default it is derived from the request host. Set
`TITLE_MCP_MCP_PUBLIC_URL` when a proxy or deployment URL should be advertised
instead.

Set `TITLE_MCP_INSPECTOR_URL` to the browser URL for MCP Inspector and
`TITLE_MCP_INSPECTOR_BACKEND_URL` to the backend URL the Inspector proxy should
connect to. When both services run through `docker compose`, the backend URL is
`http://titlemcp:8000/mcp` because that is the service name visible from the
Inspector container. Configure the instance Authorization header in Inspector.
Catalog links omit URL credentials, query parameters and fragments.
The compose service passes that URL to Inspector at startup
so direct visits to `http://localhost:6274/` default to Streamable HTTP instead
of Inspector's sample STDIO server.

HTTP deployments can opt into explicit DNS-rebinding protection allowlists:

```env
TITLE_MCP_MCP_ALLOWED_HOSTS=localhost:*,127.0.0.1:*,titlemcp:*
TITLE_MCP_MCP_ALLOWED_ORIGINS=http://localhost:*,http://127.0.0.1:*
```

`docker-compose.yml` sets these for local Inspector usage. If you expose the
Inspector on a different host or port, set `MCP_INSPECTOR_ALLOWED_ORIGINS` for
the Inspector proxy as well, for example
`MCP_INSPECTOR_ALLOWED_ORIGINS=http://localhost:6275`.

## State Backend

Local development defaults to in-memory state:

```env
TITLE_MCP_STATE_BACKEND=memory
```

`/readyz` requires instance authorization and probes current storage on every
request. Postgres checks acquire a current connection and read the workflow table
without fetching order data or changing schema. The recovery loop must also be
running. `TITLE_MCP_READINESS_TIMEOUT_SECONDS` bounds the probe (default 2 seconds;
allowed range 0.1–30 seconds). Failures return a generic HTTP 503; `/healthz`
continues to report liveness. Custom repository implementations must implement
the asynchronous `check_health(timeout_seconds=...)` contract. Failed or timed-out
Postgres probes terminate their acquired connection rather than waiting for an
unbounded reset; successful probes return it to the pool.

Application logs omit Requests/urllib3 transport diagnostics, which can include
full query URLs and raw exceptions even during ordinary DEBUG logging. Connector
status, attempt counts and timing remain available without order/provider payloads.

Postgres:

```env
TITLE_MCP_STATE_BACKEND=postgres
TITLE_MCP_POSTGRES_DSN=postgresql://title_mcp:title_mcp@localhost:5432/title_mcp
```

Install the Postgres extra:

```bash
.venv/bin/pip install -e "packages/titlemcp[postgres]"
```

Repository saves require the revision returned by the latest read/save. A stale
human review raises `WorkflowConflictError`; the trusted host must reload and ask
the reviewer to decide again. Workers retry against current review state.
Startup and five-second recovery sweeps page queued/running records in batches
of 50. These transitions only plan work and are safe to replay. In-memory state
cannot survive process loss; Postgres preserves it. Before adding real external
executors, implement provider idempotency and distributed execution leases.
Background planning failures persist a sanitized failure where storage is available;
storage outages are logged and leave queued work eligible for a later sweep.

## Parcel Provider

Parcel lookup requires smart proxy configuration.

```env
TITLE_MCP_SMART_PROXY=user:password@proxy.example.com
TITLE_MCP_REGRID_PROXY_PORT_START=10001
TITLE_MCP_REGRID_PROXY_PORT_END=10999
TITLE_MCP_REGRID_MAX_PROXY_ATTEMPTS=10
TITLE_MCP_REGRID_TIMEOUT_SECONDS=10
TITLE_MCP_REGRID_COOKIE_TIMEOUT_SECONDS=5
TITLE_MCP_REGRID_QUERY_TIMEOUT_SECONDS=60
```

Notes:

- `SMART_PROXY` is also supported as a legacy variable.
- Set the smart proxy host/auth without a rotating port. TitleMCP appends ports
  from the configured range.
- The parcel connector disables `requests` environment proxy discovery so
  `TITLE_MCP_SMART_PROXY` is not mistaken for a generic proxy variable.
- The connector logs each proxy attempt with credentials redacted.
- Initialization and requests run in one background thread per connector. The
  query deadline bounds the response, not Python thread termination. A timed-out
  call can finish its bounded network attempts; overlapping calls return a busy
  failure until it finishes.

## PACER

```env
TITLE_MCP_PACER_USERNAME=
TITLE_MCP_PACER_PASSWORD=
TITLE_MCP_PACER_CLIENT_CODE=
TITLE_MCP_PACER_QA_MODE=true
TITLE_MCP_PACER_TIMEOUT_SECONDS=30
```

Use QA credentials and `TITLE_MCP_PACER_QA_MODE=true` for non-billable testing.
Production PACER queries may be billable.

## OFAC

OFAC screening needs no credentials: the lists are public. They are downloaded from
`sanctionslistservice.ofac.treas.gov` and cached.

```env
TITLE_MCP_OFAC_CACHE_DIR=~/.cache/titlemcp/ofac
TITLE_MCP_OFAC_MAX_AGE_HOURS=24
TITLE_MCP_OFAC_MAX_SCREENING_AGE_HOURS=24
TITLE_MCP_OFAC_TIMEOUT_SECONDS=120
```

`MAX_AGE_HOURS` controls refresh attempts. `MAX_SCREENING_AGE_HOURS` is the hard
maximum acceptable cache age, default 24 hours. After a failed refresh, a copy
within that limit produces a `partial` screening with freshness metadata and a
warning. An older copy or no valid copy produces `failed` with no screening
outcome. Empty lists, inconsistent advertised counts and missing identifiers or
names are rejected before atomically replacing a valid cache.

## SerpAPI

HOA contact lookup uses SerpAPI's Google Search API.

```env
TITLE_MCP_SERPAPI_API_KEY=
TITLE_MCP_SERPAPI_TIMEOUT_SECONDS=30
```

Keep API keys in `.env` or deployment secrets, never in sample code or committed
configuration.

## Ollama

```env
TITLE_MCP_OLLAMA_MODEL=qwen3
```

The sample clients also accept `--model`, for example:

```bash
python samples/franklin_county_ollama/ollama_client.py --model qwen3:8b
```

## Extension Loading

Most installed entry-point groups are discovered by default. County packages
using `title_mcp.plugins` require explicit activation:

```env
TITLE_MCP_LOAD_ENTRY_POINT_CAPABILITIES=true
TITLE_MCP_LOAD_ENTRY_POINT_ADAPTERS=true
TITLE_MCP_LOAD_ENTRY_POINT_SOURCES=true
TITLE_MCP_LOAD_ENTRY_POINT_VENDORS=true
TITLE_MCP_LOAD_ENTRY_POINT_TOOLSETS=true
TITLE_MCP_LOAD_ENTRY_POINT_PLUGINS=false
```

Disable a group only for locked-down deployments or focused tests.

Set `TITLE_MCP_LOAD_ENTRY_POINT_PLUGINS=true` to activate trusted installed county
plugins. They execute Python code. A capability manifest can be visible while its
plugin is disabled; verify `list_source_connectors` or the
`registered_source_ids` field of `list_title_capabilities` for actual activation.

## AWS Textract

Document-analysis workflows can be configured to use AWS integrations.

```env
TITLE_MCP_AWS_REGION=us-east-1
TITLE_MCP_TEXTRACT_S3_BUCKET=
```

Install the AWS extra:

```bash
.venv/bin/pip install -e "packages/titlemcp[aws]"
```
