"""Exercise the installed image and real local MCP/HTTP transport, never providers."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import platform
import sys
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from postgres_checks import HUMAN, engine, repository, request

from title_mcp.domain.models import ReviewDecision, TaskStatus, WorkflowStatus


def identity() -> dict:
    assert os.getuid() != 0, "Image must run as a non-root user"
    version = importlib.metadata.version("titlemcp")
    assert version == os.environ["TITLEMCP_ACCEPTANCE_VERSION"]
    pins = [
        line.strip().split("==")
        for line in Path("/app/constraints-docker.txt").read_text().splitlines()
        if line and not line.startswith(("#", " "))
    ]
    for name, pinned in pins:
        assert importlib.metadata.version(name) == pinned, f"Dependency mismatch: {name}"
    assert sys.version_info[:2] == (3, 12)
    cgroup = Path("/sys/fs/cgroup")
    memory_limit = int((cgroup / "memory.max").read_text())
    quota, period = map(int, (cgroup / "cpu.max").read_text().split())
    assert memory_limit <= 384 * 1024 * 1024
    assert quota / period <= 0.5
    assert int((cgroup / "pids.max").read_text()) <= 128
    assert (cgroup / "memory.swap.max").read_text().strip() == "0"
    return {
        "version": version,
        "uid": os.getuid(),
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "dependency_pins_checked": len(pins),
        "memory_limit_bytes": memory_limit,
        "cpu_limit": quota / period,
    }


async def seed(workflow_id: str) -> dict:
    repo = repository()
    await repo.initialize()
    worker = engine(repo)
    try:
        async with repo._pool.acquire() as connection:
            database_version = await connection.fetchval("SHOW server_version")
        approved = await worker.submit_review(
            workflow_id, decision=ReviewDecision.APPROVE, reviewer=HUMAN.subject, principal=HUMAN
        )
        assert approved.status is WorkflowStatus.QUEUED
        ids = [workflow_id]
        for index in range(6):
            copy = approved.model_copy(deep=True)
            copy.id = f"seeded-{os.environ['TITLEMCP_ACCEPTANCE_RUN']}-{index}"
            copy.revision = 0
            if index % 2:
                copy.status = WorkflowStatus.RUNNING
                copy.tasks[0].status = TaskStatus.RUNNING
            await repo.save(copy)
            ids.append(copy.id)
        pending = await worker.create(request())
        rejected = await worker.create(request())
        rejected = await worker.submit_review(
            rejected.id, decision=ReviewDecision.REJECT, reviewer=HUMAN.subject, principal=HUMAN
        )
        return {
            "recoverable_ids": ids,
            "pending_id": pending.id,
            "rejected_id": rejected.id,
            "database_version": database_version,
        }
    finally:
        await worker.shutdown()
        await repo.close()


async def http_checks(mode: str, seeded: dict | None = None) -> dict:
    installed = identity()
    base = f"http://127.0.0.1:{os.environ['PORT']}"
    token = os.environ["TITLE_MCP_MCP_ACCESS_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(trust_env=False, timeout=10) as http:
        for _ in range(60):
            try:
                response = await http.get(base + "/readyz", headers=headers)
                if response.status_code == 200:
                    break
            except httpx2.TransportError:
                pass
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("Server did not become ready on PORT within 15 seconds")
        assert (await http.get(base + "/healthz")).status_code == 200
        for path in ("/", "/readyz", "/mcp", "/sse", "/messages/"):
            assert (await http.get(base + path)).status_code == 401, path
        assert (
            await http.get(base + "/", headers={"Authorization": "Bearer invalid"})
        ).status_code == 401
        catalog = (await http.get(base + "/", headers=headers)).json()
        names = {tool["name"] for tool in catalog["tools"]}
        assert "start_title_workflow" in names
        assert "submit_review" not in names
        forwarded = (
            await http.get(
                base + "/",
                headers={
                    **headers,
                    "Host": "titlemcp.acceptance.invalid",
                    "X-Forwarded-Proto": "https",
                },
            )
        ).json()
        assert forwarded["mcp"]["url"] == "https://titlemcp.acceptance.invalid/mcp"
    async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=10) as client:
        async with streamable_http_client(base + "/mcp", http_client=client) as streams:
            async with ClientSession(*streams, read_timeout_seconds=10) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert "submit_review" not in {tool.name for tool in tools.tools}
                sources = (await session.call_tool("list_source_connectors", {})).structured_content
                assert "franklin" in json.dumps(sources).lower(), (
                    "Installed county entry point missing"
                )
                if mode == "create":
                    result = await session.call_tool(
                        "start_title_workflow",
                        {
                            "kind": "tax_certificate",
                            "file_number": "SYNTHETIC-DOCKER-ACCEPTANCE",
                            "state": "FL",
                            "county": "Orange",
                            "require_human_review": False,
                        },
                    )
                    assert not result.is_error
                    created = result.structured_content
                    assert created["status"] == "needs_human_review"
                    assert created["data"]["revision"] == 1
                    return {
                        "workflow_id": created["workflow_id"],
                        "tool_count": len(tools.tools),
                        "port_override": int(os.environ["PORT"]),
                        "http_auth": "passed",
                        "forwarded_url": "passed",
                        "mcp_session": "passed",
                        "installed_county_discovery": "passed",
                        "runtime_memory_limit_bytes": installed["memory_limit_bytes"],
                    }
                assert seeded is not None
                for workflow_id in seeded["recoverable_ids"]:
                    for _ in range(30):
                        result = await session.call_tool(
                            "get_workflow_status", {"workflow_id": workflow_id}
                        )
                        record = result.structured_content["data"]
                        if record["status"] == "waiting_on_external":
                            break
                        await asyncio.sleep(0.1)
                    assert record["status"] == "waiting_on_external", workflow_id
                    assert all(t["status"] == "waiting_on_external" for t in record["tasks"])
                    assert all(t["completed_at"] is None for t in record["tasks"])
                    assert any(e["action"] == "workflow.review_submitted" for e in record["audit"])
                for key, expected in (
                    ("pending_id", "needs_human_review"),
                    ("rejected_id", "cancelled"),
                ):
                    result = await session.call_tool(
                        "get_workflow_status", {"workflow_id": seeded[key]}
                    )
                    assert result.structured_content["status"] == expected
                return {
                    "startup_recovery_count": len(seeded["recoverable_ids"]),
                    "database_version": seeded["database_version"],
                    "pending_review_preserved": True,
                    "rejection_preserved": True,
                    "tasks_remain_planned": True,
                    "http_auth": "passed",
                    "mcp_session": "passed",
                }


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    command = sys.argv[1]
    if command == "identity":
        result = identity()
    elif command == "seed":
        result = asyncio.run(seed(sys.argv[2]))
    else:
        result = asyncio.run(
            http_checks(command, json.loads(sys.argv[2]) if len(sys.argv) > 2 else None)
        )
    print(json.dumps(result))
