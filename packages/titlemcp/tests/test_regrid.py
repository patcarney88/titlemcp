from __future__ import annotations

import asyncio
import io
import logging
import secrets
import threading
import traceback
import unittest
from typing import Any
from unittest.mock import Mock, patch

import requests
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.response import HTTPResponse

from title_mcp.domain.models import Jurisdiction
from title_mcp.observability.logging import configure_logging
from title_mcp.settings import TitleMCPSettings
from title_mcp.sources import SourceKind, SourceQuery, SourceResultStatus
from title_mcp.sources.regrid import (
    RegridParcelLookupRecord,
    RegridParcelQueryService,
    RegridParcelSourceConnector,
    _redacted_proxy_map,
    _search_query_variants,
    generate_regrid_headers,
)


class RegridTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_factory_runs_off_loop_and_invalid_query_skips_it(self):
        entered, release = threading.Event(), threading.Event()

        def factory():
            entered.set()
            if not release.wait(2):
                raise AssertionError("Test did not release blocking factory")
            return _FakeRegridService()

        connector = RegridParcelSourceConnector(settings=TitleMCPSettings())
        query = SourceQuery(
            jurisdiction=Jurisdiction(country="US"),
            kind=SourceKind.VENDOR_API,
            criteria={"address": "100 Example Ave"},
        )
        with patch.object(connector, "_service_from_settings", side_effect=factory) as mocked:
            invalid = await connector.query(query.model_copy(update={"criteria": {}}))
            self.assertEqual(invalid.status, SourceResultStatus.FAILED)
            mocked.assert_not_called()
            pending = asyncio.create_task(connector.query(query))
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(
                    entered.is_set(), "Event loop must run during service initialization"
                )
                busy = await connector.query(query)
                self.assertEqual(busy.status, SourceResultStatus.FAILED)
                self.assertIn("finishing", busy.warnings[0])
            finally:
                release.set()
                self.assertEqual((await pending).status, SourceResultStatus.SUCCEEDED)

    async def test_response_deadline_does_not_start_overlapping_threads(self):
        release = threading.Event()
        calls = []

        class BlockingService:
            def lookup(self, address):
                calls.append(address)
                release.wait(2)
                return _FakeRegridService().lookup(address)

        settings = TitleMCPSettings()
        settings.regrid_query_timeout_seconds = 0.01  # fake-only deadline; production min is 1s
        connector = RegridParcelSourceConnector(settings=settings, service=BlockingService())
        query = SourceQuery(
            jurisdiction=Jurisdiction(country="US"),
            kind=SourceKind.VENDOR_API,
            criteria={"address": "100 Example Ave"},
        )
        try:
            result = await connector.query(query)
            self.assertEqual(result.status, SourceResultStatus.FAILED)
            self.assertIn("deadline", result.warnings[0])
            self.assertEqual((await connector.query(query)).status, SourceResultStatus.FAILED)
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            await connector._pending_lookup

    def test_generated_headers_look_browser_like(self) -> None:
        headers = generate_regrid_headers()

        self.assertIn("User-Agent", headers)
        self.assertIn("sec-ch-ua", headers)
        self.assertEqual(headers["upgrade-insecure-requests"], "1")

    def test_service_uses_smart_proxy_pool_and_cookie(self) -> None:
        service = RegridParcelQueryService(
            smart_proxy="user:pass@proxy.example",
            proxy_port_start=10001,
            proxy_port_end=10003,
            max_proxy_attempts=2,
            session=_FakeRegridSession(),
        )

        self.assertEqual(len(service.proxies), 3)
        self.assertEqual(service.max_proxy_attempts, 2)
        self.assertEqual(service.cookies["_session_id"], "cookie-123")
        self.assertIn(":10001", service.proxies[0]["http"])
        self.assertFalse(service.session.trust_env)

    def test_redacted_proxy_map_hides_credentials_and_ignores_env_proxy_keys(self) -> None:
        redacted = _redacted_proxy_map(
            {
                "http": "http://user:pass@proxy.example:10001",
                "https": "https://user:pass@proxy.example:10001",
                "title_mcp_smart": "user:pass@proxy.example",
            }
        )

        self.assertEqual(
            redacted,
            {
                "http": "http://***:***@proxy.example:10001",
                "https": "https://***:***@proxy.example:10001",
            },
        )

    def test_search_query_variants_try_comma_free_address_first(self) -> None:
        variants = _search_query_variants("100 Example Ave, Columbus, OH")

        self.assertEqual(
            variants,
            [
                "100 Example Ave Columbus OH",
                "100 Example Ave",
                "100 Example Ave, Columbus, OH",
            ],
        )

    def test_lookup_uses_comma_free_search_variant_first(self) -> None:
        session = _CommaSensitiveRegridSession()
        service = RegridParcelQueryService(
            smart_proxy="user:pass@proxy.example",
            proxy_port_start=10001,
            proxy_port_end=10001,
            session=session,
        )

        record = service.lookup("100 Example Ave, Columbus, OH")

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.source["search_query_used"], "100 Example Ave Columbus OH")
        self.assertEqual(session.search_queries, ["100 Example Ave Columbus OH"])

    def test_service_lookup_merges_search_result_with_detail(self) -> None:
        service = RegridParcelQueryService(
            smart_proxy="user:pass@proxy.example",
            proxy_port_start=10001,
            proxy_port_end=10001,
            session=_FakeRegridSession(),
        )

        record = service.lookup("100 Example Ave")

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.path, "/us/oh/franklin/example")
        self.assertEqual(record.fields["parcelnumb"], "01000012300")
        self.assertEqual(record.geometry["type"], "Polygon")

    async def test_source_requires_smart_proxy_configuration(self) -> None:
        connector = RegridParcelSourceConnector(
            settings=TitleMCPSettings(
                smart_proxy=None,
                load_entry_point_adapters=False,
                load_entry_point_capabilities=False,
                load_entry_point_sources=False,
                load_entry_point_vendors=False,
                load_entry_point_toolsets=False,
            )
        )

        result = await connector.query(
            SourceQuery(
                jurisdiction=Jurisdiction(country="US"),
                kind=SourceKind.VENDOR_API,
                criteria={"address": "100 Example Ave"},
            )
        )

        self.assertEqual(result.status, SourceResultStatus.REQUIRES_CONFIGURATION)
        self.assertIn("TITLE_MCP_SMART_PROXY", result.warnings[0])

    async def test_source_returns_structured_record_with_fake_service(self) -> None:
        connector = RegridParcelSourceConnector(service=_FakeRegridService())

        result = await connector.query(
            SourceQuery(
                jurisdiction=Jurisdiction(country="US"),
                kind=SourceKind.VENDOR_API,
                criteria={"address": "100 Example Ave"},
            )
        )

        self.assertEqual(result.status, SourceResultStatus.SUCCEEDED)
        self.assertEqual(result.records[0]["schema_name"], "title_mcp.parcel_record")
        self.assertEqual(result.records[0]["record_type"], "parcel")
        self.assertEqual(result.records[0]["identifiers"]["parcel_number"], "01000012300")
        self.assertEqual(result.records[0]["site"]["address_display"], "100 EXAMPLE AVE")
        self.assertEqual(result.records[0]["jurisdiction"]["county"], "Franklin")
        self.assertEqual(result.records[0]["land_use"]["use_code"], "510")
        self.assertEqual(
            result.records[0]["source_specific"]["regrid"]["fields"]["parcelnumb"],
            "01000012300",
        )
        self.assertTrue(result.metadata["smart_proxy_enabled"])


class RegridPrivacyTests(unittest.TestCase):
    def test_application_sink_blocks_real_transport_url_diagnostics(self):
        marker = secrets.token_hex(12)
        stream = io.StringIO()
        connection = Mock()
        connection.is_closed = False
        connection.proxy = None
        connection.getresponse.return_value = HTTPResponse(
            body=io.BytesIO(b"{}"),
            headers={"Content-Length": "2"},
            status=200,
            version=11,
            preload_content=False,
        )
        root = logging.getLogger()
        with patch.object(root, "handlers", []), patch.object(root, "level", root.level):
            with patch("sys.stderr", stream):
                configure_logging("DEBUG")
            with requests.Session() as session:
                session.trust_env = False
                with patch.object(HTTPConnectionPool, "_get_conn", return_value=connection):
                    response = session.get(
                        "http://parcel.example/search.json", params={"query": marker}, timeout=1
                    )
                self.assertEqual(response.json(), {})
            for logger in ("requests", "requests.adapters", "urllib3.connectionpool"):
                try:
                    raise ValueError(marker)
                except ValueError:
                    logging.getLogger(logger).exception(marker)
            logging.getLogger("title_mcp.sources.regrid").info("safe-status-marker")
        self.assertIn(marker, connection.request.call_args.args[1])
        self.assertNotIn(marker, stream.getvalue())
        self.assertIn("safe-status-marker", stream.getvalue())

    def test_retry_refresh_and_query_fallback_preserve_legitimate_requests(self):
        marker = secrets.token_hex(12)
        for statuses in ([429, 500], [503]):
            session = _RetryRegridSession(statuses, marker)
            with (
                self.subTest(statuses=statuses),
                self.assertLogs("title_mcp.sources.regrid", level="DEBUG") as captured,
            ):
                service = RegridParcelQueryService(
                    smart_proxy=f"user:{marker}@proxy.example",
                    proxy_port_start=10001,
                    proxy_port_end=10003,
                    max_proxy_attempts=3,
                    session=session,
                )
                record = service.lookup("100 Example Ave, Columbus, OH")
            self.assertIsNotNone(record)
            self.assertNotIn(marker, "\n".join(captured.output))
            self.assertTrue(all(marker in proxy["http"] for proxy in session.sent_proxies))
            self.assertFalse(session.trust_env)
            if statuses[0] == 429:
                self.assertEqual(session.head_count, 2)
            else:
                self.assertEqual(record.source["search_query_used"], "100 Example Ave")

    def test_debug_logs_never_include_proxy_userinfo_or_order_data(self):
        marker = secrets.token_hex(12)
        for userinfo in (f"user:{marker}", marker, f":{marker}", f"{marker}:"):
            with (
                self.subTest(userinfo=userinfo),
                self.assertLogs("title_mcp.sources.regrid", level="DEBUG") as captured,
            ):
                service = RegridParcelQueryService(
                    smart_proxy=f"{userinfo}@proxy.example",
                    proxy_port_start=10001,
                    proxy_port_end=10001,
                    session=_FakeRegridSession(),
                )
                record = service.lookup("100 Private Review Address")
            self.assertIsNotNone(record)
            logs = "\n".join(captured.output)
            self.assertNotIn(marker, logs)
            self.assertNotIn("Private Review Address", logs)
            self.assertNotIn("/us/oh/franklin/example", logs)

    def test_error_logs_and_exception_tracebacks_do_not_echo_upstream_data(self):
        marker = secrets.token_hex(12)
        for mode in ("cookie", "proxy", "timeout", "request", "http", "server", "unavailable"):
            with (
                self.subTest(mode=mode),
                self.assertLogs("title_mcp.sources.regrid", level="DEBUG") as captured,
            ):
                try:
                    service = RegridParcelQueryService(
                        smart_proxy=f"user:{marker}@proxy.example",
                        proxy_port_start=10001,
                        proxy_port_end=10001,
                        max_proxy_attempts=1,
                        session=_SensitiveFailureSession(mode, marker),
                    )
                    service.lookup("100 Private Review Address")
                except Exception as error:
                    rendered = "".join(traceback.format_exception(error))
                else:
                    self.fail("Synthetic upstream failure must not become a successful lookup")
            self.assertNotIn(marker, "\n".join(captured.output))
            self.assertNotIn(marker, rendered)
            self.assertNotIn("Private Review Address", "\n".join(captured.output))


class RegridSelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_candidates_do_not_escape_in_service_tracebacks(self):
        marker = secrets.token_hex(12)
        service = RegridParcelQueryService(
            smart_proxy="proxy.example",
            proxy_port_start=10001,
            proxy_port_end=10001,
            session=_AmbiguousRegridSession(
                candidates=[{"label": {"private": marker}}, {"label": "second"}]
            ),
        )
        try:
            service.lookup("100 Private Review Address")
        except Exception as error:
            rendered = "".join(traceback.format_exception(error))
        else:
            self.fail("Invalid upstream candidates must fail safely")
        self.assertNotIn(marker, rendered)
        self.assertIn("Parcel response was invalid", rendered)

    async def test_multiple_candidates_are_partial_without_fetching_a_selected_parcel(self):
        session = _AmbiguousRegridSession()
        service = RegridParcelQueryService(
            smart_proxy="proxy.example",
            proxy_port_start=10001,
            proxy_port_end=10001,
            session=session,
        )
        connector = RegridParcelSourceConnector(service=service)
        result = await connector.query(
            SourceQuery(
                jurisdiction=Jurisdiction(country="US"),
                kind=SourceKind.VENDOR_API,
                criteria={"address": "200 Intended Avenue"},
            )
        )
        self.assertEqual(result.status, SourceResultStatus.PARTIAL)
        self.assertEqual(result.records, [])
        self.assertTrue(result.requires_human_review)
        self.assertEqual(result.metadata["result_count"], 2)
        self.assertEqual(
            [candidate["label"] for candidate in result.metadata["candidates"]],
            ["100 Other Lane", "200 Intended Avenue"],
        )
        self.assertIn("Narrow", result.warnings[0])
        self.assertEqual(session.detail_requests, [])

    async def test_empty_search_stays_no_results(self):
        session = _AmbiguousRegridSession(candidates=[])
        service = RegridParcelQueryService(
            smart_proxy="proxy.example",
            proxy_port_start=10001,
            proxy_port_end=10001,
            session=session,
        )
        connector = RegridParcelSourceConnector(service=service)
        result = await connector.query(
            SourceQuery(
                jurisdiction=Jurisdiction(country="US"),
                kind=SourceKind.VENDOR_API,
                criteria={"address": "200 Intended Avenue"},
            )
        )
        self.assertEqual(result.status, SourceResultStatus.NO_RESULTS)
        self.assertEqual(session.detail_requests, [])


class _FakeRegridResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text: str | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text if text is not None else ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class _FakeRegridSession:
    trust_env = True

    def head(self, *args: Any, **kwargs: Any) -> _FakeRegridResponse:
        return _FakeRegridResponse({}, headers={"Set-Cookie": "_session_id=cookie-123; path=/"})

    def get(self, url: str, *args: Any, **kwargs: Any) -> _FakeRegridResponse:
        if url.endswith("search.json"):
            return _FakeRegridResponse(
                [
                    {
                        "label": "100 Example Ave",
                        "path": "/us/oh/franklin/example",
                    }
                ]
            )
        return _FakeRegridResponse(
            {
                "fields": {
                    "parcelnumb": "01000012300",
                    "address": "100 EXAMPLE AVE",
                    "county": "Franklin County",
                    "state2": "OH",
                    "usecode": "510",
                },
                "geometry": {"type": "Polygon", "coordinates": []},
            }
        )


class _CommaSensitiveRegridSession(_FakeRegridSession):
    def __init__(self) -> None:
        self.search_queries: list[str] = []

    def get(self, url: str, *args: Any, **kwargs: Any) -> _FakeRegridResponse:
        params = kwargs.get("params") or {}
        query = params.get("query")
        if url.endswith("search.json"):
            self.search_queries.append(query)
            if "," in query:
                return _FakeRegridResponse(
                    {},
                    status_code=503,
                    text='{ "status": "error", "code": "503", "message": "Service Unavailable" }',
                )
        return super().get(url, *args, **kwargs)


class _RetryRegridSession(_FakeRegridSession):
    def __init__(self, statuses, marker):
        self.statuses = list(statuses)
        self.marker = marker
        self.head_count = 0
        self.sent_proxies = []

    def head(self, *args, **kwargs):
        self.head_count += 1
        self.sent_proxies.append(kwargs["proxies"])
        return super().head(*args, **kwargs)

    def get(self, url, *args, **kwargs):
        self.sent_proxies.append(kwargs["proxies"])
        if self.statuses:
            return _FakeRegridResponse(
                {}, status_code=self.statuses.pop(0), text="Service Unavailable " + self.marker
            )
        return super().get(url, *args, **kwargs)


class _SensitiveFailureSession(_FakeRegridSession):
    def __init__(self, mode, marker):
        self.mode = mode
        self.marker = marker

    def head(self, *args, **kwargs):
        if self.mode == "cookie":
            raise requests.ConnectionError(self.marker)
        return super().head(*args, **kwargs)

    def get(self, *args, **kwargs):
        failures = {
            "proxy": requests.exceptions.ProxyError,
            "timeout": requests.Timeout,
            "request": requests.ConnectionError,
        }
        if self.mode in failures:
            raise failures[self.mode](self.marker)
        if self.mode == "http":
            response = _FakeRegridResponse({}, status_code=400)
            response.raise_for_status = lambda: self._http_error()
            return response
        status = 503 if self.mode == "unavailable" else 500
        return _FakeRegridResponse(
            {}, status_code=status, text="Service Unavailable " + self.marker
        )

    def _http_error(self):
        raise requests.HTTPError(self.marker)


class _AmbiguousRegridSession(_FakeRegridSession):
    def __init__(self, candidates=None):
        self.candidates = (
            [
                {"label": "100 Other Lane", "path": "/us/oh/franklin/other"},
                {"label": "200 Intended Avenue", "path": "/us/oh/franklin/intended"},
            ]
            if candidates is None
            else candidates
        )
        self.detail_requests = []

    def get(self, url, *args, **kwargs):
        if url.endswith("search.json"):
            return _FakeRegridResponse(self.candidates)
        self.detail_requests.append(url)
        return super().get(url, *args, **kwargs)


class _FakeRegridService:
    def lookup(self, address: str) -> RegridParcelLookupRecord:
        return RegridParcelLookupRecord(
            source={
                "source_id": RegridParcelSourceConnector.source_id,
                "source_name": "Regrid Parcel Search",
                "source_url": "https://app.regrid.com/example.json",
                "retrieved_at": "2026-05-14T18:00:00+00:00",
                "smart_proxy_enabled": True,
            },
            query={"address": address},
            label="100 Example Ave",
            path="/us/oh/franklin/example",
            fields={
                "parcelnumb": "01000012300",
                "parcelnumb_no_formatting": "01000012300",
                "address": "100 EXAMPLE AVE",
                "scity": "GRANDVIEW HEIGHTS",
                "county": "Franklin County",
                "state2": "OH",
                "szip": "43212",
                "usecode": "510",
                "usedesc": "SINGLE FAMILY DWELLING, PLATTED LOT",
                "landval": 290900.0,
                "improvval": 427600.0,
                "parval": 718500.0,
                "yearbuilt": 1903,
                "area_building": 2843,
                "lat": "39.979984",
                "lon": "-83.053998",
            },
            geometry={"type": "Polygon", "coordinates": []},
            raw={"path": "/us/oh/franklin/example"},
        )


if __name__ == "__main__":
    unittest.main()
