"""Probe the disposable acceptance server during real Postgres outages."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from postgres_checks import repository

from title_mcp.settings import TitleMCPSettings

LATENCY_MARGIN_SECONDS = 1.0
RECOVERY_WAIT_SECONDS = 10.0
PROBES = 3


async def pending_workflow(base: str, headers: dict[str, str], workflow_id: str) -> None:
    async with asyncio.timeout(10):
        async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=2) as client:
            async with streamable_http_client(base + "/mcp", http_client=client) as streams:
                async with ClientSession(*streams, read_timeout_seconds=5) as session:
                    await session.initialize()
                    response = await session.call_tool(
                        "get_workflow_status", {"workflow_id": workflow_id}
                    )
                    assert not response.is_error, "Workflow read failed after database restoration"
                    record = response.structured_content["data"]
                    assert record["id"] == workflow_id
                    assert record["status"] == "needs_human_review"
                    assert record["revision"] == 1, "The outage must not change workflow state"


async def check_readiness(
    expected_status: int,
    workflow_id: str | None = None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> dict:
    repository()  # Validate the runner's disposable database identity; do not connect here.
    assert expected_status in {200, 503}
    settings = TitleMCPSettings(_env_file=None)
    assert settings.mcp_access_token is not None
    base = f"http://127.0.0.1:{int(os.environ['PORT'])}"
    headers = {"Authorization": "Bearer " + settings.mcp_access_token.get_secret_value()}
    budget = settings.readiness_timeout_seconds + LATENCY_MARGIN_SECONDS
    latencies = []
    payload = {"name": settings.app_name, "environment": settings.environment}

    async with httpx2.AsyncClient(trust_env=False, timeout=budget, transport=transport) as client:

        async def probe() -> int:
            started = time.monotonic()
            async with asyncio.timeout(budget):
                response = await client.get(base + "/readyz", headers=headers)
            elapsed = time.monotonic() - started
            assert elapsed < budget, "Readiness response exceeded its acceptance budget"
            latencies.append(elapsed)
            assert response.status_code in {200, 503}, "Unexpected readiness response"
            expected = (
                {**payload, "status": "ready"}
                if response.status_code == 200
                else {**payload, "status": "error", "error": "Service readiness check failed."}
            )
            assert response.json() == expected, "Readiness response was not sanitized"
            return response.status_code

        started = time.monotonic()
        if expected_status == 200:
            async with asyncio.timeout(RECOVERY_WAIT_SECONDS):
                while await probe() != 200:
                    await asyncio.sleep(0.2)
        else:
            assert await probe() == 503, "Database outage was incorrectly reported as ready"
        recovery_wait = time.monotonic() - started
        for _ in range(PROBES - 1):
            assert await probe() == expected_status, "Readiness status was unstable"
        async with asyncio.timeout(budget):
            assert (await client.get(base + "/healthz")).status_code == 200
            assert (await client.get(base + "/readyz")).status_code == 401
            assert (
                await client.get(base + "/readyz", headers={"Authorization": "Bearer invalid"})
            ).status_code == 401

    result = {
        "readiness_status": expected_status,
        "stable_probes": PROBES,
        "total_probe_attempts": len(latencies),
        "configured_timeout_seconds": settings.readiness_timeout_seconds,
        "response_budget_seconds": budget,
        "max_response_seconds": round(max(latencies), 6),
        "health_status": 200,
        "anonymous_readiness_status": 401,
        "invalid_readiness_status": 401,
        "sanitized_response": True,
    }
    if expected_status == 200:
        result["recovery_wait_seconds"] = round(recovery_wait, 6)
        if workflow_id is not None:
            await pending_workflow(base, headers, workflow_id)
            result["workflow_preserved"] = True
    return result


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    print(
        json.dumps(
            asyncio.run(
                check_readiness(int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None)
            )
        )
    )
