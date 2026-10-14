from __future__ import annotations

import asyncio
import unittest

from pydantic import ValidationError
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from title_mcp.adapters import create_default_adapter_registry
from title_mcp.domain.models import (
    AuditActor,
    Jurisdiction,
    OrderRef,
    ReviewDecision,
    ReviewStatus,
    TaskStatus,
    WorkflowKind,
    WorkflowRecord,
    WorkflowRequest,
    WorkflowStatus,
    WorkflowTask,
    utc_now,
)
from title_mcp.mcp.server import create_mcp_server
from title_mcp.security import AuthenticatedPrincipal, PrincipalKind, ReviewPermission
from title_mcp.settings import TitleMCPSettings
from title_mcp.state.memory import InMemoryWorkflowRepository
from title_mcp.workflows.engine import WorkflowEngine

HTTP_CREDENTIAL = "synthetic-local-instance-credential-0001"


def settings(**overrides: object) -> TitleMCPSettings:
    return TitleMCPSettings(
        _env_file=None,
        load_entry_point_adapters=False,
        load_entry_point_capabilities=False,
        load_entry_point_sources=False,
        load_entry_point_vendors=False,
        load_entry_point_plugins=False,
        load_entry_point_toolsets=False,
        **overrides,
    )


def human(subject: str = "reviewer") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        subject=subject,
        kind=PrincipalKind.HUMAN,
        permissions={ReviewPermission.REVIEW_WORKFLOW},
    )


class InstanceBoundaryTests(unittest.TestCase):
    def test_http_apps_fail_closed_without_a_credential(self) -> None:
        server = create_mcp_server(settings())
        for factory in (server.streamable_http_app, server.sse_app):
            with self.subTest(factory=factory.__name__), TestClient(factory()) as client:
                self.assertEqual(client.get("/healthz").status_code, 200)
                for route in ("/", "/readyz", "/mcp", "/sse", "/messages/"):
                    response = client.get(route)
                    self.assertEqual(response.status_code, 401, route)

    def test_headers_are_required_on_every_http_request(self) -> None:
        server = create_mcp_server(settings(mcp_access_token=HTTP_CREDENTIAL))
        for factory in (server.streamable_http_app, server.sse_app):
            with self.subTest(factory=factory.__name__), TestClient(factory()) as client:
                good = {"Authorization": f"Bearer {HTTP_CREDENTIAL}"}
                self.assertEqual(client.get("/", headers=good).status_code, 200)
                self.assertEqual(client.get("/").status_code, 401)
                self.assertEqual(
                    client.get("/", params={"access_token": HTTP_CREDENTIAL}).status_code, 401
                )
                self.assertEqual(
                    client.get("/", headers={"Authorization": "Bearer wrong"}).status_code, 401
                )
                self.assertEqual(
                    client.post(
                        "/mcp", json={}, headers={"Authorization": "Basic wrong"}
                    ).status_code,
                    401,
                )
                duplicate = [
                    ("Authorization", f"Bearer {HTTP_CREDENTIAL}"),
                    ("Authorization", "Bearer other"),
                ]
                self.assertEqual(client.get("/", headers=duplicate).status_code, 401)

    def test_another_instance_credential_is_not_accepted(self) -> None:
        server = create_mcp_server(settings(mcp_access_token=HTTP_CREDENTIAL))
        with TestClient(server.streamable_http_app()) as client:
            response = client.get("/", headers={"Authorization": "Bearer another-instance"})
        self.assertEqual(response.status_code, 401)

    def test_access_credential_is_not_disclosed_in_settings(self) -> None:
        configured = settings(mcp_access_token=HTTP_CREDENTIAL)
        self.assertNotIn(HTTP_CREDENTIAL, str(configured))
        self.assertNotIn("mcp_access_token", configured.model_dump())

    def test_human_review_is_absent_from_agent_tools(self) -> None:
        server = create_mcp_server(settings())
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertNotIn("submit_human_review", names)

    def test_configuration_cannot_disable_mandatory_review(self) -> None:
        with self.assertRaises(ValidationError):
            settings(default_human_review_required=False)

    def test_blank_credential_denies_access_and_invalid_values_are_hidden(self):
        self.assertIsNone(settings(mcp_access_token="").mcp_access_token)
        invalid = "synthetic malformed credential with whitespace"
        with self.assertRaises(ValidationError) as raised:
            settings(mcp_access_token=invalid)
        self.assertNotIn(invalid, str(raised.exception))

    def test_extension_routes_share_the_boundary_and_credentials_do_not_linger(self):
        server = create_mcp_server(settings(mcp_access_token=HTTP_CREDENTIAL))
        calls = []

        @server.custom_route("/optional-package", methods=["POST"])
        async def extension(request):
            calls.append("called")
            return JSONResponse({"ok": True})

        with TestClient(server.streamable_http_app()) as client:
            self.assertEqual(client.post("/optional-package").status_code, 401)
            self.assertEqual(calls, [])
            response = client.post(
                "/optional-package",
                headers={
                    "Authorization": f"Bearer {HTTP_CREDENTIAL}",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(calls, ["called"])
            self.assertEqual(client.post("/optional-package").status_code, 401)


class HumanReviewBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repository = InMemoryWorkflowRepository()
        self.engine = WorkflowEngine(
            repository=self.repository,
            adapters=create_default_adapter_registry(),
            auto_start=False,
        )
        self.record = await self.engine.create(
            WorkflowRequest(
                kind=WorkflowKind.TAX_CERTIFICATE,
                order=OrderRef(file_number="LOCAL-TEST"),
                jurisdiction=Jurisdiction(state="OH"),
                require_human_review=False,
            )
        )

    async def test_request_cannot_disable_review(self) -> None:
        self.assertEqual(self.record.status, WorkflowStatus.NEEDS_HUMAN_REVIEW)

    async def test_agent_or_missing_principal_cannot_review(self) -> None:
        agent = AuthenticatedPrincipal(
            subject="agent",
            kind=PrincipalKind.AGENT,
            permissions={ReviewPermission.REVIEW_WORKFLOW},
        )
        for principal in (
            None,
            agent,
            AuthenticatedPrincipal(subject="human", kind=PrincipalKind.HUMAN),
        ):
            with self.subTest(principal=principal), self.assertRaises(PermissionError):
                await self.engine.submit_review(
                    self.record.id,
                    decision=ReviewDecision.APPROVE,
                    reviewer="reviewer",
                    principal=principal,
                )
        stored = await self.repository.get(self.record.id)
        self.assertEqual(stored.status, WorkflowStatus.NEEDS_HUMAN_REVIEW)

    async def test_review_identity_cannot_be_spoofed(self) -> None:
        with self.assertRaises(PermissionError):
            await self.engine.submit_review(
                self.record.id,
                decision=ReviewDecision.APPROVE,
                reviewer="someone-else",
                principal=human(),
            )
        with self.assertRaises(PermissionError):
            await self.engine.submit_review(
                self.record.id,
                decision=ReviewDecision.APPROVE,
                reviewer="reviewer",
                principal=human(),
                actor=AuditActor(actor_id="someone-else", actor_type="user"),
            )

    async def test_authenticated_review_retains_identity_and_tasks_stay_unexecuted(self) -> None:
        await self.engine.submit_review(
            self.record.id,
            decision=ReviewDecision.APPROVE,
            reviewer="reviewer",
            principal=human(),
        )
        record = await self.engine.run_once(self.record.id)
        self.assertEqual(record.status, WorkflowStatus.WAITING_ON_EXTERNAL)
        event = next(e for e in record.audit if e.action == "workflow.review_submitted")
        self.assertEqual(event.actor.actor_id, "reviewer")
        self.assertEqual(event.actor.actor_type, "user")
        self.assertTrue(record.tasks)
        for task in record.tasks:
            self.assertEqual(task.status, TaskStatus.WAITING_ON_EXTERNAL)
            self.assertIsNone(task.started_at)
            self.assertIsNone(task.completed_at)
            self.assertEqual(task.result["execution_mode"], "planned")
        self.assertFalse(any(e.action == "workflow.task_succeeded" for e in record.audit))

    async def test_legacy_missing_review_does_not_bypass_execution_gate(self) -> None:
        self.record.review = None
        self.record.status = WorkflowStatus.QUEUED
        await self.repository.save(self.record)
        record = await self.engine.run_once(self.record.id)
        self.assertEqual(record.status, WorkflowStatus.NEEDS_HUMAN_REVIEW)
        self.assertTrue(all(task.status is TaskStatus.QUEUED for task in record.tasks))

    async def test_persisted_placeholder_success_is_reclassified_on_read_and_after_review(self):
        payload = self.record.model_dump(mode="json")
        payload["tasks"][0].update(
            {
                "status": "succeeded",
                "result": {"execution_mode": "review_first_placeholder"},
                "started_at": utc_now().isoformat(),
                "completed_at": utc_now().isoformat(),
            }
        )
        legacy = WorkflowRecord.model_validate(payload)
        await self.repository.save(legacy)
        readback = await self.repository.get(legacy.id)
        self.assertIs(readback.tasks[0].status, TaskStatus.WAITING_ON_EXTERNAL)
        self.assertIsNone(readback.tasks[0].completed_at)
        self.assertIsNotNone(readback.tasks[0].result["legacy_completed_at"])
        listed = await self.repository.list()
        self.assertIs(listed[0].tasks[0].status, TaskStatus.WAITING_ON_EXTERNAL)
        await self.engine.submit_review(
            legacy.id,
            decision=ReviewDecision.APPROVE,
            reviewer="reviewer",
            principal=human(),
        )
        resumed = await self.engine.run_once(legacy.id)
        self.assertIs(resumed.tasks[0].status, TaskStatus.WAITING_ON_EXTERNAL)
        self.assertEqual(resumed.tasks[0].result["execution_mode"], "planned")

    def test_real_executor_success_is_preserved(self):
        completed = utc_now()
        task = WorkflowTask(
            name="real external work",
            status=TaskStatus.SUCCEEDED,
            result={"execution_mode": "provider", "reference": "LOCAL-TEST"},
            completed_at=completed,
        )
        self.assertIs(task.status, TaskStatus.SUCCEEDED)
        self.assertEqual(task.completed_at, completed)

    async def test_legacy_unattributed_approval_or_disabled_review_is_gated(self):
        for status in (ReviewStatus.APPROVED, ReviewStatus.NOT_REQUIRED):
            with self.subTest(status=status):
                self.record.review.status = status
                self.record.review.reviewer = "unverified-label"
                self.record.review.authenticated_reviewer = None
                self.record = await self.repository.save(self.record)
                record = await self.engine.run_once(self.record.id)
                self.assertIs(record.status, WorkflowStatus.NEEDS_HUMAN_REVIEW)
                self.assertTrue(all(task.status is TaskStatus.QUEUED for task in record.tasks))
                self.record = record


if __name__ == "__main__":
    unittest.main()
