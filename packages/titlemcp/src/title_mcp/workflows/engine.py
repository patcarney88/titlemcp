from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from title_mcp.adapters.registry import AdapterRegistry
from title_mcp.domain.models import (
    AuditActor,
    HumanReviewRequest,
    ReviewDecision,
    ReviewStatus,
    TaskStatus,
    WorkflowRecord,
    WorkflowRequest,
    WorkflowStatus,
    WorkflowTask,
    utc_now,
)
from title_mcp.observability import current_trace_id, get_logger, trace_span
from title_mcp.security import AuthenticatedPrincipal, require_human_reviewer
from title_mcp.state.base import WorkflowConflictError, WorkflowRepository


class BackgroundTaskManager:
    def __init__(self, concurrency: int = 4) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        self._failures = 0

    def enqueue(self, work: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.create_task(self._run(work))
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._failures += 1
            get_logger(__name__).error("workflow.background_failed")

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        if self._failures:
            self._failures = 0
            raise RuntimeError("Background workflow work failed; inspect persisted state.")

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, work: Callable[[], Awaitable[None]]) -> None:
        async with self._semaphore:
            await work()


class WorkflowEngine:
    """Coordinates workflow state while leaving legal and business decisions to reviewers."""

    def __init__(
        self,
        *,
        repository: WorkflowRepository,
        adapters: AdapterRegistry,
        concurrency: int = 4,
        auto_start: bool = True,
    ) -> None:
        self._repository = repository
        self._adapters = adapters
        self._background = BackgroundTaskManager(concurrency)
        self._auto_start = auto_start
        self._logger = get_logger(__name__)
        self._enqueued: set[str] = set()
        self._recovery_task: asyncio.Task[None] | None = None
        self._recovery_cursor: str | None = None

    @property
    def recovery_running(self) -> bool:
        return self._recovery_task is not None and not self._recovery_task.done()

    async def create(self, request: WorkflowRequest) -> WorkflowRecord:
        with trace_span(
            "workflow.create",
            kind=request.kind.value,
            file_number=request.order.file_number,
        ) as trace_id:
            adapter = self._adapters.resolve(request.jurisdiction, request.kind)
            plan = await adapter.plan(request)
            record = WorkflowRecord(
                kind=request.kind,
                order=request.order,
                jurisdiction=request.jurisdiction,
                payload=request.payload,
                adapter_id=plan.adapter_id,
                tasks=[
                    WorkflowTask(
                        name=step.label,
                        action_type=step.action_type,
                        result={"planned_action": step.model_dump(mode="json")},
                    )
                    for step in plan.steps
                ],
                result={
                    "external_requirements": plan.external_requirements,
                    "warnings": plan.warnings,
                    "routing_hints": plan.routing_hints,
                },
            )

            record.review = HumanReviewRequest(
                risk_level=plan.risk_level,
                prompt=(
                    f"Review {request.kind.value} workflow for order "
                    f"{request.order.file_number} in {request.jurisdiction.key} "
                    "before execution."
                ),
            )
            record.status = WorkflowStatus.NEEDS_HUMAN_REVIEW

            record.add_audit(
                "workflow.created",
                actor=request.requested_by,
                payload={"adapter_id": plan.adapter_id, "steps": len(plan.steps)},
                trace_id=trace_id,
            )
            record = await self._repository.save(record)

            if self._auto_start and record.status != WorkflowStatus.NEEDS_HUMAN_REVIEW:
                self.enqueue(record.id)
            return record

    def enqueue(self, workflow_id: str) -> None:
        if workflow_id not in self._enqueued:
            self._enqueued.add(workflow_id)
            self._background.enqueue(lambda: self._run_background(workflow_id))

    async def _run_background(self, workflow_id: str) -> None:
        try:
            await self.run_once(workflow_id)
        except WorkflowConflictError:
            # Another writer is making progress. Leave queued work for a later sweep.
            self._logger.warning("workflow.busy", extra={"workflow_id": workflow_id})
        except Exception:
            self._logger.error("workflow.execution_failed", extra={"workflow_id": workflow_id})
            # A failed stale worker must never overwrite a human decision.
            for _ in range(4):
                record = await self._repository.get(workflow_id)
                if record is None or record.status not in {
                    WorkflowStatus.QUEUED,
                    WorkflowStatus.RUNNING,
                }:
                    return
                record.status = WorkflowStatus.FAILED
                record.add_audit("workflow.execution_failed", payload={"recoverable": True})
                record.result["error"] = "Workflow planning failed. Staff review is required."
                try:
                    await self._repository.save(record)
                    break
                except WorkflowConflictError:
                    continue
            raise RuntimeError("Workflow planning failed.") from None
        finally:
            self._enqueued.discard(workflow_id)

    async def recover_page(self, *, limit: int = 50) -> int:
        records = await self._repository.list_recoverable(
            after_id=self._recovery_cursor, limit=limit
        )
        self._recovery_cursor = records[-1].id if len(records) == limit else None
        for record in records:
            self.enqueue(record.id)
        return len(records)

    async def start_recovery(self) -> None:
        if self._recovery_task is not None:
            return
        await self.recover_page()
        self._recovery_task = asyncio.create_task(self._recover_periodically())

    async def _recover_periodically(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.recover_page()
            except Exception:
                self._logger.error("workflow.recovery_failed")

    async def run_once(self, workflow_id: str) -> WorkflowRecord | None:
        for _ in range(4):
            try:
                return await self._transition_once(workflow_id)
            except WorkflowConflictError:
                # Reload and re-check review state rather than replaying a stale plan.
                continue
        raise WorkflowConflictError("Workflow remained busy; retry after reloading.")

    async def _transition_once(self, workflow_id: str) -> WorkflowRecord | None:
        with trace_span("workflow.run_once", workflow_id=workflow_id) as trace_id:
            record = await self._repository.get(workflow_id)
            if record is None:
                self._logger.warning("workflow.not_found", extra={"workflow_id": workflow_id})
                return None

            if record.status in {
                WorkflowStatus.COMPLETED,
                WorkflowStatus.FAILED,
                WorkflowStatus.CANCELLED,
                WorkflowStatus.WAITING_ON_EXTERNAL,
            }:
                return record

            gate_changed = False
            if (
                record.review is None
                or record.review.status is ReviewStatus.NOT_REQUIRED
                or (
                    record.review.status is ReviewStatus.APPROVED
                    and (
                        not record.review.authenticated_reviewer
                        or record.review.authenticated_reviewer != record.review.reviewer
                    )
                )
            ):
                record.review = HumanReviewRequest(
                    prompt="An authenticated human must review this workflow before execution."
                )
                gate_changed = True
            if record.review.status == ReviewStatus.PENDING:
                if record.status is WorkflowStatus.NEEDS_HUMAN_REVIEW and not gate_changed:
                    return record
                record.status = WorkflowStatus.NEEDS_HUMAN_REVIEW
                record.add_audit(
                    "workflow.paused_for_review",
                    payload={"review_id": record.review.id},
                    trace_id=trace_id,
                )
                return await self._repository.save(record)

            if record.review and record.review.status in {
                ReviewStatus.REJECTED,
                ReviewStatus.CHANGES_REQUESTED,
            }:
                record.status = WorkflowStatus.CANCELLED
                record.add_audit(
                    "workflow.cancelled_by_review",
                    payload={"review_status": record.review.status.value},
                    trace_id=trace_id,
                )
                return await self._repository.save(record)

            for task in record.tasks:
                task.normalize_legacy_placeholder()
                if task.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                    continue
                task.status = TaskStatus.WAITING_ON_EXTERNAL
                task.result = {
                    **(task.result or {}),
                    "execution_mode": "planned",
                    "message": (
                        "No executor is configured. This action awaits staff or external work."
                    ),
                }
                record.add_audit(
                    "workflow.task_waiting_on_external",
                    payload={"task_id": task.id, "task_name": task.name},
                    trace_id=trace_id,
                )

            record.status = WorkflowStatus.WAITING_ON_EXTERNAL
            record.result["next_state"] = (
                "Await external vendor, public-record, or staff response before completion."
            )
            record.add_audit("workflow.waiting_on_external", trace_id=trace_id)
            return await self._repository.save(record)

    async def submit_review(
        self,
        workflow_id: str,
        *,
        decision: ReviewDecision,
        reviewer: str,
        notes: str | None = None,
        actor: AuditActor | None = None,
        principal: AuthenticatedPrincipal | None = None,
    ) -> WorkflowRecord:
        authenticated_actor = require_human_reviewer(principal)
        if reviewer != authenticated_actor.actor_id:
            raise PermissionError("Reviewer must match the authenticated human identity.")
        if actor is not None and actor != authenticated_actor:
            raise PermissionError("A review cannot override its authenticated audit identity.")
        with trace_span("workflow.submit_review", workflow_id=workflow_id) as trace_id:
            record = await self._repository.get(workflow_id)
            if record is None:
                raise LookupError(f"Workflow not found: {workflow_id}")
            if record.review is None:
                record.review = HumanReviewRequest(
                    prompt="An authenticated human must review this workflow before execution.",
                )

            if decision == ReviewDecision.APPROVE:
                record.review.status = ReviewStatus.APPROVED
                record.status = WorkflowStatus.QUEUED
            elif decision == ReviewDecision.REJECT:
                record.review.status = ReviewStatus.REJECTED
                record.status = WorkflowStatus.CANCELLED
            else:
                record.review.status = ReviewStatus.CHANGES_REQUESTED
                record.status = WorkflowStatus.NEEDS_HUMAN_REVIEW

            record.review.reviewer = reviewer
            record.review.authenticated_reviewer = authenticated_actor.actor_id
            record.review.decision_notes = notes
            record.review.resolved_at = utc_now()
            record.add_audit(
                "workflow.review_submitted",
                actor=authenticated_actor,
                payload={"decision": decision.value, "notes": notes},
                trace_id=trace_id or current_trace_id(),
            )
            saved = await self._repository.save(record)

            if decision == ReviewDecision.APPROVE and self._auto_start:
                self.enqueue(record.id)
            return saved

    async def drain(self) -> None:
        await self._background.drain()

    async def shutdown(self) -> None:
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            await asyncio.gather(self._recovery_task, return_exceptions=True)
            self._recovery_task = None
        await self._background.shutdown()
        # Tasks cancelled while waiting for the semaphore never enter the wrapper's finally.
        self._enqueued.clear()
        self._recovery_cursor = None
