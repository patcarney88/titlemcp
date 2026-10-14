# Deploying TitleMCP

This personal fork supports manual Docker builds with explicit version and source
identity. Deployment has not been performed. When a configured server runs, it serves:

| Path | What it is |
| --- | --- |
| `/mcp` | The MCP endpoint that clients connect to. |
| `/` | A JSON catalog of the tools, resources and prompts, with the endpoint's full URL. |
| `/healthz` | A health check. |

The upstream hosting manifests remain reference material. They must be adapted
to supply both required Docker build arguments; they are not one-click setups
for this fork.

## Before you deploy

- **HTTP access is private and single tenant.** Set
  `TITLE_MCP_MCP_ACCESS_TOKEN` to a unique, high-entropy instance credential of at
  least 32 visible ASCII characters. All application requests require its Bearer
  Authorization header; only `/healthz` is public. Missing configuration fails
  closed. Use TLS and private ingress, and isolate each tenant's instance, state
  and connector credentials. Authorized agents share access to the instance's
  orders and paid APIs. This is not a multi-tenant sign-in or OAuth service. See
  [Configuration](CONFIGURATION.md#server) for the separate trusted-human review
  boundary. No credentials or deployment are configured by these instructions.
- **State is kept in memory.** Workflow records are lost when the service
  restarts or redeploys, and free instances restart often. For durable state,
  set `TITLE_MCP_STATE_BACKEND=postgres` and `TITLE_MCP_POSTGRES_DSN` (see
  [Configuration](CONFIGURATION.md#state-backend)).
- **Memory.** The server idles at about 60 MB. The first OFAC screening
  downloads and parses the sanctions lists and takes it to about 450 MB. A
  512 MB instance runs it; 1 GB leaves room.
- **Version.** Supply `TITLEMCP_VERSION` and the full `SOURCE_REVISION` commit SHA.
  Missing identity fails the build. No PyPI metadata request assigns a version.
  `constraints-docker.txt` pins runtime and build dependencies for Python 3.12/Linux.
  The base image still uses `python:3.12-slim`; pin its digest separately when a
  reproducible release is authorized. Current checks do not establish bit-identical images.

## Platforms

| Platform | Configured by | Notes |
| --- | --- | --- |
| Render | [`render.yaml`](../render.yaml) | Uses the free plan. Free instances stop when idle, and the first request afterwards waits while one starts. Automatic deploys are off, so redeploy from the dashboard to pick up new commits. |
| Heroku | [`app.json`](../app.json), [`heroku.yml`](../heroku.yml) | Builds on the container stack. Heroku has no free plan. |
| Google Cloud Run | [`app.json`](../app.json) | Runs from Cloud Shell and needs a Google Cloud project with billing. The service accepts unauthenticated requests. Cloud Run's default is 512 MiB; for OFAC screening, raise it with `gcloud run services update titlemcp --memory 1Gi`. |
| DigitalOcean App Platform | [`.do/deploy.template.yaml`](../.do/deploy.template.yaml) | Health-checks `/healthz` on port 8000. |
| Koyeb | The button's link | Serves port 8000. |

Any other host that builds a Dockerfile can run it too, such as Railway, Fly.io,
Azure Container Apps or AWS App Runner. The container:

- listens on `PORT` when the platform sets it, and on 8000 otherwise;
- answers health checks at `GET /healthz`;
- trusts the proxy's `X-Forwarded-Proto`, so the catalog advertises `https://`
  URLs. Set `TITLE_MCP_MCP_PUBLIC_URL` to advertise a different address.

## Configure it

Add `TITLE_MCP_*` variables in the platform's environment settings. Every
setting is described in [Configuration](CONFIGURATION.md). The service picks
them up when it restarts.

## Connect a client

Check that the deployment answers:

```bash
curl https://<your-app>/healthz
```

Then point an MCP client supporting custom Authorization headers at
`https://<your-app>/mcp`, with `Authorization: Bearer <instance-credential>`.
URL-only client registration will receive HTTP 401. Never place the credential
in a URL, shell history or a shared configuration file.

- **MCP Inspector:** run `npx @modelcontextprotocol/inspector`, choose
  Streamable HTTP, enter the URL and configure the Authorization header.

## Build the image yourself

```bash
export SOURCE_REVISION=$(git rev-parse HEAD)
export TITLEMCP_VERSION="0.3.7+fork.${SOURCE_REVISION}"
docker build --build-arg TITLEMCP_VERSION --build-arg SOURCE_REVISION -t titlemcp .
docker run --env-file .env -p 127.0.0.1:8000:8000 titlemcp
```

A release image is built with its version:

```bash
docker build --build-arg TITLEMCP_VERSION=0.3.7 \
  --build-arg SOURCE_REVISION="$(git rev-parse v0.3.7^{commit})" -t titlemcp:0.3.7 .
```

Build a release only from the corresponding clean tagged checkout. The fork
example above is a local build identity, not an assertion of an upstream release.
Regenerate dependency constraints deliberately after reviewing version changes:
`uv pip compile --python-version 3.12 --python-platform x86_64-unknown-linux-gnu
--output-file constraints-docker.txt scripts/docker-requirements.in`.

For local work with MCP Inspector alongside, use `docker compose up --build` as
described in the [README](../README.md#core-package).
