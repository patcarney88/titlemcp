from __future__ import annotations

import json
import unittest
from unittest import mock
from urllib.parse import urlencode

import requests

from title_mcp.domain.models import Jurisdiction
from title_mcp.settings import TitleMCPSettings
from title_mcp.sources import SourceKind, SourceQuery, SourceResultStatus
from title_mcp.sources.hoa_serpapi import (
    HoaContactSearchQuery,
    HoaContactSerpApiSourceConnector,
    SerpApiClientError,
    SerpApiHoaContactClient,
    _redact_serpapi_payload,
)

SENTINEL = "synthetic-sensitive-marker-for-offline-tests"


class HoaSecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_http_and_json_errors_never_repeat_request_or_provider_content(self):
        response = mock.Mock(status_code=429)
        response.raise_for_status.side_effect = requests.HTTPError(
            "429 for https://serpapi.com/?" + urlencode({"api_key": SENTINEL}), response=response
        )
        for mode in ("http", "provider", "json"):
            with self.subTest(mode=mode):
                if mode != "http":
                    response.raise_for_status.side_effect = None
                response.json.side_effect = ValueError(SENTINEL) if mode == "json" else None
                response.json.return_value = {"error": SENTINEL}
                session = mock.Mock()
                session.get.return_value = response
                client = SerpApiHoaContactClient(SENTINEL, session=session)
                with self.assertRaises(SerpApiClientError) as raised:
                    client.hoa_contact_search(HoaContactSearchQuery(hoa_name="Example HOA"))
                self.assertNotIn(SENTINEL, str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__ or mode == "provider")

    async def test_injected_client_exception_is_safe_at_connector_boundary(self):
        client = mock.Mock()
        client.hoa_contact_search.side_effect = RuntimeError(SENTINEL)
        connector = HoaContactSerpApiSourceConnector(
            client=client, settings=TitleMCPSettings(_env_file=None, serpapi_api_key=SENTINEL)
        )
        result = await connector.query(
            SourceQuery(
                jurisdiction=Jurisdiction(country="US"),
                kind=SourceKind.HOA,
                criteria={"hoa_name": "Example HOA"},
            )
        )
        self.assertIs(result.status, SourceResultStatus.FAILED)
        self.assertNotIn(SENTINEL, result.model_dump_json())

    def test_nested_encoded_and_mixed_case_key_copies_are_redacted(self):
        payload = {
            "API_KEY": SENTINEL,
            SENTINEL: "Provider also echoed its key in a property name",
            "rows": [
                "https://serpapi.com/search?" + urlencode({"API_KEY": SENTINEL}),
                "https://serpapi.com/search?%61pi_key=" + SENTINEL,
                {"snippet": "Provider echoed " + SENTINEL},
            ],
        }
        redacted = _redact_serpapi_payload(payload, secret=SENTINEL)
        self.assertNotIn(SENTINEL, json.dumps(redacted))
        self.assertIn(SENTINEL, json.dumps(payload))

    def test_successful_results_and_logs_do_not_leak_the_configured_key(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "organic_results": [
                {"title": "Example HOA", "link": "https://example.org/contact", "snippet": SENTINEL}
            ],
            "search_metadata": {"debug": SENTINEL},
        }
        session = mock.Mock()
        session.get.return_value = response
        client = SerpApiHoaContactClient(SENTINEL, session=session, page_fetcher=lambda _: None)
        with self.assertLogs("title_mcp.sources.hoa_serpapi", level="INFO") as logs:
            record = client.hoa_contact_search(HoaContactSearchQuery(hoa_name="Example HOA"))
        self.assertGreater(record.result_count, 0)
        self.assertNotIn(SENTINEL, record.model_dump_json())
        self.assertNotIn(SENTINEL, " ".join(logs.output))
