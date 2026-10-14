FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TITLE_MCP_ENVIRONMENT=docker \
    TITLE_MCP_LOG_JSON=true \
    TITLE_MCP_MCP_TRANSPORT=streamable-http \
    TITLE_MCP_MCP_HOST=0.0.0.0 \
    TITLE_MCP_MCP_PORT=8000

# A hosting platform ends TLS at its own proxy and passes the original scheme on
# in X-Forwarded-Proto. Trusting it lets the catalog at / advertise https URLs.
ENV FORWARDED_ALLOW_IPS=*

# Build context has no .git. The caller must supply both identities; no registry
# lookup may assign another release's identity to this source tree.
ARG TITLEMCP_VERSION=
ARG SOURCE_REVISION=
LABEL org.opencontainers.image.version=$TITLEMCP_VERSION \
    org.opencontainers.image.revision=$SOURCE_REVISION

WORKDIR /app

COPY scripts/validate_image_identity.py scripts/validate_image_identity.py
COPY constraints-docker.txt constraints-docker.txt
RUN python scripts/validate_image_identity.py "$TITLEMCP_VERSION" "$SOURCE_REVISION"

# A system user's home is /nonexistent unless one is given. Caches such as the
# OFAC sanctions lists are kept under ~/.cache, so the user needs a real one.
RUN addgroup --system titlemcp \
    && adduser --system --ingroup titlemcp --home /home/titlemcp titlemcp

COPY packages/titlemcp/pyproject.toml \
     packages/titlemcp/README.md \
     packages/titlemcp/LICENSE \
     packages/titlemcp/
COPY packages/titlemcp/src packages/titlemcp/src

COPY packages/jurisdictions/us/oh/franklin/recorder/pyproject.toml \
     packages/jurisdictions/us/oh/franklin/recorder/README.md \
     packages/jurisdictions/us/oh/franklin/recorder/titlemcp-capability.toml \
     packages/jurisdictions/us/oh/franklin/recorder/
COPY packages/jurisdictions/us/oh/franklin/recorder/src \
     packages/jurisdictions/us/oh/franklin/recorder/src

RUN python -m pip install -c constraints-docker.txt hatchling hatch-vcs \
    && SETUPTOOLS_SCM_PRETEND_VERSION="$TITLEMCP_VERSION" python -m pip install \
        --no-build-isolation -c constraints-docker.txt \
        "packages/titlemcp[postgres]" \
        "packages/jurisdictions/us/oh/franklin/recorder"

USER titlemcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, socket; s = socket.create_connection(('127.0.0.1', int(os.getenv('PORT') or os.getenv('TITLE_MCP_MCP_PORT', '8000'))), timeout=3); s.close()"

# Hosting platforms (Render, Heroku, Cloud Run, Railway and others) choose the
# port and pass it as PORT. When it is set, the server listens there.
CMD ["sh", "-c", "TITLE_MCP_MCP_PORT=\"${PORT:-$TITLE_MCP_MCP_PORT}\" exec titlemcp-server"]
