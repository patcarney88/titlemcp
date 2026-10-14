from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

from title_mcp.adapters import create_default_adapter_registry
from title_mcp.domain.models import (
    Jurisdiction,
    OrderRef,
    ReviewDecision,
    TaskStatus,
    WorkflowKind,
    WorkflowRecord,
    WorkflowRequest,
    WorkflowStatus,
)
from title_mcp.mcp.server import create_mcp_server
from title_mcp.platform import TitleMCPPlatform
from title_mcp.security import AuthenticatedPrincipal, PrincipalKind, ReviewPermission
from title_mcp.settings import TitleMCPSettings
from title_mcp.state.base import WorkflowConflictError
from title_mcp.state.memory import InMemoryWorkflowRepository
from title_mcp.state.postgres import PostgresWorkflowRepository
from title_mcp.workflows.engine import WorkflowEngine

HUMAN = AuthenticatedPrincipal(
    subject="reviewer",
    kind=PrincipalKind.HUMAN,
    permissions={ReviewPermission.REVIEW_WORKFLOW},
)


class PausedSaveRepository(InMemoryWorkflowRepository):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.pause = True

    async def save(self, record):
        if self.pause and record.status is WorkflowStatus.WAITING_ON_EXTERNAL:
            self.pause = False
            self.entered.set()
            await self.release.wait()
        return await super().save(record)


class WorkflowRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = PausedSaveRepository()
        self.engines = []
        self.engine = self.make_engine()
        self.record = await self.engine.create(
            WorkflowRequest(
                kind=WorkflowKind.TAX_CERTIFICATE,
                order=OrderRef(file_number="offline"),
                jurisdiction=Jurisdiction(state="FL", county="Orange"),
            )
        )

    def make_engine(self):
        engine = WorkflowEngine(
            repository=self.repo,
            adapters=create_default_adapter_registry(include_entry_points=False),
            auto_start=False,
        )
        self.engines.append(engine)
        return engine

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.shutdown()

    async def approve(self):
        return await self.engine.submit_review(
            self.record.id, decision=ReviewDecision.APPROVE, reviewer="reviewer", principal=HUMAN
        )

    async def test_memory_revision_rejects_stale_save_and_reads_legacy_json(self):
        old = self.record.model_copy(deep=True)
        current = await self.repo.save(self.record)
        self.assertEqual(current.revision, self.record.revision + 1)
        with self.assertRaises(WorkflowConflictError):
            await self.repo.save(old)
        payload = current.model_dump(mode="json")
        del payload["revision"]
        self.assertEqual(WorkflowRecord.model_validate(payload).revision, 0)

    async def test_rejection_wins_over_stale_worker(self):
        await self.approve()
        worker = asyncio.create_task(self.engine.run_once(self.record.id))
        await asyncio.wait_for(self.repo.entered.wait(), 1)
        rejection = await self.make_engine().submit_review(
            self.record.id, decision=ReviewDecision.REJECT, reviewer="reviewer", principal=HUMAN
        )
        self.repo.release.set()
        result = await worker
        self.assertEqual(result.status, WorkflowStatus.CANCELLED)
        self.assertEqual(result.revision, rejection.revision)
        self.assertTrue(all(t.status is TaskStatus.QUEUED for t in result.tasks))

    async def test_two_engines_commit_only_one_transition(self):
        approved = await self.approve()
        worker = asyncio.create_task(self.engine.run_once(self.record.id))
        await asyncio.wait_for(self.repo.entered.wait(), 1)
        second = await self.make_engine().run_once(self.record.id)
        self.repo.release.set()
        first = await worker
        self.assertEqual(first.revision, approved.revision + 1)
        self.assertEqual(first, second)
        self.assertEqual(await self.engine.run_once(first.id), first)

    async def test_human_conflict_is_surfaced_without_retry(self):
        original = self.repo.get
        read = asyncio.Event()
        release = asyncio.Event()

        async def paused_get(workflow_id):
            snapshot = await original(workflow_id)
            read.set()
            await release.wait()
            return snapshot

        with patch.object(self.repo, "get", side_effect=paused_get):
            stale_approval = asyncio.create_task(self.approve())
            await asyncio.wait_for(read.wait(), 1)
            newer = await original(self.record.id)
            newer.status = WorkflowStatus.CANCELLED
            await self.repo.save(newer)
            release.set()
            with self.assertRaises(WorkflowConflictError):
                await stale_approval

    async def test_recovery_pages_all_work_and_repairs_running_tasks(self):
        self.repo.pause = False
        approved = await self.approve()
        for index in range(7):
            copy = approved.model_copy(deep=True)
            copy.id = f"recovery-{index}"
            copy.revision = 0
            copy.status = WorkflowStatus.RUNNING
            copy.tasks[0].status = TaskStatus.RUNNING
            await self.repo.save(copy)
        restarted = self.make_engine()
        counts = [await restarted.recover_page(limit=3) for _ in range(3)]
        await restarted.drain()
        self.assertEqual(sum(counts), 8)
        self.assertEqual(await self.repo.list_recoverable(), [])
        recovered = await self.repo.get("recovery-6")
        self.assertEqual(recovered.status, WorkflowStatus.WAITING_ON_EXTERNAL)
        self.assertTrue(all(t.status is TaskStatus.WAITING_ON_EXTERNAL for t in recovered.tasks))

    async def test_background_failure_is_sanitized_persisted_and_observed(self):
        await self.approve()
        with patch.object(self.engine, "run_once", side_effect=ValueError("private-provider-data")):
            self.engine.enqueue(self.record.id)
            with self.assertRaisesRegex(RuntimeError, "Background workflow"):
                await self.engine.drain()
        record = await self.repo.get(self.record.id)
        self.assertEqual(record.status, WorkflowStatus.FAILED)
        self.assertNotIn("private-provider-data", record.model_dump_json())

    async def test_cancellation_leaves_work_recoverable(self):
        approved = await self.approve()
        self.engine.enqueue(approved.id)
        await asyncio.wait_for(self.repo.entered.wait(), 1)
        await self.engine.shutdown()
        self.assertEqual((await self.repo.get(approved.id)).status, WorkflowStatus.QUEUED)

    async def test_restart_clears_ids_cancelled_before_worker_entry(self):
        approved = await self.approve()
        self.engine.enqueue(approved.id)
        # No yield: this task has not entered its wrapper when cancelled.
        await self.engine.shutdown()
        self.repo.pause = False
        await self.engine.start_recovery()
        await self.engine.drain()
        self.assertEqual(
            (await self.repo.get(approved.id)).status, WorkflowStatus.WAITING_ON_EXTERNAL
        )

    async def test_failure_handler_does_not_overwrite_concurrent_rejection(self):
        await self.approve()

        async def fails_after_rejection(workflow_id):
            await self.make_engine().submit_review(
                workflow_id, decision=ReviewDecision.REJECT, reviewer="reviewer", principal=HUMAN
            )
            raise RuntimeError("private-provider-data")

        with patch.object(self.engine, "run_once", side_effect=fails_after_rejection):
            self.engine.enqueue(self.record.id)
            await self.engine.drain()
        self.assertEqual((await self.repo.get(self.record.id)).status, WorkflowStatus.CANCELLED)


class PostgresRevisionContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_uses_atomic_revision_predicate_and_rejects_missing_row(self):
        repo = PostgresWorkflowRepository("unused-local-fake")
        conn = AsyncMock()
        context = AsyncMock()
        context.__aenter__.return_value = conn

        class Pool:
            def acquire(self):
                return context

        repo._pool = Pool()
        record = WorkflowRecord(
            kind=WorkflowKind.TAX_CERTIFICATE,
            order=OrderRef(file_number="offline"),
            jurisdiction=Jurisdiction(state="FL"),
            revision=2,
        )
        conn.fetchrow.return_value = {
            "record": record.model_copy(update={"revision": 3}).model_dump_json()
        }
        saved = await repo.save(record)
        query, *values = conn.fetchrow.call_args.args
        self.assertIn("WHERE id = $1 AND COALESCE((record->>'revision')::bigint, 0) = $9", query)
        self.assertEqual(values[-1], 2)
        self.assertEqual(json.loads(values[5])["revision"], 3)
        self.assertEqual(saved.revision, 3)
        conn.fetchrow.return_value = None
        with self.assertRaises(WorkflowConflictError):
            await repo.save(record)
        record.revision = 0
        with self.assertRaises(WorkflowConflictError):
            await repo.save(record)
        self.assertIn("ON CONFLICT", conn.fetchrow.call_args.args[0])


class PlatformLifetimeTests(unittest.TestCase):
    def test_http_apps_own_one_platform_lifetime(self):
        for transport in ("sse", "streamable-http"):
            with self.subTest(transport=transport):
                platform = TitleMCPPlatform(
                    settings=TitleMCPSettings(
                        load_entry_point_plugins=False,
                        load_entry_point_adapters=False,
                        load_entry_point_capabilities=False,
                        load_entry_point_sources=False,
                        load_entry_point_toolsets=False,
                    )
                )
                with patch.object(platform, "initialize", new_callable=AsyncMock) as start:
                    with patch.object(platform, "shutdown", new_callable=AsyncMock) as stop:
                        server = create_mcp_server(platform=platform)
                        app = (
                            server.sse_app() if transport == "sse" else server.streamable_http_app()
                        )
                        with TestClient(app) as client:
                            self.assertEqual(client.get("/healthz").status_code, 200)
                            self.assertEqual(client.get("/healthz").status_code, 200)
                            self.assertEqual(start.await_count, 1)
                            self.assertEqual(stop.await_count, 0)
                        self.assertEqual(stop.await_count, 1)


class StdioLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_failure_releases_platform(self):
        from mcp.server.mcpserver import MCPServer

        platform = TitleMCPPlatform(settings=TitleMCPSettings())
        server = create_mcp_server(platform=platform)
        with patch.object(platform, "initialize", new_callable=AsyncMock) as start:
            with patch.object(platform, "shutdown", new_callable=AsyncMock) as stop:
                with patch.object(
                    MCPServer,
                    "run_stdio_async",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("synthetic closed input"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "synthetic closed input"):
                        await server.run_stdio_async()
                start.assert_awaited_once()
                stop.assert_awaited_once()
