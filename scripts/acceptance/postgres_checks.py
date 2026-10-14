"""Explicit local-only acceptance against the runner's disposable Postgres database."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

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
from title_mcp.security import AuthenticatedPrincipal, PrincipalKind, ReviewPermission
from title_mcp.settings import TitleMCPSettings
from title_mcp.state.base import WorkflowConflictError
from title_mcp.state.postgres import PostgresWorkflowRepository
from title_mcp.workflows.engine import WorkflowEngine

HUMAN = AuthenticatedPrincipal(
    subject="synthetic-local-reviewer",
    kind=PrincipalKind.HUMAN,
    permissions={ReviewPermission.REVIEW_WORKFLOW},
)


def repository() -> PostgresWorkflowRepository:
    run_id = os.environ.get("TITLEMCP_ACCEPTANCE_RUN", "")
    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise RuntimeError("Use the disposable local acceptance runner.")
    settings = TitleMCPSettings(_env_file=None)
    parts = urlsplit(settings.postgres_dsn or "")
    if (
        parts.hostname != f"titlemcp-accept-{run_id}-db"
        or parts.path != f"/titlemcp_acceptance_{run_id}"
        or parts.username != "acceptance_user"
        or parts.port != 5432
    ):
        raise RuntimeError("Only the runner's dedicated database is permitted.")
    return PostgresWorkflowRepository(settings.postgres_dsn)


def engine(repo: PostgresWorkflowRepository) -> WorkflowEngine:
    return WorkflowEngine(
        repository=repo,
        adapters=create_default_adapter_registry(include_entry_points=False),
        concurrency=1,
        auto_start=False,
    )


def request() -> WorkflowRequest:
    return WorkflowRequest(
        kind=WorkflowKind.TAX_CERTIFICATE,
        order=OrderRef(file_number="SYNTHETIC-ACCEPTANCE"),
        jurisdiction=Jurisdiction(state="FL", county="Orange"),
    )


class PostgresAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.first, self.second = repository(), repository()
        await self.first.initialize()
        await self.second.initialize()
        async with self.first._pool.acquire() as conn:
            self.assertEqual(
                await conn.fetchval("SELECT current_database()"),
                f"titlemcp_acceptance_{os.environ['TITLEMCP_ACCEPTANCE_RUN']}",
            )
            # This database is created for this invocation; the guard above is mandatory.
            await conn.execute("TRUNCATE title_mcp_workflows")
        self.worker = engine(self.first)
        self.other = engine(self.second)

    async def asyncTearDown(self):
        await self.worker.shutdown()
        await self.other.shutdown()
        await self.first.close()
        await self.second.close()

    async def approved(self):
        record = await self.worker.create(request())
        return await self.worker.submit_review(
            record.id, decision=ReviewDecision.APPROVE, reviewer=HUMAN.subject, principal=HUMAN
        )

    async def test_01_round_trip_filters_and_reopen(self):
        record = await self.worker.create(request())
        self.assertEqual(record.revision, 1)
        self.assertEqual(await self.second.get(record.id), record)
        listed = await self.second.list(
            file_number="SYNTHETIC-ACCEPTANCE",
            kind=WorkflowKind.TAX_CERTIFICATE,
            status=WorkflowStatus.NEEDS_HUMAN_REVIEW,
        )
        self.assertEqual([row.id for row in listed], [record.id])
        await self.first.close()
        await self.first.initialize()
        self.assertEqual(await self.first.get(record.id), record)

    async def test_02_simultaneous_updates_commit_one_revision(self):
        original = await self.worker.create(request())
        one = await self.first.get(original.id)
        two = await self.second.get(original.id)
        one.add_audit("acceptance.writer_one")
        two.add_audit("acceptance.writer_two")
        results = await asyncio.gather(
            self.first.save(one), self.second.save(two), return_exceptions=True
        )
        self.assertEqual(sum(isinstance(r, WorkflowRecord) for r in results), 1)
        self.assertEqual(sum(isinstance(r, WorkflowConflictError) for r in results), 1)
        saved = await self.first.get(original.id)
        self.assertEqual(saved.revision, original.revision + 1)
        self.assertEqual(len(saved.audit), len(original.audit) + 1)

    async def test_03_deleted_record_is_not_resurrected_by_update(self):
        record = await self.worker.create(request())
        async with self.first._pool.acquire() as conn:
            await conn.execute("DELETE FROM title_mcp_workflows WHERE id=$1", record.id)
        with self.assertRaises(WorkflowConflictError):
            await self.second.save(record)
        self.assertIsNone(await self.first.get(record.id))

    async def test_04_revisionless_historical_json_uses_atomic_zero_transition(self):
        record = await self.worker.create(request())
        async with self.first._pool.acquire() as conn:
            await conn.execute(
                "UPDATE title_mcp_workflows SET record=record-'revision' WHERE id=$1", record.id
            )
        one = await self.first.get(record.id)
        two = await self.second.get(record.id)
        self.assertEqual(one.revision, 0)
        results = await asyncio.gather(
            self.first.save(one), self.second.save(two), return_exceptions=True
        )
        self.assertEqual(sum(isinstance(r, WorkflowConflictError) for r in results), 1)
        self.assertEqual((await self.first.get(record.id)).revision, 1)

    async def test_05_rejection_survives_stale_worker_with_full_audit(self):
        record = await self.approved()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.first.save

        async def paused_save(value):
            if value.status is WorkflowStatus.WAITING_ON_EXTERNAL:
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
            return await original(value)

        with patch.object(self.first, "save", side_effect=paused_save):
            pending = asyncio.create_task(self.worker.run_once(record.id))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                rejected = await self.other.submit_review(
                    record.id,
                    decision=ReviewDecision.REJECT,
                    reviewer=HUMAN.subject,
                    principal=HUMAN,
                )
            finally:
                release.set()
            result = await asyncio.wait_for(pending, 5)
        self.assertEqual(result, rejected)
        self.assertEqual(result.status, WorkflowStatus.CANCELLED)
        self.assertEqual(
            [
                e.payload.get("decision")
                for e in result.audit
                if e.action == "workflow.review_submitted"
            ],
            ["approve", "reject"],
        )
        self.assertTrue(all(t.status is TaskStatus.QUEUED for t in result.tasks))

    async def test_06_two_engines_commit_only_one_planning_transition(self):
        record = await self.approved()
        results = await asyncio.gather(
            self.worker.run_once(record.id), self.other.run_once(record.id)
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0].revision, record.revision + 1)
        self.assertEqual(results[0].status, WorkflowStatus.WAITING_ON_EXTERNAL)

    async def test_07_bounded_recovery_pages_all_running_and_queued_records(self):
        original = await self.approved()
        for index in range(6):
            copy = original.model_copy(deep=True)
            copy.id, copy.revision = f"acceptance-recovery-{index}", 0
            if index % 2:
                copy.status = WorkflowStatus.RUNNING
                copy.tasks[0].status = TaskStatus.RUNNING
            await self.first.save(copy)
        restarted = engine(self.second)
        try:
            counts = [await restarted.recover_page(limit=2) for _ in range(4)]
            await restarted.drain()
            self.assertEqual(sum(counts), 7)
            self.assertEqual(await self.first.list_recoverable(), [])
            for row in await self.first.list():
                self.assertEqual(row.status, WorkflowStatus.WAITING_ON_EXTERNAL)
                self.assertTrue(all(t.status is TaskStatus.WAITING_ON_EXTERNAL for t in row.tasks))
        finally:
            await restarted.shutdown()

    async def test_08_background_failure_is_durable_and_sanitized(self):
        record = await self.approved()
        with patch.object(
            self.worker, "run_once", side_effect=ValueError("synthetic-private-data")
        ):
            self.worker.enqueue(record.id)
            with self.assertRaises(RuntimeError):
                await self.worker.drain()
        saved = await self.second.get(record.id)
        self.assertEqual(saved.status, WorkflowStatus.FAILED)
        self.assertIn("workflow.execution_failed", [e.action for e in saved.audit])
        self.assertNotIn("synthetic-private-data", saved.model_dump_json())


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
