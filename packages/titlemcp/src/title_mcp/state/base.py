from __future__ import annotations

from typing import Protocol

from title_mcp.domain.models import WorkflowKind, WorkflowRecord, WorkflowStatus


class WorkflowConflictError(RuntimeError):
    """The workflow changed since it was read; reload before making a new decision."""


class WorkflowRepository(Protocol):
    async def initialize(self) -> None:
        """Prepare the repository for use."""

    async def check_health(self, *, timeout_seconds: float = 2.0) -> None:
        """Probe current storage availability without creating or changing state."""
        raise NotImplementedError("Repository health checks are not implemented.")

    async def save(self, record: WorkflowRecord) -> WorkflowRecord:
        """Atomically save only the expected revision, returning its incremented copy.

        Revision zero creates a record (or updates a historical revision-zero record).
        Raise WorkflowConflictError on a stale revision or a deleted existing record.
        """

    async def get(self, workflow_id: str) -> WorkflowRecord | None:
        """Return a workflow by id."""

    async def list_recoverable(
        self, *, after_id: str | None = None, limit: int = 50
    ) -> list[WorkflowRecord]:
        """Page queued/running work by stable id, for bounded restart recovery."""

    async def list(
        self,
        *,
        file_number: str | None = None,
        status: WorkflowStatus | None = None,
        kind: WorkflowKind | None = None,
        limit: int = 50,
    ) -> list[WorkflowRecord]:
        """Return recent workflows matching optional filters."""
