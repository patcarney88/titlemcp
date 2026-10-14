from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from starlette.testclient import TestClient
from test_instance_security import HTTP_CREDENTIAL, settings

from title_mcp.mcp.server import create_mcp_server
from title_mcp.platform import TitleMCPPlatform
from title_mcp.state.memory import InMemoryWorkflowRepository
from title_mcp.state.postgres import PostgresWorkflowRepository


class ReadinessTests(unittest.TestCase):
    def test_repository_failure_after_startup_returns_503_and_can_recover(self):
        for transport in ("sse", "streamable-http"):
            with self.subTest(transport=transport):
                repo = InMemoryWorkflowRepository()
                platform = TitleMCPPlatform(
                    settings=settings(mcp_access_token=HTTP_CREDENTIAL), repository=repo
                )
                server = create_mcp_server(platform=platform)
                app = server.sse_app() if transport == "sse" else server.streamable_http_app()
                with TestClient(app) as client:
                    headers = {"Authorization": f"Bearer {HTTP_CREDENTIAL}"}
                    self.assertEqual(client.get("/readyz", headers=headers).status_code, 200)
                    with patch.object(
                        repo,
                        "check_health",
                        new=AsyncMock(side_effect=RuntimeError("private-database-error")),
                        create=True,
                    ) as probe:
                        response = client.get("/readyz", headers=headers)
                        self.assertEqual(response.status_code, 503)
                        self.assertNotIn("private-database-error", response.text)
                        probe.assert_awaited_once()
                        self.assertEqual(client.get("/readyz").status_code, 401)
                        self.assertEqual(client.get("/healthz").status_code, 200)
                        probe.assert_awaited_once()
                    self.assertEqual(client.get("/readyz", headers=headers).status_code, 200)

    def test_probe_timeout_cancels_work_and_returns_503(self):
        cancelled = []

        async def stalled(*, timeout_seconds):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        repo = InMemoryWorkflowRepository()
        platform = TitleMCPPlatform(
            settings=settings(mcp_access_token=HTTP_CREDENTIAL, readiness_timeout_seconds=0.1),
            repository=repo,
        )
        server = create_mcp_server(platform=platform)
        with TestClient(server.streamable_http_app()) as client:
            with patch.object(repo, "check_health", new=stalled, create=True):
                response = client.get(
                    "/readyz", headers={"Authorization": f"Bearer {HTTP_CREDENTIAL}"}
                )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(cancelled, [True])

    def test_stopped_recovery_loop_is_not_ready(self):
        platform = TitleMCPPlatform(settings=settings(mcp_access_token=HTTP_CREDENTIAL))
        server = create_mcp_server(platform=platform)

        async def stop_recovery():
            platform.engine._recovery_task.cancel()
            await asyncio.gather(platform.engine._recovery_task, return_exceptions=True)

        with TestClient(server.streamable_http_app()) as client:
            client.portal.call(stop_recovery)
            response = client.get("/readyz", headers={"Authorization": f"Bearer {HTTP_CREDENTIAL}"})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(client.get("/healthz").status_code, 200)


class PostgresHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_connection_and_workflow_table_are_probed_each_time(self):
        repo = PostgresWorkflowRepository("unused-local-fake")
        connection = Mock(fetchval=AsyncMock())
        repo._pool = SimpleNamespace(
            acquire=AsyncMock(return_value=connection), release=AsyncMock()
        )
        await repo.check_health()
        self.assertEqual(
            connection.fetchval.call_args.args, ("SELECT 1 FROM title_mcp_workflows LIMIT 1",)
        )
        self.assertGreater(connection.fetchval.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(connection.fetchval.call_args.kwargs["timeout"], 2)
        repo._pool.release.assert_awaited_once()
        connection.fetchval.side_effect = RuntimeError("synthetic connection failure")
        with self.assertRaises(RuntimeError):
            await repo.check_health()
        self.assertEqual(connection.fetchval.await_count, 2)
        connection.execute.assert_not_called()
        connection.terminate.assert_called_once()

    async def test_query_and_release_stalls_terminate_the_probe_connection(self):
        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()

        for phase in ("query", "release"):
            with self.subTest(phase=phase):
                connection = Mock(fetchval=AsyncMock())
                pool = SimpleNamespace(
                    acquire=AsyncMock(return_value=connection), release=AsyncMock()
                )
                operation = connection.fetchval if phase == "query" else pool.release
                operation.side_effect = stalled
                repo = PostgresWorkflowRepository("unused-local-fake")
                repo._pool = pool
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(repo.check_health(timeout_seconds=0.02), timeout=0.5)
                connection.terminate.assert_called_once()
                if phase == "release":
                    self.assertGreater(pool.release.call_args.kwargs["timeout"], 0)
                    self.assertLessEqual(pool.release.call_args.kwargs["timeout"], 0.02)

    async def test_closed_repository_is_not_reinitialized_by_a_probe(self):
        repo = PostgresWorkflowRepository("unused-local-fake")
        with patch.object(repo, "initialize", new_callable=AsyncMock) as initialize:
            with self.assertRaises(RuntimeError):
                await repo.check_health()
            initialize.assert_not_awaited()

    async def test_shielded_pool_reset_unblocks_when_probe_connection_is_terminated(self):
        terminated, reset_finished = asyncio.Event(), asyncio.Event()
        connection = Mock(fetchval=AsyncMock(), terminate=terminated.set)

        async def reset():
            await terminated.wait()
            reset_finished.set()

        async def release(*args, **kwargs):
            await asyncio.shield(reset())

        repo = PostgresWorkflowRepository("unused-local-fake")
        repo._pool = SimpleNamespace(acquire=AsyncMock(return_value=connection), release=release)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(repo.check_health(timeout_seconds=0.02), timeout=0.5)
        await asyncio.wait_for(reset_finished.wait(), timeout=0.5)


if __name__ == "__main__":
    unittest.main()
