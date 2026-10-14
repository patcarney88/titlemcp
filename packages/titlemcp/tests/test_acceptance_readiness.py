from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx2
from test_instance_security import HTTP_CREDENTIAL


def load_checks():
    directory = Path(__file__).resolve().parents[3] / "scripts/acceptance"
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location(
        "acceptance_readiness_checks", directory / "readiness_checks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checks = load_checks()


class AcceptanceReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        run_id = "a" * 12
        self.environment = patch.dict(
            os.environ,
            {
                "PORT": "18000",
                "TITLEMCP_ACCEPTANCE_RUN": run_id,
                "TITLE_MCP_POSTGRES_DSN": (
                    f"postgresql://acceptance_user@titlemcp-accept-{run_id}-db:5432/"
                    f"titlemcp_acceptance_{run_id}"
                ),
                "TITLE_MCP_MCP_ACCESS_TOKEN": HTTP_CREDENTIAL,
                "TITLE_MCP_READINESS_TIMEOUT_SECONDS": "0.1",
                "TITLE_MCP_ENVIRONMENT": "test",
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.ready_status = 503
        self.health_status = 200
        self.unauthorized_status = 401
        self.extra_payload = {}
        self.statuses = []
        self.requests = []

    def handle(self, request):
        authorized = request.headers.get("Authorization") == "Bearer " + HTTP_CREDENTIAL
        self.requests.append((request.url.path, authorized))
        if request.url.path == "/healthz":
            return httpx2.Response(self.health_status, json={"status": "ok"})
        if not authorized:
            return httpx2.Response(
                self.unauthorized_status, json={"error": "Authentication required."}
            )
        status = self.statuses.pop(0) if self.statuses else self.ready_status
        payload = {"name": "Title Operations MCP", "environment": "test"}
        payload.update(
            {"status": "ready"}
            if status == 200
            else {"status": "error", "error": "Service readiness check failed."}
        )
        return httpx2.Response(status, json={**payload, **self.extra_payload})

    async def run_check(self, expected=503, workflow_id=None):
        return await checks.check_readiness(
            expected, workflow_id, transport=httpx2.MockTransport(self.handle)
        )

    async def test_outage_requires_stable_sanitized_503_liveness_and_authorization(self):
        result = await self.run_check()
        self.assertEqual(result["readiness_status"], 503)
        self.assertEqual(result["stable_probes"], 3)
        self.assertEqual(result["total_probe_attempts"], 3)
        self.assertTrue(result["sanitized_response"])
        self.assertEqual(
            sum(path == "/readyz" and authorized for path, authorized in self.requests), 3
        )
        self.assertIn(("/healthz", False), self.requests)

    async def test_healthy_state_verifies_existing_workflow(self):
        self.ready_status = 200
        with patch.object(checks, "pending_workflow", new_callable=AsyncMock) as pending:
            result = await self.run_check(200, "synthetic-workflow")
            self.assertTrue(result["workflow_preserved"])
            pending.assert_awaited_once()
            self.assertEqual(pending.call_args.args[2], "synthetic-workflow")

    async def test_restoration_can_recover_from_transient_sanitized_503(self):
        self.statuses = [503, 200, 200, 200]
        result = await self.run_check(200)
        self.assertEqual(result["total_probe_attempts"], 4)

    async def test_false_green_during_outage_fails_immediately(self):
        self.ready_status = 200
        with self.assertRaisesRegex(AssertionError, "incorrectly reported as ready"):
            await self.run_check()

    async def test_upstream_details_cannot_be_accepted_as_a_sanitized_failure(self):
        self.extra_payload = {"internal_error": "synthetic-private-detail"}
        with self.assertRaisesRegex(AssertionError, "not sanitized"):
            await self.run_check()

    async def test_public_readiness_or_failed_liveness_cannot_pass(self):
        for failed_check in ("authorization", "liveness"):
            with self.subTest(failed_check=failed_check):
                self.unauthorized_status = 200 if failed_check == "authorization" else 401
                self.health_status = 503 if failed_check == "liveness" else 200
                with self.assertRaises(AssertionError):
                    await self.run_check()

    async def test_stalled_response_obeys_total_response_deadline(self):
        async def stalled(request):
            await checks.asyncio.Event().wait()

        with patch.object(checks, "LATENCY_MARGIN_SECONDS", 0.01):
            with self.assertRaises(TimeoutError):
                await checks.check_readiness(503, transport=httpx2.MockTransport(stalled))

    async def test_unrecovered_database_has_a_bounded_wait(self):
        with patch.object(checks, "RECOVERY_WAIT_SECONDS", 0.05):
            with self.assertRaises(TimeoutError):
                await self.run_check(200)

    async def test_non_disposable_identity_is_rejected_before_http(self):
        os.environ["TITLEMCP_ACCEPTANCE_RUN"] = "not-a-disposable-run"
        with self.assertRaises(RuntimeError):
            await self.run_check()
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
