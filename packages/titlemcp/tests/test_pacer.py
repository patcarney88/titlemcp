from __future__ import annotations

import unittest
from unittest import mock

from title_mcp.domain.models import Jurisdiction
from title_mcp.settings import TitleMCPSettings
from title_mcp.sources import SourceKind, SourceQuery, SourceResultStatus
from title_mcp.sources.pacer import (
    PacerBankruptcyCaseRecord,
    PacerBankruptcySearchQuery,
    PacerBankruptcySearchRecord,
    PacerBankruptcySearchSummary,
    PacerBankruptcySourceConnector,
    PacerClient,
    build_bankruptcy_party_search_criteria,
    clean_business_name,
    clean_last_name_for_pacer,
    is_valid_ssn_deep,
    redacted_criteria,
)


class PacerTests(unittest.IsolatedAsyncioTestCase):
    def test_ssn_validation_uses_deeper_rules(self) -> None:
        self.assertTrue(is_valid_ssn_deep("111-22-3333"))
        self.assertFalse(is_valid_ssn_deep("000-22-3333"))
        self.assertFalse(is_valid_ssn_deep("666-22-3333"))
        self.assertFalse(is_valid_ssn_deep("912-22-3333"))
        self.assertFalse(is_valid_ssn_deep("123-45-6789"))

    def test_name_normalization_preserves_business_names(self) -> None:
        self.assertEqual(clean_last_name_for_pacer("Smith, John"), "Smith")
        self.assertEqual(clean_last_name_for_pacer("Smith-Jones"), "Smith-Jones")
        self.assertEqual(clean_business_name(" Example Holdings, LLC "), "Example Holdings, LLC")

    def test_person_criteria_redacts_ssn_for_return_payloads(self) -> None:
        criteria = build_bankruptcy_party_search_criteria(
            PacerBankruptcySearchQuery(
                last_name="Smith, John",
                first_name="John",
                ssn="111-22-3333",
                exact_name_match=True,
            )
        )

        self.assertEqual(criteria["lastName"], "Smith")
        self.assertEqual(criteria["firstName"], "John")
        self.assertEqual(criteria["ssn"], "111223333")
        self.assertEqual(criteria["exactNameMatch"], "true")
        self.assertEqual(redacted_criteria(criteria)["ssn"], "***-**-3333")

    def test_entity_criteria_uses_business_name_with_ein(self) -> None:
        criteria = build_bankruptcy_party_search_criteria(
            PacerBankruptcySearchQuery(
                tax_id_type="EIN",
                business_name="Example Holdings, LLC",
            )
        )

        self.assertEqual(criteria["lastName"], "Example Holdings, LLC")
        self.assertNotIn("ssn", criteria)

    def test_entity_criteria_infers_business_search_from_business_name(self) -> None:
        criteria = build_bankruptcy_party_search_criteria(
            PacerBankruptcySearchQuery(business_name="Example Holdings LLC")
        )

        self.assertEqual(criteria["lastName"], "Example Holdings LLC")
        self.assertNotIn("ssn", criteria)

    async def test_source_requires_configuration_without_credentials(self) -> None:
        connector = PacerBankruptcySourceConnector(
            settings=TitleMCPSettings(
                pacer_username=None,
                pacer_password=None,
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
                kind=SourceKind.COURT,
                criteria={"last_name": "Smith"},
            )
        )

        self.assertEqual(result.status, SourceResultStatus.REQUIRES_CONFIGURATION)
        self.assertIn("TITLE_MCP_PACER_USERNAME", result.warnings[0])

    async def test_source_returns_structured_bankruptcy_record(self) -> None:
        connector = PacerBankruptcySourceConnector(client=_FakePacerClient())

        result = await connector.query(
            SourceQuery(
                jurisdiction=Jurisdiction(country="US"),
                kind=SourceKind.COURT,
                criteria={"last_name": "Smith", "ssn": "111-22-3333"},
            )
        )

        self.assertEqual(result.status, SourceResultStatus.SUCCEEDED)
        self.assertEqual(result.records[0]["schema_name"], "title_mcp.pacer_bankruptcy_search")
        self.assertEqual(result.records[0]["query"]["ssn"], "***-**-3333")
        self.assertEqual(result.records[0]["cases"][0]["case_number"], "25-10001")
        self.assertTrue(result.records[0]["summary"]["title_officer_review_required"])
        self.assertTrue(result.metadata["title_officer_review_required"])


class _FakePacerClient:
    def bankruptcy_search(self, query: PacerBankruptcySearchQuery) -> PacerBankruptcySearchRecord:
        return PacerBankruptcySearchRecord(
            source={
                "source_id": PacerBankruptcySourceConnector.source_id,
                "source_name": "PACER Case Locator Bankruptcy Party Search",
                "environment": "qa",
                "retrieved_at": "2026-05-14T18:00:00+00:00",
            },
            query={
                "last_name": query.last_name,
                "ssn": "***-**-3333",
            },
            search_criteria={"lastName": "Smith", "ssn": "***-**-3333"},
            result_count=1,
            complete=True,
            total_result_count=1,
            cases=[
                PacerBankruptcyCaseRecord(
                    party_name="John Smith",
                    first_name="John",
                    last_name="Smith",
                    court_id="ohsb",
                    case_number="25-10001",
                    date_filed="2025-01-05",
                    bankruptcy_chapter="7",
                    disposition_method=None,
                    is_open=True,
                    raw={"firstName": "John", "lastName": "Smith"},
                )
            ],
            summary=PacerBankruptcySearchSummary(
                title_officer_review_required=True,
                text=(
                    "TITLE OFFICER REVIEW IS REQUIRED\n\nJohn Smith - Ch.7 - 2025 - OHSB - 25-10001"
                ),
            ),
            duration_seconds=0.01,
        )


class PacerCompletenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_paginated_or_missing_metadata_never_clears_a_search(self):
        page = {
            "number": 0,
            "totalPages": 2,
            "totalElements": 55,
            "numberOfElements": 1,
            "first": True,
            "last": False,
        }
        for metadata in (page, None, {**page, "totalPages": "2"}):
            with self.subTest(metadata=metadata):
                client = PacerClient("synthetic-user", "synthetic-passphrase")
                client.auth_token = str(id(client))
                response = {"content": [{"lastName": "Example", "dateTermed": "2025-01-01"}]}
                if metadata is not None:
                    response["pageInfo"] = metadata
                with mock.patch.object(
                    client, "advanced_party_search", return_value=response
                ) as call:
                    result = await PacerBankruptcySourceConnector(client=client).query(
                        SourceQuery(
                            jurisdiction=Jurisdiction(country="US"),
                            kind=SourceKind.COURT,
                            criteria={"last_name": "Example"},
                        )
                    )
                self.assertIs(result.status, SourceResultStatus.PARTIAL)
                record = result.records[0]
                self.assertFalse(record["complete"])
                self.assertTrue(record["summary"]["title_officer_review_required"])
                self.assertNotIn("REVIEW IS NOT REQUIRED", record["summary"]["text"])
                self.assertTrue(result.warnings)
                call.assert_called_once()

    async def test_proven_complete_empty_page_returns_no_results_with_review_required(self):
        client = PacerClient("synthetic-user", "synthetic-passphrase")
        client.auth_token = str(id(client))
        response = {
            "content": [],
            "pageInfo": {
                "number": 0,
                "totalPages": 0,
                "totalElements": 0,
                "numberOfElements": 0,
                "first": True,
                "last": True,
            },
        }
        with mock.patch.object(client, "advanced_party_search", return_value=response):
            result = await PacerBankruptcySourceConnector(client=client).query(
                SourceQuery(
                    jurisdiction=Jurisdiction(country="US"),
                    kind=SourceKind.COURT,
                    criteria={"last_name": "Example"},
                )
            )
        self.assertIs(result.status, SourceResultStatus.NO_RESULTS)
        self.assertTrue(result.records[0]["complete"])
        self.assertEqual(result.records[0]["total_result_count"], 0)
        self.assertTrue(result.records[0]["summary"]["title_officer_review_required"])


if __name__ == "__main__":
    unittest.main()
