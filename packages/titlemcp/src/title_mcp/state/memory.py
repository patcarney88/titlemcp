from __future__ import annotations

import asyncio

from title_mcp.domain.models import WorkflowKind, WorkflowRecord, WorkflowStatus
from title_mcp.state.base import WorkflowConflictError, WorkflowRepository


class InMemoryWorkflowRepository(WorkflowRepository):
    """Async repository used for local development, tests, and ephemeral deployments."""

    def __init__(self) -> None:
        self._records: dict[str, WorkflowRecord] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def save(self, record: WorkflowRecord) -> WorkflowRecord:
        async with self._lock:
            current = self._records.get(record.id)
            if (current is None and record.revision != 0) or (
                current is not None and current.revision != record.revision
            ):
                raise WorkflowConflictError("Workflow changed; reload before saving.")
            stored = WorkflowRecord.model_validate(record.model_dump())
            stored.revision += 1
            self._records[stored.id] = stored
            return stored.model_copy(deep=True)

    async def get(self, workflow_id: str) -> WorkflowRecord | None:
        async with self._lock:
            record = self._records.get(workflow_id)
            return record.model_copy(deep=True) if record else None

    async def list_recoverable(
        self, *, after_id: str | None = None, limit: int = 50
    ) -> list[WorkflowRecord]:
        async with self._lock:
            records = sorted(
                (
                    r
                    for r in self._records.values()
                    if r.status in {WorkflowStatus.QUEUED, WorkflowStatus.RUNNING}
                    and (after_id is None or r.id > after_id)
                ),
                key=lambda r: r.id,
            )
            return [r.model_copy(deep=True) for r in records[:limit]]

    async def list(
        self,
        *,
        file_number: str | None = None,
        status: WorkflowStatus | None = None,
        kind: WorkflowKind | None = None,
        limit: int = 50,
    ) -> list[WorkflowRecord]:
        async with self._lock:
            records = list(self._records.values())

        if file_number:
            records = [record for record in records if record.order.file_number == file_number]
        if status:
            records = [record for record in records if record.status == status]
        if kind:
            records = [record for record in records if record.kind == kind]

        records.sort(key=lambda record: record.updated_at, reverse=True)
        return [record.model_copy(deep=True) for record in records[:limit]]
