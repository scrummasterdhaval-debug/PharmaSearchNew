import csv
import json
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import repository
from sources import americas_registers, asia_registers, europe_registers, national_registers
from bs4 import BeautifulSoup

from export_service import build_export_rows
from models.regulatory import RegulatoryProduct
from repository import (
    get_persisted_search_job,
    get_persisted_search_job_results,
    save_search_job,
    save_search_job_progress,
    save_search_job_results,
)
from services.evidence_assertions import missing_reason_for_row
from services.evidence_backfill import backfill_evidence
from sources.ema import _ema_result_from_record, _expand_xlsx_records
from sources.synonyms import get_substance_search_terms
from sources.eu_mri import rows_from_products, run_eu_mri_search, search_filter
from sources.medsafe import _fallback_rows as _medsafe_fallback_rows
from sources.mhra_document_parser import (
    _document_links_from_html,
    _invalid_manufacturer_value,
    _metadata_from_text,
)
from sources.parser import (
    clean_product_name,
    extract_dosage_form,
    extract_registration_number,
    extract_strength,
)
from sources.regional_live import (
    REGIONAL_SOURCES,
    parse_regional_source_results,
    run_cdsco_india_search,
    run_nmpa_china_search,
)
from services.field_completion import (
    _completions_for_group,
    attach_reference_documents,
    complete_fields,
    names_two_strengths,
)
from services.harvest import CONSECUTIVE_FAILURE_LIMIT, run_harvest
from services.harvest_vocabulary import (
    _Accumulator,
    is_combination,
    molecule_group_key,
    row_molecule_key,
    normalize_molecule,
    split_combination,
)
from sources.connectors.base import SourceMetadata
from sources.source_registry import CONNECTORS, SOURCES, connector_metadata
from sources.tga import _fallback_rows, _merge_detail, _parse_artg_detail, _parse_artg_search_results
from services.ai_client import ai_extract_regulatory_fields, ai_status
from services.ai_enrichment import enrichment_metadata, missing_enrichment_fields
from services.connector_health import connector_health_rows, record_source_health
from services.connector_status import connector_status_rows
from services.english_normalizer import english_row, english_text
from services.field_availability import (
    NOT_COLLECTED,
    NOT_SUPPLIED,
    NOT_SUPPLIED_LABEL,
    PENDING_ENRICHMENT,
    field_value,
)
from services.result_formatter import manufacturer_name_value
from services.search_pipeline import (
    _country_lookup_rows,
    _eu_lookup_rows,
    combined_search,
    filtered_search_results,
    prepared_cached_results,
    sources_for_scope,
    suppress_generic_lookup_rows,
    is_connector_lookup_fallback,
    row_relevant_to_substance,
    store_live_rows,
)
from services.freshness import source_freshness
from services.search_jobs import SLOW_SOURCES, _dedupe_rows, order_sources_for_job, source_skipped_in_mode
from services.therapeutic_category import short_therapeutic_category


class CachedResultPreparationTests(unittest.TestCase):
    def test_prepares_background_results_without_mutating_saved_rows(self):
        saved_rows = [{"substance": "metformin", "product": "Metformin 500 mg", "source": "FDA"}]

        prepared = prepared_cached_results(saved_rows)

        self.assertEqual(saved_rows[0].get("data_confidence"), None)
        self.assertEqual(prepared[0]["product"], "Metformin 500 mg")
        self.assertIn("data_confidence", prepared[0])


class StructuredEvidenceRepositoryTests(unittest.TestCase):
    def test_persists_multiple_sites_documents_and_field_evidence(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.save_product_detail(
                {
                    "substance": "example",
                    "product": "Example 10 mg tablets",
                    "country": "Exampleland",
                    "source": "Example Regulator",
                    "registration_number": "EX-1",
                    "product_url": "https://regulator.test/products/1",
                    "smpc_url": "https://regulator.test/products/1/smpc.pdf",
                    "manufacturers": [
                        {
                            "name": "Example Manufacturing Ltd",
                            "address": "Site One",
                            "country": "Germany",
                            "role": "FINISHED_PRODUCT_MANUFACTURER",
                            "verification_status": "VERIFIED_OFFICIAL_DOCUMENT",
                        },
                        {
                            "name": "Example Release GmbH",
                            "address": "Site Two",
                            "country": "Germany",
                            "role": "BATCH_RELEASE_MANUFACTURER",
                            "verification_status": "VERIFIED_OFFICIAL_DOCUMENT",
                        },
                    ],
                    "evidence": [{
                        "field_name": "manufacturer_name",
                        "value": "Example Manufacturing Ltd",
                        "evidence_url": "https://regulator.test/products/1/smpc.pdf",
                        "evidence_page": "42",
                        "verification_status": "VERIFIED_OFFICIAL_DOCUMENT",
                    }],
                }
            )
            row = repository.search_product_details("example")[0]
            self.assertEqual(len(row["manufacturers"]), 2)
            self.assertEqual(row["documents"][0]["document_type"], "SMPC")
            self.assertEqual(row["evidence"][0]["evidence_page"], "42")

    def test_registry_handoff_is_not_persisted_as_a_product(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            result = repository.save_product_detail(
                {
                    "substance": "example",
                    "product": "Example official registry search",
                    "source": "Example Regulator",
                    "connector_mode": "manual_registry",
                }
            )
            self.assertEqual(result["persistence_status"], "SOURCE_RUN_ONLY")
            self.assertEqual(repository.search_product_details("example"), [])


class FieldAvailabilityTests(unittest.TestCase):
    def test_missing_document_is_not_claimed_unavailable_without_an_attempt(self):
        self.assertEqual(field_value({"source": "FDA"}, "smpc_url"), NOT_SUPPLIED_LABEL)

    def test_manufacturer_is_not_pending_for_fda_schema(self):
        row = {"source": "FDA", "product_url": "https://example.test/label"}
        self.assertEqual(field_value(row, "manufacturer_name"), NOT_COLLECTED)

    def test_manufacturer_is_not_pending_for_regional_api_schema(self):
        row = {"source": "BPOM Indonesia", "product_url": "https://example.test/product"}
        self.assertEqual(field_value(row, "manufacturer_name"), NOT_COLLECTED)

    def test_missing_mhra_manufacturer_is_marked_for_enrichment(self):
        row = {"source": "MHRA", "pil_url": "https://example.test/pil.pdf"}
        self.assertEqual(field_value(row, "manufacturer_name"), PENDING_ENRICHMENT)

    def test_missing_mhra_manufacturer_is_not_pending_after_document_parse(self):
        row = {
            "source": "MHRA",
            "pil_url": "https://example.test/pil.pdf",
            "document_enrichment_attempted": True,
        }
        self.assertEqual(field_value(row, "manufacturer_name"), NOT_SUPPLIED_LABEL)

    def test_export_excludes_registry_handoff_rows(self):
        rows = build_export_rows(
            "metformin",
            [
                {
                    "substance": "metformin",
                    "product": "Official registry search",
                    "source": "NMPA China",
                    "connector_mode": "manual_registry",
                    "document_type": "Official registry search handoff",
                }
            ],
        )
        self.assertEqual(rows, [])


class SubstanceRelevanceTests(unittest.TestCase):
    def test_combination_search_rejects_single_ingredient_record(self):
        row = {
            "source": "FDA",
            "product": "Prilocaine topical cream",
            "source_substance": "prilocaine",
        }
        self.assertFalse(row_relevant_to_substance(row, "Prilocaine+Lidocaine"))

    def test_a_combination_matches_every_registers_language_and_us_names(self):
        query = "paracetamol +Caffeine"
        for row in (
            {"source": "AIFA Italy", "product": "NEO NISIDINA", "source_substance": "PARACETAMOLO/ACIDO ACETILSALICILICO/CAFFEINA"},
            {"source": "ANVISA Brazil", "product": "BESEROL", "source_substance": "paracetamol, carisoprodol, cafeína"},
            {"source": "FDA", "product": "Excedrin", "source_substance": "ASPIRIN; ACETAMINOPHEN; CAFFEINE"},
        ):
            self.assertTrue(row_relevant_to_substance(row, query), row["source"])
        # The searched term stored on the row is not evidence it is a combination.
        tylenol = {"source": "FDA", "product": "Tylenol", "source_substance": "ACETAMINOPHEN", "substance": query}
        self.assertFalse(row_relevant_to_substance(tylenol, query))

    def test_a_typed_combination_is_searched_as_each_register_understands_it(self):
        terms = get_substance_search_terms("paracetamol +Caffeine")
        for expected in ("acetaminophen caffeine", "paracetamol", "acetaminophen", "caffeine"):
            self.assertIn(expected, terms)

    def test_combination_search_accepts_both_ingredients(self):
        row = {
            "source": "FDA",
            "product": "Lidocaine and Prilocaine cream",
            "source_substance": "lidocaine prilocaine",
        }
        self.assertTrue(row_relevant_to_substance(row, "Prilocaine+Lidocaine"))


class LiveSourceSelectionTests(unittest.TestCase):
    @patch("services.search_pipeline.search_medicines", return_value=[])
    @patch("services.search_pipeline.search_product_details")
    @patch("services.search_pipeline.search_substance", return_value=[])
    def test_cached_sources_and_live_sources_are_separate(
        self,
        search_substance_mock,
        search_product_details_mock,
        _search_medicines_mock,
    ):
        search_product_details_mock.return_value = [
            {
                "substance": "metformin",
                "product": "Cached MHRA product",
                "source": "MHRA",
                "country": "United Kingdom",
            }
        ]

        rows = combined_search(
            "metformin",
            sources=["FDA", "MHRA"],
            live_sources=["FDA"],
            live_timeout=5,
            force_live=True,
        )

        self.assertEqual(rows[0]["source"], "MHRA")
        self.assertEqual(search_substance_mock.call_args.kwargs["source_names"], ["FDA"])


class StoreFirstFreshnessTests(unittest.TestCase):
    """A source is asked live unless it was checked recently and the store kept its answer."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self._db = patch.object(repository, "DB_PATH", Path(self._directory.name) / "fresh.db")
        self._db.start()
        repository.initialize_database()

    def tearDown(self):
        self._db.stop()
        self._directory.cleanup()

    def _run(self, source, status, found, days_ago=0.0, query="metformin"):
        when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        with repository.get_connection() as conn:
            conn.execute(
                "INSERT INTO source_runs(source, query, status, records_found, checked_at) VALUES (?, ?, ?, ?, ?)",
                (source, query, status, found, when),
            )

    def _save(self, source, rows, days_ago=0.0, query="metformin"):
        when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        with repository.get_connection() as conn:
            conn.execute(
                "INSERT INTO live_saves(source, query, rows_saved, saved_at) VALUES (?, ?, ?, ?)",
                (source, query, rows, when),
            )

    def _decision(self, source, max_age_days=7):
        decisions = source_freshness("metformin", [source], max_age_days=max_age_days)
        return {item.source: item for item in decisions}[source]

    def test_a_source_never_checked_is_asked(self):
        self.assertTrue(self._decision("MHRA").ask_live)

    def test_a_molecule_the_source_does_not_list_is_not_asked_again(self):
        self._run("MHRA", "NOT_PUBLISHED", 0, days_ago=1)
        self.assertFalse(self._decision("MHRA").ask_live)

    def test_presence_is_not_coverage(self):
        # Checked, and the store may hold rows for it, but no search stored the
        # answer and read it back: the old search saved only the page it showed.
        self._run("MHRA", "SUCCESS", 300, days_ago=1)
        decision = self._decision("MHRA")
        self.assertTrue(decision.ask_live)
        self.assertEqual(decision.reason, "answer not held in the store")

    def test_a_recent_check_whose_answer_was_stored_is_not_asked(self):
        self._run("MHRA", "SUCCESS", 12, days_ago=1)
        self._save("MHRA", 8, days_ago=0.99)
        decision = self._decision("MHRA")
        self.assertFalse(decision.ask_live)
        self.assertTrue(decision.checked_at)

    def test_a_check_after_the_save_is_not_covered(self):
        # A synonym that answered after the search stopped waiting: its rows were never stored.
        self._save("MHRA", 8, days_ago=1)
        self._run("MHRA", "SUCCESS", 12, days_ago=0.99)
        self.assertTrue(self._decision("MHRA").ask_live)

    def test_an_old_check_is_asked_again(self):
        self._run("MHRA", "SUCCESS", 3, days_ago=8)
        self._save("MHRA", 3, days_ago=7.9)
        self.assertTrue(self._decision("MHRA").ask_live)

    def test_a_failed_check_is_asked_again(self):
        self._run("MHRA", "SUCCESS", 3, days_ago=2)
        self._save("MHRA", 3, days_ago=1.9)
        self._run("MHRA", "PARSER_FAILED", 0, days_ago=1)
        self.assertTrue(self._decision("MHRA").ask_live)

    def test_max_age_zero_asks_everything(self):
        self._run("MHRA", "NOT_PUBLISHED", 0, days_ago=0.01)
        self.assertTrue(self._decision("MHRA", max_age_days=0).ask_live)

    def test_local_registers_and_view_only_sources_are_always_asked(self):
        for source in ["AIFA Italy", "BfArM Germany"]:
            self._run(source, "NOT_PUBLISHED", 0, days_ago=0.1)
            self.assertTrue(self._decision(source).ask_live, source)

    def test_query_matches_whatever_the_case(self):
        self._run("MHRA", "NOT_PUBLISHED", 0, days_ago=1, query="Metformin")
        self.assertFalse(self._decision("MHRA").ask_live)

    @patch("services.search_pipeline.store_live_rows_in_background")
    @patch("services.search_pipeline.search_substance", return_value=[])
    @patch("services.search_pipeline.search_medicines", return_value=[])
    @patch("services.search_pipeline.search_product_details")
    def test_combined_search_asks_only_the_stale_sources(self, stored_mock, _medicines, live_mock, _store):
        stored_mock.return_value = [
            {"substance": "metformin", "product": "Metformin 500 mg tablets", "source": "MHRA",
             "country": "United Kingdom"}
        ]
        self._run("MHRA", "SUCCESS", 1, days_ago=1)
        self._save("MHRA", 1, days_ago=0.9)
        report = []

        rows = combined_search("metformin", sources=["MHRA", "Spain CIMA"], freshness_report=report)

        self.assertEqual(live_mock.call_args.kwargs["source_names"], ["Spain CIMA"])
        self.assertEqual([row["product"] for row in rows], ["Metformin 500 mg tablets"])
        self.assertEqual({item.source: item.ask_live for item in report}, {"MHRA": False, "Spain CIMA": True})

        combined_search("metformin", sources=["MHRA", "Spain CIMA"], force_live=True)
        self.assertEqual(live_mock.call_args.kwargs["source_names"], ["MHRA", "Spain CIMA"])

    @patch("services.search_pipeline.store_live_rows_in_background")
    @patch("services.search_pipeline.search_substance")
    @patch("services.search_pipeline.search_medicines", return_value=[])
    @patch("services.search_pipeline.search_product_details", return_value=[])
    def test_combined_search_skips_the_live_call_when_the_store_answers_everything(
        self, _stored, _medicines, live_mock, store_mock
    ):
        self._run("MHRA", "NOT_PUBLISHED", 0, days_ago=1)

        combined_search("metformin", sources=["MHRA"])

        live_mock.assert_not_called()
        store_mock.assert_called_once_with("metformin", [])

    def _saves(self):
        with repository.get_connection() as conn:
            return {row[0]: row[1] for row in conn.execute("SELECT source, rows_saved FROM live_saves")}

    def test_store_live_rows_keeps_the_molecule_and_logs_what_came_back(self):
        rows = [
            {"substance": "metformin", "product": "Metformin 500 mg tablets", "source": "MHRA",
             "country": "United Kingdom", "registration_number": "PL 1"},
            # A full-text hit for another molecule is not stored under this one.
            {"substance": "metformin", "product": "Amlodipine 5 mg tablets", "source": "MHRA",
             "country": "United Kingdom", "registration_number": "PL 2"},
            {"substance": "metformin", "product": "Metformin Heumann", "source": "BfArM Germany",
             "country": "Germany", "registration_number": "1", "view_only": True},
        ]

        counts = store_live_rows("metformin", rows)

        self.assertEqual(counts, {"MHRA": 1})
        self.assertEqual(self._saves(), {"MHRA": 1})
        with repository.get_connection() as conn:
            products = [
                row[0] for row in conn.execute(
                    "SELECT product FROM product_details WHERE registration_number IN ('PL 1', 'PL 2', '1')"
                )
            ]
        self.assertEqual(products, ["Metformin 500 mg tablets"])

    def test_a_source_that_answered_only_about_other_molecules_holds_nothing(self):
        rows = [{"substance": "metformin", "product": "Amlodipine 5 mg tablets", "source": "MHRA",
                 "country": "United Kingdom", "registration_number": "PL 2"}]

        self.assertEqual(store_live_rows("metformin", rows), {"MHRA": 0})
        self.assertEqual(self._saves(), {"MHRA": 0})

    def test_a_source_the_store_cannot_hold_row_for_row_is_not_logged(self):
        # MHRA shows the SmPC and the leaflet of one licence as two rows; the
        # store keeps one row for the licence. Read back, one is missing, so
        # the store never answers for MHRA with half the rows.
        spc = {"substance": "metformin", "product": "Metformin 500 mg tablets", "source": "MHRA",
               "country": "United Kingdom", "registration_number": "PL 1",
               "product_url": "https://mhra.test/spc", "document_type": "SPC"}
        pil = dict(spc, product_url="https://mhra.test/pil", document_type="PIL")

        counts = store_live_rows("metformin", [spc, pil])

        self.assertEqual(counts, {})
        self.assertEqual(self._saves(), {})

class SearchPageFilterTests(unittest.TestCase):
    def test_every_result_column_has_a_header_filter(self):
        from main import search_page

        response = search_page("metformin", live=False, sources=["FDA"], page_size=10)
        soup = BeautifulSoup(response.body.decode(), "html.parser")
        table = soup.select_one("table.results-table")
        header_rows = table.select("thead tr")

        self.assertEqual(len(header_rows[0].select("th")), 26)
        filter_cells = header_rows[1].select("th")
        self.assertEqual(len(filter_cells), 26)
        self.assertTrue(all(cell.select_one("input, select") for cell in filter_cells))


class ParserTests(unittest.TestCase):
    def test_extracts_uk_registration_variants(self):
        self.assertEqual(
            extract_registration_number("Example 5 mg tablets - PLGB 12345/0001"),
            "PLGB 12345/0001",
        )
        self.assertEqual(
            extract_registration_number("Example syrup PLNI 99999/0002-001"),
            "PLNI 99999/0002-001",
        )

    def test_extracts_strength_and_dosage_form(self):
        product = "DAPAGLIFLOZIN 5 MG FILM-COATED TABLETS - PL 59787/0020"
        self.assertEqual(extract_strength(product), "5 MG")
        self.assertEqual(extract_dosage_form(product), "Film-coated tablet")

    def test_cleans_repeated_product_text(self):
        self.assertEqual(clean_product_name("Forxiga Forxiga PL 12345/0001"), "Forxiga PL 12345/0001")


class ExportTests(unittest.TestCase):
    def test_build_export_rows_adds_provenance_columns(self):
        rows = build_export_rows(
            "Dapagliflozin",
            [
                {
                    "substance": "Dapagliflozin",
                    "product": "Forxiga 10 mg film-coated tablets",
                    "country": "United Kingdom",
                    "source": "MHRA",
                    "url": "https://example.test/doc",
                }
            ],
        )
        self.assertEqual(rows[0]["Region"], "UK")
        self.assertEqual(rows[0]["Strength"], "10 mg")
        self.assertEqual(rows[0]["Dosage Form"], "Film-coated tablet")
        self.assertEqual(rows[0]["Source"], "MHRA")

    def test_build_export_rows_uses_mhra_document_metadata(self):
        rows = build_export_rows(
            "Ibuprofen",
            [
                {
                    "substance": "Ibuprofen",
                    "product": "Ibuprofen 200 mg tablets",
                    "country": "United Kingdom",
                    "source": "MHRA",
                    "company": "Example MA Holder Ltd",
                    "manufacturer_name": "Example Manufacturer Ltd",
                    "pack_size": "24 tablets",
                    "smpc_url": "https://example.test/spc.pdf",
                    "pil_url": "https://example.test/pil.pdf",
                }
            ],
        )
        self.assertEqual(rows[0]["MA Holder Name"], "Example MA Holder Ltd")
        self.assertEqual(rows[0]["Manufacturer Name"], "Example Manufacturer Ltd")
        self.assertEqual(rows[0]["Pack Size"], "24 tablets")
        self.assertEqual(rows[0]["SMPC URL"], "https://example.test/spc.pdf")
        self.assertEqual(rows[0]["PIL / Assessment Report"], "https://example.test/pil.pdf")


class MHRADocumentParserTests(unittest.TestCase):
    def test_rejects_leaflet_narrative_as_manufacturer(self):
        self.assertTrue(
            _invalid_manufacturer_value(
                "This leaflet was last revised in August 2022. EVER Pharma Jena GmbH "
                "Contents of the pack and other information"
            )
        )
        self.assertFalse(_invalid_manufacturer_value("EVER Pharma Jena GmbH"))

    def test_preserves_explicit_manufacturer_when_it_is_also_the_holder(self):
        row = {
            "company": "Henry Schein UK Holdings Ltd",
            "manufacturer_name": "Henry Schein UK Holdings Ltd.",
            "manufacturer_source": "MHRA document",
        }
        self.assertEqual(manufacturer_name_value(row), "Henry Schein UK Holdings Ltd.")

    def test_extracts_document_metadata_from_text(self):
        metadata = _metadata_from_text(
            """
            Marketing Authorisation Holder
            Example Pharma Ltd
            10 High Street

            Manufacturer responsible for batch release
            Example Manufacturing Ltd
            Industrial Estate

            Pack sizes
            24 tablets
            """
        )
        self.assertEqual(metadata["company"], "Example Pharma Ltd")
        self.assertEqual(metadata["manufacturer_name"], "Example Manufacturing Ltd")
        self.assertEqual(metadata["pack_size"], "24 tablets")

    def test_extracts_related_document_links_from_html(self):
        soup = BeautifulSoup(
            """
            <a href="/docs/example-spc.pdf">Summary of Product Characteristics</a>
            <a href="/docs/example-pil.pdf">Patient Information Leaflet</a>
            """,
            "html.parser",
        )
        links = _document_links_from_html(soup)
        self.assertEqual(links["smpc_url"], "https://products.mhra.gov.uk/docs/example-spc.pdf")
        self.assertEqual(links["pil_url"], "https://products.mhra.gov.uk/docs/example-pil.pdf")

    def test_extracts_multiple_manufacturers_and_pack_section(self):
        metadata = _metadata_from_text(
            """
            Marketing Authorisation Holder
            Mylan Ltd, Potters Bar, United Kingdom

            Manufacturer
            Merckle GmbH, Ludwig-Merckle-Strasse 3, Germany
            Mylan Hungary Kft, Mylan utca 1, Hungary
            McDermott Laboratories Ltd, Dublin, Ireland

            Contents of the pack
            The tablets are available in packs of 10, 30 and 60 tablets.
            """
        )
        self.assertEqual(metadata["company"], "Mylan Ltd")
        self.assertIn("Merckle GmbH", metadata["manufacturer_name"])
        self.assertIn("Mylan Hungary Kft", metadata["manufacturer_name"])
        self.assertEqual(metadata["manufacturer_country"], "Germany; Hungary; Ireland")
        self.assertIn("packs of 10, 30 and 60 tablets", metadata["pack_size"])


class EMAConnectorTests(unittest.TestCase):
    def test_maps_ema_record_to_source_schema(self):
        result = _ema_result_from_record(
            {
                "active_substance": "Metformin",
                "name_of_medicine": "Glucophage 500 mg tablets",
                "marketing_authorisation_holder_company_name": "Example Holder GmbH",
                "pharmaceutical_form": "Tablet",
                "medicine_url": "/en/medicines/human/EPAR/glucophage",
                "medicine_status": "Authorised",
                "ema_product_number": "EMEA/H/C/000000",
            },
            "Metformin",
            "Germany",
            "https://example.test/ema.xlsx",
        )

        self.assertEqual(result["substance"], "Metformin")
        self.assertEqual(result["product"], "Glucophage 500 mg tablets")
        self.assertEqual(result["company"], "Example Holder GmbH")
        self.assertEqual(result["country"], "Germany")
        self.assertEqual(result["strength"], "500 mg")
        self.assertEqual(result["dosage_form"], "Tablet")
        self.assertEqual(result["source"], "EMA")
        self.assertEqual(
            result["product_url"],
            "https://www.ema.europa.eu/en/medicines/human/EPAR/glucophage",
        )

    def test_ema_search_filters_by_active_substance(self):
        results = _expand_xlsx_records(
            [
                {
                    "active_substance": "Metformin hydrochloride",
                    "name_of_medicine": "Metformin Example 500 mg tablets",
                    "marketing_authorisation_developer_applicant_holder": "Example Holder",
                    "medicine_url": "https://example.test/metformin",
                },
                {
                    "active_substance": "Ibuprofen",
                    "name_of_medicine": "Ibuprofen Example",
                    "medicine_url": "https://example.test/ibuprofen",
                },
            ],
            "Metformin",
        )

        self.assertTrue(results)
        self.assertTrue(all(item["source"] == "EMA" for item in results))
        self.assertTrue(all("Metformin" in item["substance"] for item in results))


class EUMRIConnectorTests(unittest.TestCase):
    # One product as the index's ProductSearch API returns it (trimmed).
    PRODUCT = {
        "productKey": "MT/H/0651/001",
        "name": "Dapaglifozin/Metformin Hydrochloride Aurobindo Pharma 5 mg/850 mg film-coated tablets",
        "outcome": "Positive",
        "dateOfOutcome": "2026-01-14T00:00:00Z",
        "maHolder": "Aurobindo Pharma (Malta) Limited\r\nVault 14, Level 2\r\nValletta Waterfront\r\nMalta",
        "activeSubstances": [
            {"innName": "Dapagliflozin", "saltName": None, "amount": 5.0, "unitDescription": "mg"},
            {"innName": "Metformin", "saltName": "hydrochloride", "amount": 850.0, "unitDescription": "mg"},
        ],
        "atcCodes": [{"code": "A10BD15"}],
        "cms": [{"isoCode": "DE", "countryName": "Germany"}, {"isoCode": "IT", "countryName": "Italy"}],
        "doseForms": [{"doseFormName": "Film-coated tablet"}],
        "rms": {"isoCode": "MT", "countryName": "Malta"},
        "typeLevelCategory": {
            "typeLevelCategory2": "Multiple (Copy) Application",
            "typeLevelCategory3": "Generic",
            "typeLevelCategory5": "Prescription Only",
        },
        "withdrawalReasons": [],
    }

    def test_a_product_takes_holder_strength_form_and_member_states(self):
        rows = rows_from_products([self.PRODUCT], "metformin")

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source"], "EU MRI Product Index")
        self.assertEqual(row["company"], "Aurobindo Pharma (Malta) Limited")
        self.assertEqual(row["country"], "Malta")
        self.assertEqual(row["concerned_member_states"], "Germany, Italy")
        self.assertEqual(row["strength"], "5 mg/850 mg")
        self.assertEqual(row["dosage_form"], "Film-coated tablet")
        self.assertEqual(row["registration_number"], "MT/H/0651/001")
        self.assertEqual(row["registration_date"], "2026-01-14")
        self.assertEqual(row["atc_code"], "A10BD15")
        self.assertEqual(row["status"], "Authorised via MRP/DCP")
        self.assertEqual(row["active_substance"], "Dapagliflozin + Metformin hydrochloride")

    def test_a_combination_keeps_only_products_naming_every_molecule(self):
        self.assertEqual(rows_from_products([self.PRODUCT], "metformin + sitagliptin"), [])
        withdrawn = dict(self.PRODUCT, withdrawalReasons=[{"reason": "x"}])
        self.assertEqual(rows_from_products([withdrawn], "dapagliflozin")[0]["status"], "Withdrawn (MRP/DCP)")

    def test_a_salt_is_searched_by_its_molecule(self):
        self.assertIn("'atorvastatin'", search_filter("atorvastatin calcium"))

    def test_an_unreachable_index_hands_off_to_the_portal(self):
        with patch("sources.eu_mri._query_index", side_effect=RuntimeError("no browser")):
            rows = run_eu_mri_search("metformin")
        self.assertEqual(rows[0]["connector_mode"], "manual_registry")

    def test_mri_connector_returns_empty_for_blank_query(self):
        self.assertEqual(run_eu_mri_search(""), [])


class PlatformCoreTests(unittest.TestCase):
    def test_unified_product_converts_to_export_schema(self):
        product = RegulatoryProduct(
            source="Swissmedic",
            active_substance="Metformin",
            product_name="Metformin Example 500 mg tablets",
            marketing_authorisation_holder="Example Holder AG",
            country="Switzerland",
            region="CH",
            strength="500 mg",
            dosage_form="Tablet",
            product_url="https://example.test/product",
        )
        record = product.to_source_record()
        self.assertEqual(record["source"], "Swissmedic")
        self.assertEqual(record["substance"], "Metformin")
        self.assertEqual(record["product"], "Metformin Example 500 mg tablets")
        self.assertEqual(record["company"], "Example Holder AG")
        self.assertEqual(record["url"], "https://example.test/product")

    def test_connector_registry_exposes_target_regulators(self):
        names = {item["name"] for item in connector_metadata()}
        self.assertTrue(
            {
                "FDA",
                "MHRA",
                "EMA",
                "EU MRI Product Index",
                "Health Canada",
                "TGA Australia",
                "Medsafe New Zealand",
                "SAHPRA South Africa",
                "FDA Ghana",
                "SFDA Saudi Arabia",
                "Israel Drug Registry",
                "CDSCO India",
                "NMPA China",
                "BPOM Indonesia",
                "NPRA Malaysia",
                "FDA Philippines",
                "HSA Singapore",
                "MFDS South Korea",
                "Thai FDA",
                "DAV Vietnam",
                "Swissmedic",
                "PMDA Japan",
            }.issubset(names)
        )
        self.assertTrue(all("function" in item for item in SOURCES))
        self.assertTrue(all(connector.metadata.name for connector in CONNECTORS))

    def test_enabled_sources_include_tga_australia(self):
        self.assertIn("TGA Australia", {item["name"] for item in SOURCES})

    def test_enabled_sources_include_medsafe_new_zealand(self):
        self.assertIn("Medsafe New Zealand", {item["name"] for item in SOURCES})

    def test_eu_lookup_returns_country_row_for_missing_connector(self):
        rows = _eu_lookup_rows("atorvastatin", country="Greece")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Greece")
        self.assertEqual(rows[0]["source"], "EU National Registry")
        self.assertEqual(rows[0]["document_type"], "EU national lookup fallback")

    def test_filtered_search_adds_eu_country_fallback_when_empty(self):
        # Its own empty store: the real one now holds Greek rows, because the
        # weekly live refresh harvests EOF Greece, and this test is about what
        # happens when nothing is stored.
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "empty.db"
        ):
            rows, _, _ = filtered_search_results(
                "atorvastatin",
                live=False,
                sources=["EMA"],
                country="Greece",
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Greece")
        self.assertEqual(rows[0]["source"], "EU National Registry")

    def test_country_lookup_returns_row_for_any_country(self):
        # Brazil, then Argentina, were the example here until each gained a
        # region; Iceland has neither a connector nor a region.
        rows = _country_lookup_rows("atorvastatin", country="Iceland")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Iceland")
        self.assertEqual(rows[0]["region"], "Global")
        self.assertEqual(rows[0]["source"], "Regulatory Registry Lookup")
        # A Latin American country without a connector is looked up in its region.
        argentina = _country_lookup_rows("atorvastatin", country="Argentina")
        self.assertEqual((argentina[0]["region"], argentina[0]["source"]), ("LA", "Latin America Generic Registry Lookup"))

    def test_filtered_search_adds_global_country_fallback_when_empty(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["EMA"],
            country="Japan",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Japan")
        self.assertEqual(rows[0]["region"], "JP")
        self.assertEqual(rows[0]["source"], "Regulatory Registry Lookup")

    def test_country_lookup_returns_new_zealand_row(self):
        rows = _country_lookup_rows("atorvastatin", country="New Zealand")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "New Zealand")
        self.assertEqual(rows[0]["region"], "NZ")

    def test_country_lookup_returns_africa_country_row(self):
        rows = _country_lookup_rows("atorvastatin", country="Kenya")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Kenya")
        self.assertEqual(rows[0]["region"], "AF")
        self.assertEqual(rows[0]["source"], "Africa Generic Registry Lookup")
        self.assertEqual(rows[0]["document_type"], "Generic Africa registry lookup fallback")
        self.assertIn("pharmacyboardkenya", rows[0]["url"])

    def test_country_lookup_returns_middle_east_country_row(self):
        rows = _country_lookup_rows("atorvastatin", country="Saudi Arabia")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Saudi Arabia")
        self.assertEqual(rows[0]["region"], "ME")
        self.assertEqual(rows[0]["source"], "Middle East Generic Registry Lookup")
        self.assertEqual(rows[0]["document_type"], "Generic Middle East registry lookup fallback")
        self.assertIn("sfda.gov.sa", rows[0]["url"])

    def test_region_lookup_returns_africa_rows(self):
        rows = _country_lookup_rows("atorvastatin", region="AF")
        countries = {row["country"] for row in rows}
        self.assertEqual(len(rows), 54)
        self.assertIn("South Africa", countries)
        self.assertIn("Nigeria", countries)
        self.assertIn("Zimbabwe", countries)
        self.assertTrue(all(row["region"] == "AF" for row in rows))
        self.assertTrue(all(row["source"] == "Africa Generic Registry Lookup" for row in rows))

    def test_region_lookup_returns_middle_east_rows(self):
        rows = _country_lookup_rows("atorvastatin", region="ME")
        countries = {row["country"] for row in rows}
        self.assertEqual(len(rows), 17)
        self.assertIn("Saudi Arabia", countries)
        self.assertIn("United Arab Emirates", countries)
        self.assertIn("Yemen", countries)
        self.assertTrue(all(row["region"] == "ME" for row in rows))
        self.assertTrue(all(row["source"] == "Middle East Generic Registry Lookup" for row in rows))

    def test_country_lookup_returns_asia_country_row(self):
        rows = _country_lookup_rows("atorvastatin", country="India")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "India")
        self.assertEqual(rows[0]["region"], "AS")
        self.assertEqual(rows[0]["source"], "Asia Generic Registry Lookup")
        self.assertEqual(rows[0]["document_type"], "Generic Asia registry lookup fallback")
        self.assertIn("cdsco.gov.in", rows[0]["url"])

    def test_sources_for_scope_uses_india_connector_for_india_country(self):
        sources = sources_for_scope(
            ["CDSCO India"],
            country="India",
        )
        self.assertEqual(sources, ["CDSCO India"])

    def test_sources_for_scope_auto_adds_country_connector_when_missing(self):
        sources = sources_for_scope(
            ["FDA", "Hong Kong Drug Office", "France BDPM", "Spain CIMA"],
            country="China",
        )
        self.assertEqual(sources, ["NMPA China"])

    def test_manual_registry_rows_are_not_treated_as_product_records(self):
        self.assertTrue(
            is_connector_lookup_fallback(
                {
                    "country": "China",
                    "source": "NMPA China",
                    "product": "paracetamol official China registry search",
                    "document_type": "Official registry search handoff",
                    "connector_mode": "manual_registry",
                }
            )
        )

    def test_region_lookup_returns_asia_rows(self):
        rows = _country_lookup_rows("atorvastatin", region="AS")
        countries = {row["country"] for row in rows}
        self.assertIn("India", countries)
        self.assertIn("China", countries)
        self.assertIn("Saudi Arabia", countries)
        self.assertIn("Japan", countries)
        self.assertTrue(all(row["region"] == "AS" for row in rows))
        self.assertTrue(all(row["source"] == "Asia Generic Registry Lookup" for row in rows))

    def test_region_lookup_returns_all_country_rows(self):
        rows = _country_lookup_rows("atorvastatin", region="ALL")
        countries = {row["country"] for row in rows}
        self.assertIn("India", countries)
        self.assertIn("South Africa", countries)
        self.assertIn("Saudi Arabia", countries)
        self.assertIn("United States", countries)
        self.assertTrue(all(row["region"] == "ALL" for row in rows))
        self.assertTrue(all(row["source"] == "Global Generic Registry Lookup" for row in rows))

    def test_filtered_search_adds_africa_region_fallback_when_empty(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["EMA"],
            region="AF",
        )
        self.assertEqual(len(rows), 54)
        self.assertTrue(all(row["region"] == "AF" for row in rows))

    def test_filtered_search_keeps_africa_connector_rows_and_fills_missing_countries(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["SAHPRA South Africa"],
            region="AF",
        )
        countries = {row["country"] for row in rows}
        self.assertIn("South Africa", countries)
        self.assertIn("Zimbabwe", countries)
        self.assertEqual(len(countries), 54)

    def test_filtered_search_adds_middle_east_region_fallback_when_empty(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["EMA"],
            region="ME",
        )
        self.assertEqual(len(rows), 17)
        self.assertTrue(all(row["region"] == "ME" for row in rows))

    def test_filtered_search_adds_asia_region_fallback_when_empty(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["EMA"],
            region="AS",
        )
        self.assertGreaterEqual(len(rows), 10)
        self.assertTrue(all(row["region"] == "AS" for row in rows))

    def test_filtered_search_adds_all_country_fallback_when_empty(self):
        rows, _, _ = filtered_search_results(
            "atorvastatin",
            live=False,
            sources=["EMA"],
            region="ALL",
        )
        self.assertGreaterEqual(len(rows), 100)
        self.assertIn("Global Generic Registry Lookup", {row["source"] for row in rows})

    def test_suppresses_generic_row_when_country_connector_row_exists(self):
        rows = suppress_generic_lookup_rows(
            [
                {
                    "country": "India",
                    "source": "CDSCO India",
                    "product": "CDSCO India regulator lookup",
                },
                {
                    "country": "India",
                    "source": "Asia Generic Registry Lookup",
                    "product": "paracetamol India generic regulatory registry lookup",
                },
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "CDSCO India")


class RegionalLiveConnectorTests(unittest.TestCase):
    def test_parses_regional_source_table_results(self):
        rows = parse_regional_source_results(
            """
            <table>
              <tr>
                <th>Product Name</th>
                <th>Active Ingredient</th>
                <th>Company</th>
                <th>Registration No</th>
                <th>Status</th>
              </tr>
              <tr>
                <td><a href="/product/1">Atorvastatin Example 20 mg tablet</a></td>
                <td>atorvastatin calcium</td>
                <td>Example Holder</td>
                <td>REG-123</td>
                <td>Registered</td>
              </tr>
            </table>
            """,
            "atorvastatin",
            REGIONAL_SOURCES["SFDA Saudi Arabia"],
            "https://www.sfda.gov.sa/en/drugs-list",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "SFDA Saudi Arabia")
        self.assertEqual(rows[0]["country"], "Saudi Arabia")
        self.assertEqual(rows[0]["region"], "ME")
        self.assertEqual(rows[0]["company"], "Example Holder")
        self.assertEqual(rows[0]["registration_number"], "REG-123")
        self.assertEqual(rows[0]["strength"], "20 mg")
        self.assertEqual(rows[0]["dosage_form"], "Tablet")

    def test_regional_fallback_uses_stable_product_for_synonym_dedupe(self):
        from sources.regional_live import _fallback_rows

        rows = _fallback_rows(REGIONAL_SOURCES["CDSCO India"], "acetaminophen")
        self.assertEqual(rows[0]["product"], "acetaminophen official India registry search")
        self.assertEqual(rows[0]["connector_mode"], "manual_registry")

    def test_cdsco_india_search_delegates_to_approvals_connector(self):
        expected = [
            {
                "substance": "dapagliflozin",
                "product": "Dapagliflozin approval row",
                "country": "India",
                "source": "CDSCO India",
            }
        ]

        with patch("sources.regional_live._run_cdsco_india_search", return_value=expected) as connector:
            rows = run_cdsco_india_search("dapagliflozin")

        connector.assert_called_once_with("dapagliflozin")
        self.assertEqual(rows, expected)

    def test_cdsco_india_search_falls_back_when_no_approvals_match(self):
        with patch("sources.regional_live._run_cdsco_india_search", return_value=[]):
            rows = run_cdsco_india_search("acetaminophen")

        self.assertEqual(rows[0]["connector_mode"], "manual_registry")
        self.assertEqual(rows[0]["country"], "India")


class CDSCOIndiaConnectorTests(unittest.TestCase):
    PAYLOAD = {
        "iTotalRecords": 2,
        "aaData": [
            {
                "num_form_id": 39942,
                "str_man_unit_name": "PURE & CURE HEALTHCARE Pvt. Ltd.",
                "str_address": "305, Mohan Place, Saraswati Vihar, , Delhi, India, 110034",
                "str_drug_name": "Dapagliflozin+Metoprolol Succinate (Er)",
                "str_composition": "Dapagliflozin Propanediol Monohydrate Eq. To Dapagliflozin 10.0000 Milligram (Mg)",
                "manuf_addr": "Precise Chemipharma Pvt.Ltd, Navi Mumbai Maharashtra India-400703"
                "<br>Reine Lifescience, Ankleshwar Gujarat India-393002",
                "str_dosage": "Tablets",
                "str_indication": "Indicated In Patients With Heart Failure",
                "dt_closure_dt": "09-JAN-2026",
                "str_applied_for": "Finished Formulation",
            },
            {
                "num_form_id": 54683,
                "str_man_unit_name": "Serum Institute Of India Pvt. Ltd.",
                "str_address": "212/2, Hadapsar, , Maharashtra, India, 411028",
                "str_drug_name": "Hepatitis B Surface Antigen Concentrate Bulk",
                "str_composition": "Hepatitis B Protein 0.2000 Mg/Ml",
                "manuf_addr": "Serum Institute Of India Pvt Ltd.., Pune Maharashtra India-411028",
                "str_dosage": "NA",
                "str_indication": "NA",
                "dt_closure_dt": "13-AUG-2026",
                "str_applied_for": "Bulk Drug",
            },
        ],
    }

    def setUp(self):
        from sources import cdsco_india

        # The connector caches the corpus on disk and in memory; both have to be
        # bypassed so a real cache file cannot mask the payload under test.
        cdsco_india._MEMORY_CACHE.clear()
        self.addCleanup(cdsco_india._MEMORY_CACHE.clear)
        for target in ("_read_cache", "_write_cache"):
            patcher = patch.object(
                cdsco_india, target, return_value=None if target == "_read_cache" else None
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def _search(self, substance, payload=None):
        from sources import cdsco_india

        response = MagicMock()
        response.json.return_value = payload or self.PAYLOAD
        response.raise_for_status.return_value = None
        with patch.object(cdsco_india.requests, "get", return_value=response):
            return cdsco_india.run_cdsco_india_search(substance)

    def test_maps_company_and_supply_type(self):
        rows = self._search("dapagliflozin")

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["company"], "PURE & CURE HEALTHCARE Pvt. Ltd.")
        self.assertEqual(row["country"], "India")
        self.assertEqual(row["source"], "CDSCO India")
        self.assertEqual(row["status"], "Approved (finished formulation)")
        self.assertEqual(row["dosage_form"], "Tablets")
        self.assertEqual(row["registration_number"], "39942")

    def test_all_manufacturing_sites_are_kept_apart_from_the_applicant(self):
        row = self._search("dapagliflozin")[0]

        self.assertEqual(row["manufacturer_name"], "PURE & CURE HEALTHCARE Pvt. Ltd.")
        self.assertEqual(row["manufacturer_country"], "India")
        self.assertEqual(len(row["manufacturers"]), 2)
        self.assertEqual(
            row["manufacturers"][0]["address"],
            "Precise Chemipharma Pvt.Ltd, Navi Mumbai Maharashtra India-400703",
        )
        self.assertEqual(row["manufacturers"][0]["role"], "FINISHED_PRODUCT_MANUFACTURER")

    def test_bulk_drug_approvals_are_labelled_as_api(self):
        row = self._search("hepatitis b")[0]

        self.assertEqual(row["company"], "Serum Institute Of India Pvt. Ltd.")
        self.assertEqual(row["status"], "Approved (bulk drug / API)")
        # "NA" placeholders must not leak into the output.
        self.assertEqual(row["therapeutic_category"], "")

    def test_ignores_rows_that_only_mention_the_substance_in_the_indication(self):
        payload = {
            "aaData": [
                {
                    "num_form_id": 1,
                    "str_man_unit_name": "Some Pharma Ltd",
                    "str_drug_name": "Ramipril Tablets",
                    "str_composition": "Ramipril 5mg",
                    "str_indication": "Used alongside dapagliflozin therapy",
                    "str_applied_for": "Finished Formulation",
                    "manuf_addr": "",
                    "str_dosage": "Tablets",
                    "dt_closure_dt": "01-JAN-2026",
                }
            ]
        }
        self.assertEqual(self._search("dapagliflozin", payload), [])

    def test_serves_repeat_searches_from_the_cached_corpus(self):
        from sources import cdsco_india

        response = MagicMock()
        response.json.return_value = self.PAYLOAD
        response.raise_for_status.return_value = None
        with patch.object(cdsco_india, "_read_cache", side_effect=[None, self.PAYLOAD["aaData"]]):
            with patch.object(cdsco_india.requests, "get", return_value=response) as fetch:
                cdsco_india.run_cdsco_india_search("dapagliflozin")
                cdsco_india.run_cdsco_india_search("hepatitis b")

        self.assertEqual(fetch.call_count, 1)

    def test_returns_empty_when_the_endpoint_is_unavailable(self):
        from sources import cdsco_india

        with patch.object(cdsco_india.requests, "get", side_effect=requests.RequestException("boom")):
            self.assertEqual(cdsco_india.run_cdsco_india_search("dapagliflozin"), [])


class TGAConnectorTests(unittest.TestCase):
    def test_parses_artg_search_results(self):
        rows = _parse_artg_search_results(
            """
            <a href="/resources/artg/528326">
              ADMED PARACETAMOL SUSPENSION FOR CHILDREN 1-5 YEARS paracetamol 24 mg/mL strawberry flavour oral suspension bottle (528326)
            </a>
            """,
            "paracetamol",
            "https://www.tga.gov.au/resources/artg?keywords=paracetamol",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "TGA Australia")
        self.assertEqual(rows[0]["country"], "Australia")
        self.assertEqual(rows[0]["region"], "AU")
        self.assertEqual(rows[0]["registration_number"], "528326")
        self.assertEqual(rows[0]["strength"], "24 mg/mL")
        self.assertEqual(rows[0]["dosage_form"], "Suspension")
        self.assertEqual(rows[0]["product_url"], "https://www.tga.gov.au/resources/artg/528326")

    def test_parses_artg_detail_metadata(self):
        metadata = _parse_artg_detail(
            """
            <main>
              <div>Product name</div><div>Example paracetamol 500 mg tablet blister pack</div>
              <div>Sponsor</div><div>Example Pharma Pty Ltd</div>
              <div>Manufacturer</div><div>Example Manufacturing Pty Ltd</div>
              <div>Active ingredients</div><div>paracetamol</div>
              <div>Dosage form</div><div>Tablet</div>
              <div>Route of administration</div><div>Oral</div>
              <a href="/resources/product-information/example-pi.pdf">Product Information</a>
              <a href="/resources/consumer-medicine-information/example-cmi.pdf">Consumer Medicine Information</a>
            </main>
            """
        )

        self.assertEqual(metadata["product"], "Example paracetamol 500 mg tablet blister pack")
        self.assertEqual(metadata["company"], "Example Pharma Pty Ltd")
        self.assertEqual(metadata["manufacturer_name"], "Example Manufacturing Pty Ltd")
        self.assertEqual(metadata["substance"], "paracetamol")
        self.assertEqual(metadata["dosage_form"], "Tablet")
        self.assertEqual(metadata["route"], "Oral")
        self.assertEqual(metadata["smpc_url"], "https://www.tga.gov.au/resources/product-information/example-pi.pdf")
        self.assertEqual(metadata["pil_url"], "https://www.tga.gov.au/resources/consumer-medicine-information/example-cmi.pdf")

    def test_merges_tga_detail_without_losing_search_values(self):
        row = {
            "product": "Example paracetamol 500 mg tablet blister pack",
            "company": "",
            "strength": "",
        }
        merged = _merge_detail(row, {"company": "Example Pharma Pty Ltd"})
        self.assertEqual(merged["company"], "Example Pharma Pty Ltd")
        self.assertEqual(merged["strength"], "500 mg")

    def test_tga_fallback_returns_registry_handoff_for_paracetamol(self):
        rows = _fallback_rows("paracetamol", 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Australia")
        self.assertEqual(rows[0]["source"], "TGA Australia")
        self.assertEqual(rows[0]["connector_mode"], "manual_registry")

    def test_tga_fallback_returns_lookup_row_for_any_substance(self):
        rows = _fallback_rows("metformin", 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "Australia")
        self.assertEqual(rows[0]["source"], "TGA Australia")
        self.assertEqual(rows[0]["document_type"], "Official registry search handoff")
        self.assertEqual(rows[0]["connector_mode"], "manual_registry")
        self.assertIn("metformin", rows[0]["product"].lower())


class MedsafeConnectorTests(unittest.TestCase):
    SEARCH = """<p><strong>1 records found</strong></p><table id="productGrid" class="quickgrid"><thead><tr>
        <th>Product</th><th>Active ingredients</th><th>Sponsor</th><th>Status</th><th>Approval date</th>
        <th>Notification date</th></tr></thead><tbody><tr>
        <td><a href="ProductDetail?Product_id=16908">Renvela, Film coated tablet 800 mg (Prescription)</a></td>
        <td>Sevelamer</td><td>Sanofi-Aventis New Zealand Limited </td><td>Approval lapsed</td>
        <td>30/07/2015</td><td>1/03/2022</td></tr></tbody></table>"""
    DETAIL = """<table><tbody><tr><td></td><th><h2>Medsafe Product Detail</h2></th></tr>
        <tr><td class="right-justified">File ref: TT50-9571</td></tr></tbody></table>
        <table class="form-table"><tbody><tr><th>Trade Name</th><th>Dose Form</th><th>Strength</th><th>Identifier</th></tr>
        <tr><td>Renvela</td><td>Film coated tablet</td><td>800 mg</td><td> </td></tr>
        <tr><th>Sponsor</th><th>Application date</th><th>Regulatory status</th><th>Classification</th></tr>
        <tr><td>Sanofi-Aventis New Zealand Limited <br />P O Box 12851<br />AUCKLAND 1642</td><td>5/06/2014</td>
        <td>Approval lapsed<br>Approval date:30/07/2015<br>Notification date: 1/03/2022</td><td>Prescription</td></tr>
        </tbody></table><h4>Composition</h4><table><thead><tr><th>Component</th><th>Ingredient</th><th>Manufacturer</th></tr></thead>
        <tbody><tr><td>film coated tablet</td><td>Active</td><td>&nbsp;</td></tr>
        <tr><td>&nbsp;</td><td>Sevelamer carbonate 800mg</td><td>EuroAPI UK Limited <br />37 Hollands Road<br />Haverhill<br />UNITED KINGDOM</td></tr>
        <tr><td>&nbsp;</td><td>Excipient</td><td>&nbsp;</td></tr><tr><td>&nbsp;</td><td>Zinc stearate </td><td></td></tr></tbody></table>
        <h4>Production</h4><table><thead><tr><th>Manufacturing step</th><th>Manufacturer</th></tr></thead><tbody>
        <tr><td>Manufacture of Active Ingredient</td><td>EuroAPI UK Limited <br />Haverhill<br />UNITED KINGDOM</td></tr>
        <tr><td>Manufacture of Final Dose Form</td><td>Genzyme Ireland Ltd <br />Old Kilmeadon Road<br />Waterford<br />IRELAND</td></tr>
        <tr><td>Packing</td><td>Genzyme Ireland Ltd <br />Waterford<br />IRELAND</td></tr>
        <tr><td>NZ Site of Product Release</td><td>Pharmacy Retailing (NZ) Ltd<br />AUCKLAND 2022</td></tr></tbody></table>
        <h4>Packaging</h4><table><thead><tr><th>Package</th><th>Contents</th><th>Shelf life</th></tr></thead>
        <tbody><tr><td>Bottle, plastic, HDPE</td><td>180 dose units</td><td>36 months</td></tr></tbody></table>"""

    def test_a_product_takes_its_sponsor_composition_and_dose_form_site(self):
        from sources.medsafe import build_row, parse_product_detail, parse_search_results

        listings = parse_search_results(self.SEARCH)
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0]["url"], "https://www.medsafe.govt.nz/DbSearch/ProductDetail?Product_id=16908")
        row = build_row(listings[0], parse_product_detail(self.DETAIL))

        self.assertEqual(row["product"], "Renvela 800 mg")
        self.assertEqual(row["active_substance"], "Sevelamer carbonate")
        self.assertEqual(row["company"], "Sanofi-Aventis New Zealand Limited")
        self.assertEqual((row["status"], row["registration_date"]), ("Approval lapsed", "30/07/2015"))
        self.assertEqual(row["registration_number"], "TT50-9571")
        self.assertEqual(row["dosage_form"], "Film coated tablet")
        # The site that makes the tablet, not the release site or the API maker.
        self.assertEqual((row["manufacturer_name"], row["manufacturer_country"]), ("Genzyme Ireland Ltd", "Ireland"))
        self.assertEqual(row["api_manufacturer"], "EuroAPI UK Limited")
        self.assertEqual(row["pack_size"], "Bottle, plastic, HDPE, 180 dose units")
        self.assertTrue(row_relevant_to_substance(row, "sevelamer carbonate"))
        self.assertFalse(row_relevant_to_substance(row, "sevelamer hydrochloride"))

    def test_a_product_whose_page_was_not_read_keeps_the_listing_form_and_strength(self):
        from sources.medsafe import build_row, parse_search_results

        row = build_row(parse_search_results(self.SEARCH)[0], {})
        self.assertEqual((row["dosage_form"], row["strength"]), ("Film coated tablet", "800 mg"))
        self.assertEqual(row["product"], "Renvela 800 mg")

    def test_a_refused_product_page_is_asked_again(self):
        from sources import medsafe

        detail_calls = []

        def get(url, params=None, **_kwargs):
            if params:
                return MagicMock(text=self.SEARCH, raise_for_status=lambda: None)
            detail_calls.append(url)
            if len(detail_calls) == 1:
                raise requests.ConnectionError("busy")
            return MagicMock(text=self.DETAIL, raise_for_status=lambda: None)

        with patch("requests.Session.get", side_effect=get):
            rows = medsafe.run_medsafe_search("sevelamer")
        self.assertEqual(len(detail_calls), 2)
        self.assertEqual(rows[0]["registration_number"], "TT50-9571")

    def test_a_salt_the_register_does_not_file_is_searched_by_its_molecule(self):
        from sources import medsafe

        asked = []

        def get(url, params=None, **_kwargs):
            asked.append((params or {}).get("ingr"))
            html = self.SEARCH if params and params.get("ingr") == "sevelamer" else "<p>0 records found</p>"
            return MagicMock(text=html if params else self.DETAIL, raise_for_status=lambda: None)

        with patch("requests.Session.get", side_effect=get):
            rows = medsafe.run_medsafe_search("sevelamer carbonate")
        self.assertEqual(asked[:2], ["sevelamer carbonate", "sevelamer"])
        self.assertEqual([row["product"] for row in rows], ["Renvela 800 mg"])

    def test_an_empty_register_answer_is_not_a_handoff(self):
        from sources import medsafe

        with patch("requests.Session.get", return_value=MagicMock(text="<p>0 records found</p>", raise_for_status=lambda: None)):
            self.assertEqual(medsafe.run_medsafe_search("nonexistentmab"), [])

    def test_medsafe_fallback_returns_lookup_row_for_any_substance(self):
        rows = _medsafe_fallback_rows("metformin", 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["country"], "New Zealand")
        self.assertEqual(rows[0]["region"], "NZ")
        self.assertEqual(rows[0]["source"], "Medsafe New Zealand")
        self.assertEqual(rows[0]["document_type"], "Official registry search handoff")
        self.assertEqual(rows[0]["connector_mode"], "manual_registry")
        self.assertIn("metformin", rows[0]["product"].lower())


class ConnectorStatusTests(unittest.TestCase):
    def test_core_connector_status_contains_step_one_sources(self):
        rows = connector_status_rows()
        names = {row["name"] for row in rows}

        for source in [
            "FDA",
            "MHRA",
            "EMA",
            "Health Canada",
            "TGA Australia",
            "Medsafe New Zealand",
            "Hong Kong Drug Office",
            "GRLS Russia",
        ]:
            self.assertIn(source, names)

    def test_manual_or_browser_sources_are_explicitly_marked(self):
        rows = {row["name"]: row for row in connector_status_rows()}

        # Russia is read from GRLS's published register, which reports its own state.
        self.assertEqual(rows["GRLS Russia"]["mode"], "open_register")
        self.assertIn(rows["GRLS Russia"]["status"], {"ready", "not_downloaded_yet", "download_failed"})
        self.assertEqual(rows["TGA Australia"]["status"], "partial")
        self.assertEqual(rows["Medsafe New Zealand"]["status"], "partial")

    def test_nmpa_china_is_served_from_its_published_register(self):
        rows = {row["name"]: row for row in connector_status_rows()}

        # Like Russia, China reports its register's own state: "ready" only once
        # the file has been downloaded, which a fresh checkout has not done.
        self.assertEqual(rows["NMPA China"]["mode"], "open_register")
        self.assertIn(rows["NMPA China"]["status"], {"ready", "not_downloaded_yet", "download_failed"})

    def test_nmpa_china_returns_registry_handoff_not_fake_product_rows(self):
        rows = run_nmpa_china_search("metformin")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "NMPA China")
        self.assertEqual(rows[0]["country"], "China")
        self.assertEqual(rows[0]["connector_mode"], "manual_registry")
        self.assertIn("parser", rows[0]["status"].lower())

    def test_connector_health_records_last_source_result(self):
        record_source_health("NMPA China", "timeout", 0, 10.2, "timeout")
        rows = {row["name"]: row for row in connector_health_rows()}

        self.assertEqual(rows["NMPA China"]["last_status"], "timeout")
        self.assertEqual(rows["NMPA China"]["last_records"], 0)
        self.assertEqual(rows["NMPA China"]["last_error"], "timeout")


class EnglishNormalizerTests(unittest.TestCase):
    def test_translates_known_regulatory_values_to_english(self):
        self.assertEqual(english_text("Д"), "Active")
        self.assertEqual(english_text("Бетмига"), "Betmiga")
        self.assertEqual(english_text("Астеллас Фарма Юроп Б.В."), "Astellas Pharma Europe B.V.")
        self.assertEqual(english_text("Нидерланды"), "Netherlands")

    def test_normalizes_row_fields_for_display_and_export(self):
        row = english_row(
            {
                "product": "Бетмига",
                "company": "Астеллас Фарма Юроп Б.В.",
                "manufacturer_country": "Нидерланды",
                "source_substance": "Мирабегрон",
                "dosage_form": "таблетки пролонгированного высвобождения",
            }
        )

        self.assertEqual(row["product"], "Betmiga")
        self.assertEqual(row["company"], "Astellas Pharma Europe B.V.")
        self.assertEqual(row["manufacturer_country"], "Netherlands")
        self.assertEqual(row["source_substance"], "Mirabegron")
        self.assertEqual(row["dosage_form"], "Prolonged-release tablet")

    def test_marks_non_latin_values_that_need_ai_translation(self):
        self.assertEqual(
            english_text("药品"),
            "Local-language registry value: 药品",
        )


class AIEnrichmentTests(unittest.TestCase):
    def test_marks_complete_connector_row_as_high_confidence(self):
        metadata = enrichment_metadata(
            {
                "substance": "Example",
                "product": "Example 10 mg tablets",
                "company": "Example Holder Ltd",
                "country": "United Kingdom",
                "source": "MHRA",
                "registration_number": "PL 12345/0001",
                "registration_date": "1 Jan 2024",
                "manufacturer_name": "Example Manufacturer Ltd",
                "manufacturer_country": "Germany",
                "pack_size": "28 tablets",
                "product_url": "https://example.test/product",
                "smpc_url": "https://example.test/spc.pdf",
                "pil_url": "https://example.test/pil.pdf",
                "assessment_report_url": "https://example.test/par.pdf",
            }
        )

        self.assertEqual(metadata["data_confidence"], "High")
        self.assertEqual(metadata["missing_fields"], "None")

    def test_marks_manual_registry_rows_for_review(self):
        metadata = enrichment_metadata(
            {
                "connector_mode": "manual_registry",
                "product": "example official registry search",
                "country": "Exampleland",
                "source": "Example Registry",
            }
        )

        self.assertEqual(metadata["data_confidence"], "Needs manual review")
        self.assertIn("Product record not extracted", metadata["missing_fields"])

    def test_missing_fields_are_generic(self):
        fields = missing_enrichment_fields(
            {
                "product": "Example 5 mg tablets",
                "country": "Canada",
                "source": "Health Canada",
            }
        )

        self.assertIn("MA Holder", fields)
        self.assertIn("Manufacturer Name", fields)
        self.assertIn("SmPC URL", fields)

    def test_ai_client_is_safe_when_api_key_missing(self):
        original_key = os.environ.pop("OPENAI_API_KEY", None)
        try:
            self.assertFalse(ai_status()["api_key_configured"])
            self.assertEqual(ai_extract_regulatory_fields({"product": "Example"}, "Example text"), {})
        finally:
            if original_key is not None:
                os.environ["OPENAI_API_KEY"] = original_key

    def test_ai_enriched_rows_are_labelled(self):
        metadata = enrichment_metadata(
            {
                "substance": "Example",
                "product": "Example 10 mg tablets",
                "company": "Example Holder Ltd",
                "country": "United Kingdom",
                "source": "MHRA",
                "product_url": "https://example.test/product",
                "registration_number": "PL 12345/0001",
                "manufacturer_name": "Example Manufacturer Ltd",
                "manufacturer_country": "Germany",
                "smpc_url": "https://example.test/spc.pdf",
                "ai_enriched": "true",
                "ai_confidence": "High",
            }
        )

        self.assertEqual(metadata["data_confidence"], "High")
        self.assertIn("AI extracted", metadata["enrichment_status"])


class SearchJobTests(unittest.TestCase):
    def test_background_job_orders_fast_sources_before_slow_sources(self):
        # Health Canada was the quick source here until it was measured at
        # ~19s and moved to SLOW_SOURCES; Spain CIMA answers in ~5s.
        sources = order_sources_for_job(["GRLS Russia", "FDA", "Spain CIMA"])
        self.assertEqual(sources[:2], ["FDA", "Spain CIMA"])
        self.assertIn("GRLS Russia", SLOW_SOURCES)

    def test_fast_background_job_skips_heavy_sources(self):
        self.assertTrue(source_skipped_in_mode("GRLS Russia", "fast"))
        self.assertFalse(source_skipped_in_mode("FDA", "fast"))

    def test_full_background_job_keeps_heavy_sources_queued(self):
        self.assertFalse(source_skipped_in_mode("GRLS Russia", "full"))

    def test_dedupes_job_rows_by_source_product_country_registration_and_url(self):
        rows = _dedupe_rows(
            [
                {
                    "source": "FDA",
                    "product": "Example",
                    "country": "United States",
                    "registration_number": "1",
                    "url": "https://example.test",
                },
                {
                    "source": "FDA",
                    "product": "Example",
                    "country": "United States",
                    "registration_number": "1",
                    "url": "https://example.test",
                },
            ]
        )

        self.assertEqual(len(rows), 1)


class SearchJobPersistenceTests(unittest.TestCase):
    def test_persists_search_job_progress_and_results(self):
        job_id = "test_job_persist"
        save_search_job(
            {
                "job_id": job_id,
                "substance": "metformin",
                "sources": ["FDA"],
                "mode": "fast",
                "status": "done",
                "created_at": "2026-01-01T00:00:00",
                "started_at": "2026-01-01T00:00:01",
                "finished_at": "2026-01-01T00:00:02",
            }
        )
        save_search_job_progress(
            job_id,
            {
                "source": "FDA",
                "status": "done",
                "records": 1,
                "error": "",
                "started_at": "2026-01-01T00:00:01",
                "finished_at": "2026-01-01T00:00:02",
            },
        )
        save_search_job_results(
            job_id,
            [
                {
                    "substance": "metformin",
                    "product": "Metformin 500 mg tablets",
                    "country": "United States",
                    "source": "FDA",
                    "url": "https://example.test/metformin",
                }
            ],
        )

        job = get_persisted_search_job(job_id)
        results = get_persisted_search_job_results(job_id)

        self.assertEqual(job["substance"], "metformin")
        self.assertEqual(job["record_count"], 1)
        self.assertEqual(job["progress"][0]["source"], "FDA")
        self.assertEqual(results[0]["product"], "Metformin 500 mg tablets")


class TherapeuticCategoryTests(unittest.TestCase):
    def test_shortens_long_indication_to_generic_category(self):
        category = short_therapeutic_category(
            "Quetiapine is indicated for the treatment of schizophrenia and bipolar disorder in adults.",
            "quetiapine",
            "",
        )

        self.assertEqual(category, "Antipsychotic")

    def test_never_guesses_a_category_from_the_molecule_name(self):
        # A category no regulator published for this product is not shown.
        self.assertEqual(short_therapeutic_category("", "mirabegron", ""), "")

    def test_uses_atc_category_when_available(self):
        self.assertEqual(short_therapeutic_category("", "", "N02BE01"), "Analgesic")


class HarvestVocabularyTests(unittest.TestCase):
    def test_reduces_a_registry_substance_string_to_its_molecule(self):
        self.assertEqual(
            normalize_molecule(
                "Dapagliflozin Propanediol Monohydrate IP eq. To Dapagliflozin 10.0000 Milligram (Mg)"
            ),
            "dapagliflozin",
        )

    def test_strips_a_trailing_salt(self):
        self.assertEqual(normalize_molecule("DICLOFENAC SODIUM"), "diclofenac")
        self.assertEqual(normalize_molecule("ATORVASTATIN CALCIUM"), "atorvastatin")

    def test_keeps_a_salt_that_is_itself_the_molecule(self):
        self.assertEqual(normalize_molecule("SODIUM CHLORIDE"), "sodium chloride")
        self.assertEqual(normalize_molecule("MAGNESIUM SULFATE"), "magnesium sulfate")

    def test_rejects_containers_and_excipients(self):
        for value in ("Vial", "Water For Injection", "Tablets", "Polysorbate 80"):
            self.assertEqual(normalize_molecule(value), "", value)

    def test_splits_a_fixed_dose_combination_into_its_molecules(self):
        self.assertEqual(
            split_combination("Amoxycillin Trihydrate Ip Eq. To Amoxycillin+Vonoprazan Fumarate"),
            ["amoxycillin", "vonoprazan"],
        )

    def test_ranks_multi_registry_molecules_before_high_count_singletons(self):
        accumulator = _Accumulator()
        # A US-only monograph ingredient with an enormous label count.
        accumulator.add("ZINC OXIDE", "openfda", evidence=4148)
        # A prescription molecule attested by two registries.
        accumulator.add("METFORMIN", "openfda", evidence=200)
        accumulator.add("Metformin Hydrochloride IP", "cdsco")

        self.assertEqual(
            [entry["molecule"] for entry in accumulator.entries()],
            ["metformin", "zinc oxide"],
        )


class _StubConnector:
    """Stands in for a registry connector in harvest tests."""

    def __init__(self, name, rows=None, error=None):
        self.metadata = SourceMetadata(
            name=name, region="EU", countries=("Germany",), rate_limit_per_minute=6000
        )
        self._rows = rows or []
        self._error = error
        self.calls = []

    def search(self, substance):
        self.calls.append(substance)
        if self._error:
            raise self._error
        return [dict(row) for row in self._rows]


class HarvestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.persisted = []
        persist = patch(
            "services.harvest.save_product_detail",
            side_effect=lambda record: self.persisted.append(record) or record,
        )
        # Progress is a database write; the runner's logic is what is under test.
        progress = patch("services.harvest.record_progress")
        tables = patch("services.harvest.initialize_harvest_tables")
        self.record_progress = progress.start()
        persist.start()
        tables.start()
        self.addCleanup(patch.stopall)

    def test_harvests_every_molecule_and_stores_the_rows(self):
        connector = _StubConnector("Stub", rows=[{"product": "Glucophage", "source": "Stub"}])

        with patch("services.harvest.CONNECTORS", [connector]), patch(
            "services.harvest.completed_pairs", return_value=set()
        ):
            result = run_harvest(molecules=["metformin", "atorvastatin"])

        self.assertEqual(connector.calls, ["metformin", "atorvastatin"])
        self.assertEqual(result.rows, 2)
        # A connector row without a substance is filed under the molecule that
        # produced it, or the harvested row could never be found again.
        self.assertEqual([row["substance"] for row in self.persisted], ["metformin", "atorvastatin"])

    def test_skips_pairs_already_harvested(self):
        connector = _StubConnector("Stub", rows=[{"product": "Glucophage"}])

        with patch("services.harvest.CONNECTORS", [connector]), patch(
            "services.harvest.completed_pairs", return_value={("Stub", "metformin")}
        ):
            result = run_harvest(molecules=["metformin", "atorvastatin"])

        self.assertEqual(connector.calls, ["atorvastatin"])
        self.assertEqual(result.outcomes["Stub"].skipped, 1)

    def test_one_failing_registry_does_not_stop_the_others(self):
        broken = _StubConnector("Broken", error=requests.RequestException("down"))
        working = _StubConnector("Working", rows=[{"product": "Glucophage"}])

        with patch("services.harvest.CONNECTORS", [broken, working]), patch(
            "services.harvest.completed_pairs", return_value=set()
        ):
            result = run_harvest(molecules=["metformin", "atorvastatin"])

        self.assertEqual(result.outcomes["Broken"].failures, 2)
        self.assertEqual(result.outcomes["Broken"].rows, 0)
        self.assertEqual(result.outcomes["Working"].rows, 2)

    def test_stops_a_registry_that_has_gone_down(self):
        broken = _StubConnector("Broken", error=requests.RequestException("down"))
        molecules = [f"molecule-{index}" for index in range(CONSECUTIVE_FAILURE_LIMIT + 5)]

        with patch("services.harvest.CONNECTORS", [broken]), patch(
            "services.harvest.completed_pairs", return_value=set()
        ):
            result = run_harvest(molecules=molecules)

        outcome = result.outcomes["Broken"]
        self.assertTrue(outcome.stopped_early)
        self.assertEqual(len(broken.calls), CONSECUTIVE_FAILURE_LIMIT)
        # The molecules never attempted stay unrecorded, so the next run retries
        # them rather than treating a dead registry as covered.
        recorded = {call.args[1] for call in self.record_progress.call_args_list}
        self.assertEqual(recorded, set(molecules[:CONSECUTIVE_FAILURE_LIMIT]))


class FieldCompletionTests(unittest.TestCase):
    def _group(self, *rows):
        return {row_id: changes for row_id, changes in _completions_for_group(list(rows))}

    def test_lends_a_molecule_level_code_to_a_registry_that_omits_it(self):
        ema = {"id": 1, "source": "EMA", "substance": "metformin", "atc_code": "A10BA02"}
        cdsco = {"id": 2, "source": "CDSCO India", "substance": "Metformin Hydrochloride IP"}

        changes = self._group(ema, cdsco)

        self.assertEqual(changes[2]["atc_code"], "A10BA02")
        self.assertEqual(changes[2]["completion_source"], "EMA")
        # The lender keeps its own code and gains nothing from itself.
        self.assertNotIn("atc_code", changes.get(1, {}))

    def test_groups_a_molecule_across_languages_and_registers(self):
        # France files IBUPROFÈNE and the Latin INN keeps a final "e" English
        # drops, so grouping on the plain name leaves the French rows with
        # nobody to lend them a document.
        for french, english in (
            ("IBUPROFÈNE", "ibuprofen"),
            ("AMOXICILLINE, AMOXICILLINE TRIHYDRATE", "amoxicillin"),
            ("ATORVASTATINE, ATORVASTATINE CALCIUM", "ATORVASTATIN CALCIUM"),
            ("CHLORHYDRATE DE METFORMINE, METFORMINE", "metformin"),
            ("ÉSOMÉPRAZOLE, ÉSOMÉPRAZOLE MAGNÉSIQUE", "esomeprazole"),
        ):
            self.assertEqual(
                molecule_group_key(french), molecule_group_key(english), french
            )

    def test_keys_a_combination_on_all_its_molecules_in_any_order(self):
        # Keyed on its first molecule, Janumet sat in metformin's group and
        # lent or borrowed metformin's ATC code.
        self.assertEqual(molecule_group_key("sitagliptin;metformin hydrochloride"), "metformin+sitagliptin")
        self.assertEqual(
            molecule_group_key("METFORMIN HYDROCHLORIDE, SITAGLIPTIN PHOSPHATE MONOHYDRATE"),
            molecule_group_key("sitagliptin / metformin"),
        )
        self.assertNotEqual(molecule_group_key("sitagliptin;metformin"), molecule_group_key("metformin"))

    def test_a_molecule_listed_beside_its_own_salt_is_still_one_molecule(self):
        self.assertFalse(is_combination("ATORVASTATINE, ATORVASTATINE CALCIUM"))
        self.assertFalse(is_combination("\u00c9SOM\u00c9PRAZOLE, \u00c9SOM\u00c9PRAZOLE MAGN\u00c9SIQUE"))
        self.assertTrue(is_combination("VALSARTAN, AMLODIPINE BESYLATE"))

    def test_a_searched_row_is_keyed_on_what_the_registry_says_it_contains(self):
        # A search stores the typed term as the substance.
        janumet = {"substance": "metformin", "source_substance": "SITAGLIPTIN; METFORMIN HYDROCHLORIDE"}
        italian = {"substance": "metformin", "source_substance": "METFORMINA CLORIDRATO"}
        self.assertEqual(row_molecule_key(janumet), "metformin+sitagliptin")
        # A single molecule keeps the searched name, which is what groups it
        # across the registries' languages.
        self.assertEqual(row_molecule_key(italian), "metformin")

    def test_a_combination_and_its_molecule_lend_each_other_nothing(self):
        rows = [
            {"id": 1, "source": "EMA", "substance": "metformin", "atc_code": "A10BA02"},
            {"id": 2, "source": "EMA", "substance": "metformin", "atc_code": "A10BA02"},
            {"id": 3, "source": "MHRA", "substance": "metformin",
             "source_substance": "SITAGLIPTIN, METFORMIN HYDROCHLORIDE", "atc_code": ""},
            {"id": 4, "source": "EMA", "substance": "sitagliptin;metformin", "atc_code": "A10BD07"},
            {"id": 5, "source": "Spain CIMA", "substance": "metformin", "atc_code": "",
             "product": "EUCREAS 50 MG/850 MG COMPRIMIDOS"},
        ]
        groups = {}
        for row in rows:
            groups.setdefault(row_molecule_key(row), []).append(row)
        changes = {}
        for group in groups.values():
            changes.update({row_id: change for row_id, change in _completions_for_group(group)})

        self.assertEqual(changes[3]["atc_code"], "A10BD07")
        # A combination the registry did not state is recognised by its pair
        # of strengths and not given the molecule's code.
        self.assertNotIn("atc_code", changes.get(5, {}))

    def test_a_pair_of_strengths_names_a_combination(self):
        self.assertTrue(names_two_strengths("FENZIL 10 MG/160 MG COMPRIMIDOS"))
        self.assertTrue(names_two_strengths("Co-codamol 15mg/500mg Tablets"))
        self.assertFalse(names_two_strengths("Calpol 120mg/5ml Infant Oral Suspension"))
        self.assertFalse(names_two_strengths("Atorvastatin 10 mg/20 mg/40 mg Tablets"))
        self.assertFalse(names_two_strengths("Metformin 500 mg tablets"))

    def test_lends_stored_documents_to_a_live_row(self):
        # A live search builds rows from the connectors, so they carry no
        # completion at all even when the database holds the molecule.
        live_row = {"source": "CDSCO India", "substance": "QUÉTIAPINE"}
        stored = [
            {
                "substance_key": "quetiapin",
                "source": "MHRA",
                "product": "Seroquel 25mg",
                "smpc_url": "https://mhra/seroquel-smpc",
                "pil_url": "",
                "assessment_report_url": "",
            }
        ]

        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = stored
        with patch("services.field_completion.get_connection") as get_connection:
            get_connection.return_value.__enter__.return_value = connection
            rows = attach_reference_documents([live_row])

        self.assertEqual(rows[0]["reference_smpc_url"], "https://mhra/seroquel-smpc")
        self.assertEqual(rows[0]["reference_source"], "MHRA")
        # Nothing is written into the row's own column, in memory or otherwise.
        self.assertNotIn("smpc_url", rows[0])

    def test_does_not_overwrite_a_live_rows_own_document(self):
        live_row = {"source": "MHRA", "substance": "quetiapine", "smpc_url": "https://mhra/own"}

        with patch("services.field_completion.get_connection") as get_connection:
            rows = attach_reference_documents([live_row])

        get_connection.assert_not_called()
        self.assertEqual(rows[0]["smpc_url"], "https://mhra/own")
        self.assertNotIn("reference_smpc_url", rows[0])

    def test_keeps_different_molecules_apart(self):
        self.assertNotEqual(molecule_group_key("metformin"), molecule_group_key("metoprolol"))
        self.assertNotEqual(molecule_group_key("ibuprofen"), molecule_group_key("naproxen"))

    def test_derives_the_category_instead_of_lending_another_registrys_wording(self):
        # Romania states the ATC class in Romanian and would win a majority
        # vote; lending that text would put it on every other country's rows.
        romania = {
            "id": 1,
            "source": "ANMDMR Romania",
            "substance": "metformin",
            "atc_code": "A10BA02",
            "therapeutic_category": "Medicamente De Scadere A Glucozei Din Sang, Excl. Insuline",
        }
        cdsco = {"id": 2, "source": "CDSCO India", "substance": "metformin"}

        changes = self._group(romania, cdsco)

        self.assertEqual(changes[2]["therapeutic_category"], "Antidiabetic")
        # And the Romanian regulator's own text is left on the Romanian row.
        self.assertNotIn(
            "Medicamente",
            " ".join(value for row in changes.values() for value in row.values()),
        )

    def test_takes_the_code_most_registries_agree_on(self):
        rows = [
            {"id": 1, "source": "EMA", "substance": "metformin", "atc_code": "A10BA02"},
            {"id": 2, "source": "MHRA", "substance": "metformin", "atc_code": "A10BA02"},
            # A single mis-parsed value must not be lent to the whole molecule.
            {"id": 3, "source": "Thai FDA", "substance": "metformin", "atc_code": "XXXX"},
            {"id": 4, "source": "CDSCO India", "substance": "metformin"},
        ]

        self.assertEqual(self._group(*rows)[4]["atc_code"], "A10BA02")

    def test_never_writes_another_countrys_document_into_the_products_own_column(self):
        ema = {
            "id": 1,
            "source": "EMA",
            "substance": "metformin",
            "product": "Glucophage",
            "smpc_url": "https://ema.europa.eu/glucophage-smpc",
        }
        cdsco = {"id": 2, "source": "CDSCO India", "substance": "metformin"}

        changes = self._group(ema, cdsco)[2]

        self.assertNotIn("smpc_url", changes)
        self.assertEqual(changes["reference_smpc_url"], "https://ema.europa.eu/glucophage-smpc")
        self.assertEqual(changes["reference_source"], "EMA")
        self.assertEqual(changes["reference_product"], "Glucophage")

    def test_never_lends_an_assessment_report(self):
        # An assessment report evaluates one licence application -- it is
        # headed with that procedure and licence number -- so it describes a
        # decision about another country's authorisation, not this medicine.
        ema = {
            "id": 1,
            "source": "EMA",
            "substance": "metformin",
            "product": "Glucophage",
            "smpc_url": "https://ema/smpc",
            "assessment_report_url": "https://ema/glucophage-assessment",
        }
        cdsco = {"id": 2, "source": "CDSCO India", "substance": "metformin"}

        changes = self._group(ema, cdsco)[2]

        self.assertEqual(changes["reference_smpc_url"], "https://ema/smpc")
        self.assertNotIn("reference_assessment_report_url", changes)
        self.assertNotIn("assessment_report_url", changes)

    def test_leaves_a_row_that_has_its_own_document_alone(self):
        ema = {"id": 1, "source": "EMA", "substance": "metformin", "smpc_url": "https://ema/one"}
        mhra = {"id": 2, "source": "MHRA", "substance": "metformin", "smpc_url": "https://mhra/two"}

        changes = self._group(ema, mhra)

        self.assertNotIn("reference_smpc_url", changes.get(2, {}))

    def test_lends_from_the_same_dosage_form(self):
        # Triamcinolone is a nasal spray in one country and an injectable
        # suspension in another; the injection's assessment report describes a
        # different medicine and must not be lent to the spray.
        injection = {
            "id": 1,
            "source": "MHRA",
            "substance": "triamcinolone",
            "product": "TRIAMCINOLONE HEXACETONIDE SUSPENSION FOR INJECTION",
            "dosage_form": "Suspension for injection",
            "smpc_url": "https://mhra/injection-smpc",
            "pil_url": "https://mhra/injection-pil",
            "assessment_report_url": "https://mhra/injection-par",
        }
        spray = {
            "id": 2,
            "source": "MHRA",
            "substance": "triamcinolone",
            "product": "NASACORT ALLERGY NASAL SPRAY",
            "dosage_form": "Nasal spray",
            "smpc_url": "https://mhra/spray-smpc",
        }
        us_spray = {
            "id": 3,
            "source": "FDA",
            "substance": "triamcinolone",
            "product": "Good Sense Nasal Allergy",
            "dosage_form": "Spray",
        }

        changes = self._group(injection, spray, us_spray)[3]

        self.assertEqual(changes["reference_smpc_url"], "https://mhra/spray-smpc")
        self.assertEqual(changes["reference_product"], "NASACORT ALLERGY NASAL SPRAY")

    def test_lends_from_the_same_salt(self):
        # Kenalog is triamcinolone acetonide and Aristospan is hexacetonide:
        # same molecule, same injectable suspension, different medicines. The
        # FDA states the salt in substance_name while the product is a brand.
        hexacetonide = {
            "id": 1,
            "source": "MHRA",
            "substance": "Triamcinolone",
            "product": "TRIAMCINOLONE HEXACETONIDE 20MG/ML SUSPENSION FOR INJECTION",
            "dosage_form": "Suspension for injection",
            "smpc_url": "https://mhra/hexacetonide-smpc",
            "pil_url": "https://mhra/hexacetonide-pil",
        }
        acetonide = {
            "id": 2,
            "source": "MHRA",
            "substance": "Triamcinolone",
            "product": "KENALOG INTRA-ARTICULAR TRIAMCINOLONE ACETONIDE",
            "dosage_form": "Suspension for injection",
            "smpc_url": "https://mhra/acetonide-smpc",
        }
        kenalog = {
            "id": 3,
            "source": "FDA",
            "substance": "triamcinolone",
            "source_substance": "TRIAMCINOLONE ACETONIDE",
            "product": "KENALOG-10",
            "dosage_form": "Suspension",
        }

        changes = self._group(hexacetonide, acetonide, kenalog)[3]

        self.assertEqual(changes["reference_smpc_url"], "https://mhra/acetonide-smpc")

    def test_prefers_the_regulator_that_publishes_a_full_document_set(self):
        stray = {"id": 1, "source": "Thai FDA", "substance": "metformin", "pil_url": "https://thai/pil"}
        ema = {
            "id": 2,
            "source": "EMA",
            "substance": "metformin",
            "smpc_url": "https://ema/smpc",
            "pil_url": "https://ema/pil",
        }
        cdsco = {"id": 3, "source": "CDSCO India", "substance": "metformin"}

        changes = self._group(stray, ema, cdsco)[3]

        self.assertEqual(changes["reference_source"], "EMA")
        self.assertEqual(changes["reference_pil_url"], "https://ema/pil")


class ExportSpeedTests(unittest.TestCase):
    """Saving a large search's rows is what made its export take an hour."""

    def test_the_schema_is_checked_once_per_database_not_once_per_call(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ), patch.object(repository, "_create_and_migrate_schema",
                        wraps=repository._create_and_migrate_schema) as schema:
            for _ in range(5):
                repository.initialize_database()
            self.assertEqual(schema.call_count, 1)

    def test_a_replaced_database_file_is_checked_again(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.initialize_database()
            Path(repository.DB_PATH).unlink()
            repository.initialize_database()
            with repository.get_connection() as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("product_details", tables)

    def test_many_rows_are_saved_together_and_returned_in_order(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            records = [
                {"substance": "example", "product": f"Example {index} mg", "source": "Example Regulator",
                 "registration_number": f"EX-{index}", "product_url": f"https://regulator.test/{index}"}
                for index in range(30)
            ]
            stored = repository.save_product_details(records)
            again = repository.save_product_details(records[:5])
            with repository.get_connection() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM product_details WHERE source = 'Example Regulator'"
                ).fetchone()[0]
        self.assertEqual([row["product"] for row in stored], [record["product"] for record in records])
        self.assertEqual(count, 30)          # saving the same rows again updates them
        self.assertEqual(len(again), 5)

    def test_the_duplicate_lookup_uses_an_index(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.initialize_database()
            with repository.get_connection() as conn:
                plan = " ".join(
                    str(step[-1]) for step in conn.execute(
                        "EXPLAIN QUERY PLAN SELECT id FROM product_details "
                        "WHERE COALESCE(source,'') = COALESCE(?,'') AND COALESCE(product,'') = COALESCE(?,'') "
                        "AND COALESCE(country,'') = COALESCE(?,'')",
                        ("FDA", "X", "United States"),
                    )
                )
        self.assertIn("idx_product_details_identity", plan)

    def test_an_exported_row_carries_the_provenance_its_save_recorded(self):
        from services import search_pipeline

        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ), patch.object(search_pipeline, "enrich_deep_results", lambda rows: rows), \
             patch.object(search_pipeline, "attach_ai_enrichment_metadata", lambda rows: rows):
            rows = search_pipeline.enriched_cached_results([{
                "substance": "example", "product": "Example 10 mg", "source": "Example Regulator",
                "registration_number": "EX-1", "product_url": "https://regulator.test/products/1",
            }])
        # Before, the export wrote this row without the evidence its save stamped,
        # and every row read "Evidence URL: Not available".
        self.assertEqual(rows[0]["evidence_url"], "https://regulator.test/products/1")
        self.assertEqual(rows[0]["verification_status"], "VERIFIED_REGULATOR_RECORD")


def _ndc_record(**overrides):
    record = {
        "product_ndc": "33342-409",
        "product_type": "HUMAN PRESCRIPTION DRUG",
        "finished": True,
        "brand_name": "Metformin Hydrochloride",
        "active_ingredients": [{"name": "METFORMIN HYDROCHLORIDE", "strength": "500 mg/1"}],
        "dosage_form": "TABLET, FILM COATED",
        "route": ["ORAL"],
        "labeler_name": "Macleods Pharmaceuticals Limited",
        "marketing_category": "ANDA",
        "application_number": "ANDA211559",
        "marketing_start_date": "20260406",
        "listing_expiration_date": "20271231",
        "pharm_class": ["Biguanide [EPC]", "Biguanides [CS]"],
        "packaging": [{"description": "30 TABLET, FILM COATED in 1 BOTTLE (33342-409-07)"}],
        "openfda": {"spl_set_id": ["7d575ca8-9c1f-469d-a341-34daf2f036dd"]},
    }
    record.update(overrides)
    return record


class FdaNdcRegisterTests(unittest.TestCase):
    """The FDA's NDC directory, streamed from openFDA's download and searched locally."""

    def setUp(self):
        from sources import open_registers

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(open_registers, "INDEX_DIR", Path(self.directory.name) / "idx")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _zip(self, records):
        import json as _json
        import zipfile as _zipfile

        path = Path(self.directory.name) / "ndc.json.zip"
        document = {"meta": {"results": {"total": len(records)}}, "results": records}
        with _zipfile.ZipFile(path, "w") as archive:
            archive.writestr("drug-ndc-0001-of-0001.json", _json.dumps(document, indent=2))
        return path

    def test_the_stream_yields_every_record_however_the_reads_fall(self):
        from sources.fda_ndc import stream_results

        records = [_ndc_record(product_ndc=f"0000-{index:03d}") for index in range(40)]
        path = self._zip(records)
        for chunk_size in (7, 1 << 20):
            streamed = [record["product_ndc"] for record in stream_results(path, chunk_size=chunk_size)]
            self.assertEqual(streamed, [record["product_ndc"] for record in records], chunk_size)

    def test_a_listing_becomes_a_row_with_its_approval_and_label(self):
        from sources.fda_ndc import FDA_NDC
        from sources.open_registers import build_index, search_register

        build_index(FDA_NDC, self._zip([_ndc_record()]))
        (row,) = search_register(FDA_NDC, "metformin")
        self.assertEqual(row["product"], "Metformin Hydrochloride 500 mg (NDC 33342-409)")
        self.assertEqual(row["authorisation_scope"], "ANDA (generic)")
        self.assertEqual(row["registration_number"], "ANDA211559")
        self.assertEqual(row["registration_date"], "2026-04-06")
        self.assertEqual(row["strength"], "500 mg")
        self.assertEqual(row["therapeutic_category"], "Biguanide")
        self.assertEqual(row["status"], "Marketed")
        self.assertEqual(row["pack_size"], "30 TABLET, FILM COATED in 1 BOTTLE")
        self.assertEqual(
            row["smpc_url"],
            "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=7d575ca8-9c1f-469d-a341-34daf2f036dd",
        )

    def test_a_repackager_under_the_same_application_stays_its_own_row(self):
        from sources.fda_ndc import FDA_NDC
        from sources.open_registers import build_index, search_register

        repackaged = _ndc_record(product_ndc="50090-6123", labeler_name="A-S Medication Solutions")
        build_index(FDA_NDC, self._zip([_ndc_record(), repackaged]))
        rows = search_register(FDA_NDC, "metformin")
        self.assertEqual(len({row["product"] for row in rows}), 2)

    def test_products_that_are_not_finished_human_medicines_are_left_out(self):
        from sources.fda_ndc import FDA_NDC
        from sources.open_registers import build_index, search_register

        build_index(FDA_NDC, self._zip([
            _ndc_record(),
            _ndc_record(product_ndc="1-1", product_type="BULK INGREDIENT"),
            _ndc_record(product_ndc="1-2", finished=False),
            _ndc_record(product_ndc="1-3", marketing_category="UNAPPROVED HOMEOPATHIC"),
        ]))
        self.assertEqual(len(search_register(FDA_NDC, "metformin")), 1)

    def test_a_search_past_the_bound_says_how_many_there_are(self):
        import dataclasses
        from sources.fda_ndc import FDA_NDC
        from sources.open_registers import build_index, search_register

        small = dataclasses.replace(FDA_NDC, max_results=2)
        build_index(small, self._zip([_ndc_record(product_ndc=f"9-{index}") for index in range(5)]))
        rows = search_register(small, "metformin")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["available_total"] for row in rows}, {5})


class HealthCanadaDpdRegisterTests(unittest.TestCase):
    """Health Canada's DPD, fetched dataset by dataset and joined locally."""

    def setUp(self):
        from sources import open_registers

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(open_registers, "INDEX_DIR", Path(self.directory.name) / "idx")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _datasets(self):
        import json as _json

        workdir = Path(self.directory.name) / "dpd"
        workdir.mkdir()
        data = {
            "drugproduct": [
                {"drug_code": 98443, "class_name": "Human", "drug_identification_number": "02494418",
                 "brand_name": "AG-METFORMIN", "company_name": "ANGITA PHARMA INC."},
                {"drug_code": 1, "class_name": "Veterinary", "drug_identification_number": "1",
                 "brand_name": "METFORMIN FOR CATS", "company_name": "VET CO"},
                {"drug_code": 2, "class_name": "Human", "drug_identification_number": "2",
                 "brand_name": "METFORMINUM 30CH", "company_name": "BOIRON"},
            ],
            "activeingredient": [
                {"drug_code": 98443, "ingredient_name": "METFORMIN HYDROCHLORIDE", "strength": "500", "strength_unit": "MG"},
                {"drug_code": 1, "ingredient_name": "METFORMIN HYDROCHLORIDE", "strength": "5", "strength_unit": "MG"},
                {"drug_code": 2, "ingredient_name": "METFORMIN", "strength": "30", "strength_unit": "CH"},
            ],
            "form": [{"drug_code": 98443, "pharmaceutical_form_name": "Tablet"}],
            "route": [{"drug_code": 98443, "route_of_administration_name": "Oral"}],
            "packaging": [{"drug_code": 98443, "package_size": "100", "package_size_unit": "TAB", "package_type": "BOTTLE"}],
            "status": [{"drug_code": 98443, "status": "Marketed", "original_market_date": "2023-01-04"}],
            "schedule": [
                {"drug_code": 98443, "schedule_name": "PRESCRIPTION"},
                {"drug_code": 2, "schedule_name": "HOMEOPATHIC"},
            ],
            "therapeuticclass": [{"drug_code": 98443, "tc_atc_number": "A10BA02", "tc_atc": "METFORMIN"}],
        }
        for name, records in data.items():
            (workdir / f"{name}.json").write_text(_json.dumps(records), encoding="utf-8")
        return workdir

    def test_a_product_is_joined_from_every_dataset(self):
        from sources.health_canada_dpd import HEALTH_CANADA_DPD
        from sources.open_registers import build_index, search_register

        build_index(HEALTH_CANADA_DPD, self._datasets())
        (row,) = search_register(HEALTH_CANADA_DPD, "metformin")
        # Named and numbered as the per-product connector did, so saved rows
        # are updated in place rather than duplicated.
        self.assertEqual(row["product"], "AG-METFORMIN")
        self.assertEqual(row["registration_number"], "02494418")
        self.assertEqual(row["atc_code"], "A10BA02")
        self.assertEqual(row["therapeutic_category"], "Metformin")
        self.assertEqual(row["authorisation_scope"], "Prescription")
        self.assertEqual(row["strength"], "500 MG")
        self.assertEqual((row["dosage_form"], row["route"]), ("Tablet", "Oral"))
        self.assertEqual(row["pack_size"], "100 TAB BOTTLE")
        self.assertEqual((row["status"], row["registration_date"]), ("Marketed", "2023-01-04"))

    def test_veterinary_and_homeopathic_products_are_left_out(self):
        from sources.health_canada_dpd import HEALTH_CANADA_DPD
        from sources.open_registers import build_index, search_register

        build_index(HEALTH_CANADA_DPD, self._datasets())
        self.assertEqual([row["product"] for row in search_register(HEALTH_CANADA_DPD, "metformin")], ["AG-METFORMIN"])

    def test_the_product_link_is_the_api_record_that_still_answers(self):
        from sources.health_canada_dpd import HEALTH_CANADA_DPD
        from sources.open_registers import build_index, search_register

        build_index(HEALTH_CANADA_DPD, self._datasets())
        (row,) = search_register(HEALTH_CANADA_DPD, "metformin")
        self.assertEqual(
            row["product_url"],
            "https://health-products.canada.ca/api/drug/drugproduct/?lang=en&type=json&id=98443",
        )

    def test_fetching_asks_for_every_dataset_the_join_needs(self):
        from sources import health_canada_dpd

        requested = []
        with patch.object(health_canada_dpd, "download_file",
                          side_effect=lambda register, url, destination: requested.append(url) or destination):
            health_canada_dpd.fetch_datasets(health_canada_dpd.HEALTH_CANADA_DPD, Path(self.directory.name))
        names = {url.split("/api/drug/")[1].split("/")[0] for url in requested}
        self.assertEqual(names, set(health_canada_dpd.DATASETS))


class CappedResultTests(unittest.TestCase):
    """Registries that stop at a fixed number say how many they hold."""

    def test_the_fda_passes_on_how_many_labels_match(self):
        from sources import fda

        response = MagicMock(status_code=200)
        response.json.return_value = {
            "meta": {"results": {"total": 3276}},
            "results": [{"openfda": {"brand_name": ["TYLENOL"], "manufacturer_name": ["Kenvue"]}}],
        }
        with patch.object(fda.requests, "get", return_value=response), \
             patch.object(fda, "_fetch_ndc_records", return_value=[]):
            rows = fda.run_fda_search("acetaminophen", limit=100)
        self.assertEqual(rows[0]["available_total"], 3276)

    def test_health_canada_passes_on_how_many_drug_codes_it_found(self):
        from sources import health_canada

        ingredients = [{"drug_code": str(code)} for code in range(1, 8)]
        with patch.object(health_canada, "_ingredient_rows", return_value=ingredients), \
             patch.object(health_canada, "_drug_record",
                          side_effect=lambda substance, code, rows: {"product": f"P{code}"}):
            rows = health_canada.run_health_canada_search("acetaminophen", limit=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["available_total"] for row in rows}, {7})

    def test_a_count_shows_the_total_only_when_it_is_larger(self):
        from main import records_with_total

        self.assertEqual(records_with_total(100, 3276), "100 of 3,276")
        self.assertEqual(records_with_total(8, 8), "8")
        self.assertEqual(records_with_total(21, 0), "21")

    def test_the_results_page_names_only_the_registries_that_were_capped(self):
        from main import capped_sources_note

        rows = [{"source": "FDA", "available_total": 3276}] * 100
        rows += [{"source": "FDA", "available_total": 0}]           # a synonym term that found nothing
        rows += [{"source": "Spain CIMA"}] * 20                      # says no total
        rows += [{"source": "Health Canada", "available_total": 3}] * 3  # returned all it holds
        note = capped_sources_note(rows)
        self.assertIn("FDA returned 100 of 3,276", note)
        self.assertNotIn("Spain CIMA", note)
        self.assertNotIn("Health Canada", note)
        self.assertEqual(capped_sources_note([{"source": "Spain CIMA"}]), "")

    def test_a_job_records_and_keeps_the_registry_total(self):
        from services import search_jobs

        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            job = search_jobs.SearchJob(
                job_id="capped-job",
                substance="acetaminophen",
                sources=["FDA"],
                progress={"FDA": search_jobs.SourceProgress(source="FDA")},
            )
            with search_jobs._lock:
                search_jobs._jobs[job.job_id] = job
            rows = [{"source": "FDA", "product": f"P{i}", "available_total": 3276} for i in range(100)]
            with patch.object(search_jobs, "_run_source_for_job", return_value=("FDA", rows, "")):
                search_jobs._run_job(job.job_id)
            live = search_jobs.get_search_job(job.job_id)
            with search_jobs._lock:
                search_jobs._jobs.pop(job.job_id, None)
            persisted = repository.get_persisted_search_job(job.job_id)

        self.assertEqual((live["progress"][0]["records"], live["progress"][0]["available"]), (100, 3276))
        # Read back from the database, as a job looks after a restart.
        self.assertEqual(persisted["progress"][0]["available"], 3276)


ITALY_FIXTURE = (
    "CODICE_AIC;COD_FARMACO;COD_CONFEZIONE;DENOMINAZIONE;DESCRIZIONE;CODICE_DITTA;RAGIONE_SOCIALE;"
    "STATO_AMMINISTRATIVO;TIPO_PROCEDURA;FORMA;CODICE_ATC;PA_ASSOCIATI;LINK\n"
    "026846010;026846;010;GLUCOPHAGE;500 MG COMPRESSE RIVESTITE- 30 COMPRESSE;1;MERCK SERONO S.P.A.;"
    "Autorizzata;Procedura Nazionale;Compressa rivestita con film;A10BA02;METFORMINA CLORIDRATO;\n"
    "026846022;026846;022;GLUCOPHAGE;500 MG COMPRESSE RIVESTITE- 60 COMPRESSE;1;MERCK SERONO S.P.A.;"
    "Autorizzata;Procedura Nazionale;Compressa rivestita con film;A10BA02;METFORMINA CLORIDRATO;\n"
    "026846034;026846;034;GLUCOPHAGE;30 COMPRESSE IN BLISTER DA 1000 MG;1;MERCK SERONO S.P.A.;"
    "Sospesa;Procedura Nazionale;Compressa rivestita con film;A10BA02;METFORMINA CLORIDRATO;\n"
    "049877011;049877;011;ADIABIN;50 MG/1000 MG COMPRESSE- 56 COMPRESSE;2;PHARMEXTRACTA S.P.A.;"
    "Autorizzata;Procedura Mutuo riconoscimento o Decentrata;Compressa rivestita con film;A10BD07;"
    "SITAGLIPTIN/METFORMINA CLORIDRATO;\n"
    "033001011;033001;011;NORVASC;5 MG COMPRESSE- 28 COMPRESSE;3;PFIZER ITALIA S.R.L.;"
    "Autorizzata;Procedura Nazionale;Compressa;C08CA01;AMLODIPINA BESILATO;\n"
    "900001011;900001;011;METFORMIN HOMEOPATHIC GRANULES;GRANULI 4 G;4;BOIRON;"
    "Autorizzata;Omeopatico;Granuli;V03AX;METFORMINA;\n"
)

BRAZIL_FIXTURE = (
    "TIPO_PRODUTO;NOME_PRODUTO;DATA_FINALIZACAO_PROCESSO;CATEGORIA_REGULATORIA;NUMERO_REGISTRO_PRODUTO;"
    "DATA_VENCIMENTO_REGISTRO;NUMERO_PROCESSO;CLASSE_TERAPEUTICA;EMPRESA_DETENTORA_REGISTRO;"
    "SITUACAO_REGISTRO;PRINCIPIO_ATIVO\n"
    '"MEDICAMENTO";"CLORIDRATO DE METFORMINA";"02/09/2024";"Genérico";155840680;"092034";"25351";'
    '"ANTIDIABETICOS";"06626253000151 - BRAINFARMA INDÚSTRIA QUÍMICA E FARMACÊUTICA S.A";"Ativo";'
    '"cloridrato de metformina"\n'
    '"MEDICAMENTO";"ANLO";"11/03/2015";"Similar";102351148;"032025";"25352";"ANTI-HIPERTENSIVOS";'
    '"57507378000365 - EMS S/A";"Inativo";"besilato de anlodipino"\n'
    '"MEDICAMENTO";"METFORMINUM";"22/10/2013";"DINAMIZADO";;;;;"60862208000141 - HOMEOPATICO LTDA";'
    '"Ativo";"metformina"\n'
)


class OpenRegisterTests(unittest.TestCase):
    """Italy's and Brazil's published registers, searched from a local index."""

    def setUp(self):
        from sources import open_registers

        self.registers = open_registers
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        index_dir = Path(self.directory.name) / "open_registers"
        patcher = patch.object(open_registers, "INDEX_DIR", index_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _index(self, register, text):
        csv_path = Path(self.directory.name) / f"{register.slug}.csv"
        csv_path.write_bytes(text.encode(register.encoding))
        self.registers.build_index(register, csv_path)

    def test_an_english_molecule_matches_its_italian_and_portuguese_spelling(self):
        tokens = self.registers.query_tokens
        inn = self.registers.inn_tokens
        for english, italian, portuguese in [
            ("amlodipine", "AMLODIPINA BESILATO", "besilato de anlodipino"),
            ("simvastatin", "SIMVASTATINA", "sinvastatina"),
            ("levothyroxine", "LEVOTIROXINA SODICA", "levotiroxina sódica"),
            ("amoxicillin", "AMOXICILLINA TRIIDRATO", "amoxicilina tri-hidratada"),
            ("metformin hydrochloride", "METFORMINA CLORIDRATO", "cloridrato de metformina"),
        ]:
            for register_text in (italian, portuguese):
                self.assertTrue(set(tokens(english)) <= set(inn(register_text)), (english, register_text))

    def test_salt_words_are_ignored_unless_they_are_the_whole_search(self):
        self.assertEqual(self.registers.query_tokens("metformin hydrochloride"), ["metformin"])
        self.assertEqual(len(self.registers.query_tokens("sodium chloride")), 2)

    def test_italian_packs_fold_into_one_row_per_product_strength_and_form(self):
        self._index(self.registers.ITALY, ITALY_FIXTURE)
        rows = self.registers.search_register(self.registers.ITALY, "metformin")
        by_product = {row["product"]: row for row in rows}

        glucophage = by_product["GLUCOPHAGE 500 MG, Film-coated tablet"]
        self.assertEqual(glucophage["pack_size"], "30 COMPRESSE; 60 COMPRESSE")
        self.assertEqual(glucophage["registration_number"], "026846")
        self.assertEqual(glucophage["dosage_form"], "Film-coated tablet")
        self.assertEqual(glucophage["status"], "Authorised")
        self.assertEqual(glucophage["authorisation_scope"], "National")
        self.assertEqual(glucophage["atc_code"], "A10BA02")
        # The pack written pack-first is still read, and keeps its own strength.
        self.assertEqual(by_product["GLUCOPHAGE 1000 MG, Film-coated tablet"]["pack_size"], "30 COMPRESSE")
        self.assertEqual(by_product["GLUCOPHAGE 1000 MG, Film-coated tablet"]["status"], "Suspended")
        # A combination keeps the register's own combination code.
        self.assertEqual(by_product["ADIABIN 50 MG/1000 MG, Film-coated tablet"]["atc_code"], "A10BD07")

    def test_two_forms_under_one_code_stay_two_products(self):
        powder = ITALY_FIXTURE + (
            "026846046;026846;046;GLUCOPHAGE;500 MG POLVERE PER SOLUZIONE ORALE- 30 BUSTINE;1;"
            "MERCK SERONO S.P.A.;Autorizzata;Procedura Nazionale;Polvere per soluzione orale;A10BA02;"
            "METFORMINA CLORIDRATO;\n"
        )
        self._index(self.registers.ITALY, powder)
        names = [row["product"] for row in self.registers.search_register(self.registers.ITALY, "metformin")]
        # Same AIC code and strength, different medicines: they must not share a
        # name, or the search job and the save path merge them into one row.
        self.assertIn("GLUCOPHAGE 500 MG, Film-coated tablet", names)
        self.assertIn("GLUCOPHAGE 500 MG, Powder for oral solution", names)
        self.assertEqual(len(names), len(set(names)))

    def test_homeopathic_registrations_are_left_out(self):
        self._index(self.registers.ITALY, ITALY_FIXTURE)
        self._index(self.registers.BRAZIL, BRAZIL_FIXTURE)
        for register in (self.registers.ITALY, self.registers.BRAZIL):
            products = [row["product"] for row in self.registers.search_register(register, "metformin")]
            self.assertFalse(any("HOMEOPATHIC" in p or p == "METFORMINUM" for p in products), products)

    def test_only_the_registers_own_active_ingredient_decides_a_match(self):
        self._index(self.registers.ITALY, ITALY_FIXTURE)
        amlodipine = self.registers.search_register(self.registers.ITALY, "amlodipine")
        self.assertEqual([row["product"] for row in amlodipine], ["NORVASC 5 MG, Tablet"])
        self.assertEqual(self.registers.search_register(self.registers.ITALY, "atorvastatin"), [])

    def test_authorised_rows_come_before_suspended_ones(self):
        self._index(self.registers.ITALY, ITALY_FIXTURE)
        statuses = [row["status"] for row in self.registers.search_register(self.registers.ITALY, "metformin")]
        self.assertEqual(statuses[-1], "Suspended")

    def test_brazilian_registrations_keep_category_dates_and_holder_name(self):
        self._index(self.registers.BRAZIL, BRAZIL_FIXTURE)
        (metformin,) = self.registers.search_register(self.registers.BRAZIL, "metformin")
        self.assertEqual(metformin["authorisation_scope"], "Generic")
        self.assertEqual(metformin["registration_date"], "02/09/2024")
        self.assertEqual(metformin["expiry_date"], "2034-09")
        self.assertEqual(metformin["company"], "BRAINFARMA INDÚSTRIA QUÍMICA E FARMACÊUTICA S.A")
        self.assertEqual(metformin["status"], "Active")
        self.assertEqual(metformin["region"], "BR")

        (amlodipine,) = self.registers.search_register(self.registers.BRAZIL, "amlodipine")
        self.assertEqual(amlodipine["product"], "ANLO")
        self.assertEqual(amlodipine["status"], "Inactive")

    def test_a_register_not_yet_downloaded_says_so_and_starts_the_download(self):
        with patch.object(self.registers, "refresh_in_background", return_value=True) as refresh:
            with self.assertRaises(self.registers.RegisterNotReady) as raised:
                self.registers.search_register(self.registers.BRAZIL, "metformin")
        refresh.assert_called_once_with(self.registers.BRAZIL)
        self.assertIn("search again in a few minutes", str(raised.exception))

    def test_a_stale_register_is_still_searched_while_it_refreshes(self):
        self._index(self.registers.BRAZIL, BRAZIL_FIXTURE)
        with patch.object(self.registers, "_is_stale", return_value=True), \
             patch.object(self.registers, "refresh_in_background") as refresh:
            rows = self.registers.search_register(self.registers.BRAZIL, "metformin")
        self.assertEqual(len(rows), 1)
        refresh.assert_called_once_with(self.registers.BRAZIL)

    def test_the_brazil_download_trusts_the_intermediate_its_server_omits(self):
        bundle = Path(self.registers._ca_bundle(self.registers.BRAZIL)).read_text(encoding="ascii")
        shipped = (
            self.registers.CERT_DIR / "sectigo_public_server_authentication_ca_ov_r36.pem"
        ).read_text(encoding="ascii").strip()
        self.assertIn(shipped, bundle)
        # certifi's own roots are still there: verification is extended, not replaced.
        import certifi
        self.assertIn(Path(certifi.where()).read_text(encoding="ascii").strip()[:200], bundle)

    def test_italy_and_brazil_are_searched_for_their_countries_and_regions(self):
        from services.search_pipeline import DEFAULT_SOURCES, sources_for_scope

        self.assertIn("AIFA Italy", sources_for_scope(DEFAULT_SOURCES, country="Italy"))
        self.assertIn("AIFA Italy", sources_for_scope(DEFAULT_SOURCES, region="EU"))
        self.assertEqual(sources_for_scope(DEFAULT_SOURCES, country="Brazil"), ["ANVISA Brazil"])
        self.assertEqual(sources_for_scope(DEFAULT_SOURCES, region="BR"), ["ANVISA Brazil"])


class EnvFileTests(unittest.TestCase):
    def test_credentials_in_the_env_file_reach_the_environment(self):
        from config import load_env_file

        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text(
                "# admin sign-in\n"
                "PHARMASEARCH_TEST_USER=demo-admin\n"
                'export PHARMASEARCH_TEST_QUOTED="scrypt$abc=def"\n'
                "not a setting\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PHARMASEARCH_TEST_USER", None)
                os.environ.pop("PHARMASEARCH_TEST_QUOTED", None)
                load_env_file(env)
                self.assertEqual(os.environ["PHARMASEARCH_TEST_USER"], "demo-admin")
                # A digest can hold "=" and "$"; only the first "=" splits.
                self.assertEqual(os.environ["PHARMASEARCH_TEST_QUOTED"], "scrypt$abc=def")

    def test_a_variable_already_in_the_environment_is_not_overridden(self):
        from config import load_env_file

        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("PHARMASEARCH_TEST_USER=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"PHARMASEARCH_TEST_USER": "from-deployment"}):
                load_env_file(env)
                self.assertEqual(os.environ["PHARMASEARCH_TEST_USER"], "from-deployment")

    def test_writing_credentials_keeps_the_other_lines(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "make_admin_password", Path(__file__).parent / "tools" / "make_admin_password.py"
        )
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text(
                "OPENAI_API_KEY=keep-me\nPHARMASEARCH_ADMIN_USERNAME=old\n", encoding="utf-8"
            )
            tool.write_env({
                "PHARMASEARCH_ADMIN_USERNAME": "new",
                "PHARMASEARCH_SESSION_SECRET": "s",
            }, env)
            lines = env.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines,
            ["OPENAI_API_KEY=keep-me", "PHARMASEARCH_ADMIN_USERNAME=new", "PHARMASEARCH_SESSION_SECRET=s"],
        )


class EmaPageUrlTests(unittest.TestCase):
    def test_only_https_pages_on_the_ema_website_are_read(self):
        from sources.ema_product_parser import is_ema_page_url

        self.assertTrue(is_ema_page_url("https://www.ema.europa.eu/en/medicines/human/EPAR/avandamet"))
        for url in [
            "http://www.ema.europa.eu/en/x",          # not https
            "http://127.0.0.1:8765/api/version",      # internal address
            "https://169.254.169.254/latest/meta-data",
            "file:///C:/Windows/win.ini",
            "https://ema.europa.eu@evil.example/x",   # credentials trick
            "https://ema.europa.eu.evil.example/x",   # look-alike host
            "https://evilema.europa.eu/x",
            "https://www.ema.europa.eu:8443/x",       # unexpected port
            "",
        ]:
            self.assertFalse(is_ema_page_url(url), url)

    def test_a_refused_address_never_starts_a_browser(self):
        from sources import ema_product_parser

        with patch.object(ema_product_parser, "sync_playwright") as playwright:
            with self.assertRaises(ema_product_parser.NotAnEmaPage):
                ema_product_parser.extract_product_page("http://127.0.0.1/")
        playwright.assert_not_called()

    def test_a_page_with_no_product_is_not_saved(self):
        import main

        with patch.object(main, "extract_product_page", lambda url: {"product_name": ""}), \
             patch.object(main, "save_product_detail") as save:
            with self.assertRaises(main.EmptyProductPage):
                main.save_ema_product_page("https://www.ema.europa.eu/en/x")
        save.assert_not_called()


class RegulatoryDateTests(unittest.TestCase):
    def test_every_shape_a_registry_publishes_is_read(self):
        from datetime import date
        from services.regulatory_dates import parse_regulatory_date

        for text, expected in [
            ("2024-08-23", date(2024, 8, 23)),
            ("2025-09-15T10:56:55Z", date(2025, 9, 15)),
            ("2018-04-03T09:47:15+00:00", date(2018, 4, 3)),
            ("31/12/2009", date(2009, 12, 31)),
            ("17 Dec, 2013", date(2013, 12, 17)),
            ("22-DEC-2020", date(2020, 12, 22)),
            ("2025-Apr-07", date(2025, 4, 7)),
            ("22.12.2023", date(2023, 12, 22)),
            ("20261231", date(2026, 12, 31)),
        ]:
            self.assertEqual(parse_regulatory_date(text), expected, text)

    def test_a_slashed_date_is_read_day_first(self):
        from datetime import date
        from services.regulatory_dates import parse_regulatory_date

        self.assertEqual(parse_regulatory_date("03/04/2020"), date(2020, 4, 3))

    def test_an_impossible_or_empty_date_reads_as_none(self):
        from services.regulatory_dates import parse_regulatory_date

        for text in ["99/99/9999", "31/02/2020", "Not yet assigned", "", None]:
            self.assertIsNone(parse_regulatory_date(text))

    def test_newest_first_orders_by_the_date_not_the_text(self):
        from services.search_pipeline import sort_rows

        rows = [
            {"registration_date": "31/12/2009"},
            {"registration_date": ""},
            {"registration_date": "2024-01-01"},
            {"registration_date": "17 Dec, 2013"},
        ]
        newest = [r["registration_date"] for r in sort_rows(rows, [], "registration_date", "desc")]
        oldest = [r["registration_date"] for r in sort_rows(rows, [], "registration_date", "asc")]
        self.assertEqual(newest, ["2024-01-01", "17 Dec, 2013", "31/12/2009", ""])
        # A row with no date answers neither question, so it goes last both ways.
        self.assertEqual(oldest, ["31/12/2009", "17 Dec, 2013", "2024-01-01", ""])


class SearchTermCleaningTests(unittest.TestCase):
    def test_padding_and_repeated_spaces_are_removed(self):
        from repository import clean_search_term

        self.assertEqual(clean_search_term("  metformin  "), "metformin")
        self.assertEqual(clean_search_term("amlodipine  /\tvalsartan "), "amlodipine / valsartan")
        self.assertEqual(clean_search_term(None), "")

    def test_a_padded_search_finds_the_same_rows(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.save_product_detail({
                "substance": "examplezumab",
                "product": "Brandname examplezumab 10 mg",
                "source": "Example Regulator",
                "registration_number": "EX-PAD",
            })
            plain = repository.search_product_details("examplezumab")
            padded = repository.search_product_details("  examplezumab  ")
        self.assertEqual(len(plain), 1)
        self.assertEqual(len(padded), len(plain))


class CombinationRouteTests(unittest.TestCase):
    def test_a_substance_containing_a_slash_reaches_the_export_route(self):
        import main
        from starlette.routing import Match

        paths = {
            "/export/amlodipine / valsartan": "export_live",
            "/deep_export/amlodipine / valsartan": "deep_export_live",
            "/search/amlodipine / valsartan": "search_saved",
        }
        for path, endpoint_name in paths.items():
            scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
            matched = [
                route for route in main.app.router.routes
                if getattr(route, "matches", None) and route.matches(scope)[0] == Match.FULL
            ]
            self.assertTrue(matched, path)
            self.assertEqual(matched[0].endpoint.__name__, endpoint_name)
            self.assertEqual(
                matched[0].matches(scope)[1]["path_params"]["substance"], "amlodipine / valsartan"
            )


class BoundedResultCacheTests(unittest.TestCase):
    def test_only_the_most_recent_entries_are_kept(self):
        from services.result_cache import BoundedResultCache

        cache = BoundedResultCache(max_entries=3)
        for key in "abcd":
            cache[key] = [key]
        self.assertEqual(len(cache), 3)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("d"), ["d"])

    def test_reading_an_entry_keeps_it_from_being_dropped(self):
        from services.result_cache import BoundedResultCache

        cache = BoundedResultCache(max_entries=2)
        cache["search"] = ["rows"]
        cache["sorted"] = ["rows"]
        cache.get("search")          # the export reads it back
        cache["filtered"] = ["rows"]
        self.assertEqual(cache.get("search"), ["rows"])
        self.assertIsNone(cache.get("sorted"))

    def test_an_entry_lapses_after_its_time(self):
        from services.result_cache import BoundedResultCache

        now = [1000.0]
        cache = BoundedResultCache(max_entries=4, ttl_seconds=60, clock=lambda: now[0])
        cache["search"] = ["rows"]
        now[0] += 59
        self.assertEqual(cache.get("search"), ["rows"])
        now[0] += 120
        self.assertIsNone(cache.get("search"))
        self.assertEqual(len(cache), 0)


class SubstanceSynonymTests(unittest.TestCase):
    def test_a_us_adopted_name_is_reached_from_the_inn_and_back(self):
        for inn, usan in [
            ("salbutamol", "albuterol"),
            ("adrenaline", "epinephrine"),
            ("glibenclamide", "glyburide"),
            ("rifampicin", "rifampin"),
        ]:
            self.assertIn(usan, get_substance_search_terms(inn))
            self.assertIn(inn, get_substance_search_terms(usan))

    def test_the_term_typed_is_always_searched_first(self):
        self.assertEqual(get_substance_search_terms("salbutamol")[0], "salbutamol")
        self.assertEqual(get_substance_search_terms("Albuterol")[0], "Albuterol")

    def test_a_molecule_with_no_alias_searches_only_itself(self):
        self.assertEqual(get_substance_search_terms("tirzepatide"), ["tirzepatide"])

    def test_a_combination_reaches_its_parts_but_not_the_reverse(self):
        combination = get_substance_search_terms("lidocaine+prilocaine")
        self.assertIn("lidocaine", combination)
        self.assertIn("prilocaine", combination)
        # A search for one molecule must not drag in every combination it
        # appears in, or paracetamol would search half the register.
        self.assertNotIn("prilocaine", get_substance_search_terms("lidocaine"))


class SearchJobTimeoutTests(unittest.TestCase):
    def test_a_slow_term_does_not_discard_the_terms_that_answered(self):
        import time as _time
        from services import search_jobs

        def fake_connector(source, term, substance):
            if term == "slow-term":
                _time.sleep(10)
                return [{"product": "Late", "source": source}]
            return [{"product": "Answered", "source": source, "country": "X"}]

        with patch.object(search_jobs, "_run_connector_once", fake_connector), \
             patch.object(search_jobs, "get_substance_search_terms",
                          lambda s: [s, "slow-term"]), \
             patch.object(search_jobs, "_source_timeout", lambda s: 1), \
             patch.object(search_jobs, "_set_source_progress", lambda *a, **k: None), \
             patch.object(search_jobs, "record_source_health", lambda *a, **k: None):
            _source, rows, error = search_jobs._run_source_for_job("job", "example", "FDA")

        self.assertEqual([row["product"] for row in rows], ["Answered"])
        # Rows arrived, so the source did not fail even though a term ran on.
        self.assertEqual(error, "")

    def test_the_registries_measured_slower_than_the_default_are_given_longer(self):
        from services.search_jobs import (
            JOB_SLOW_SOURCE_TIMEOUT_SECONDS,
            _source_timeout,
        )
        for source in ("France BDPM", "CDSCO India"):
            self.assertEqual(_source_timeout(source), JOB_SLOW_SOURCE_TIMEOUT_SECONDS)


class EvidenceBackfillTests(unittest.TestCase):
    """The provenance reconstructed for rows saved before evidence existed."""

    def _backfill(self, *records):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            for record in records:
                repository.save_product_detail(record)
            summary = backfill_evidence()
            rows = repository.search_product_details("example")
            return summary, {row["registration_number"]: row for row in rows}

    def test_register_values_point_at_the_record_they_were_read_from(self):
        _, rows = self._backfill({
            "substance": "example",
            "product": "Example 10 mg tablets",
            "country": "Exampleland",
            "source": "Example Regulator",
            "registration_number": "EX-1",
            "product_url": "https://regulator.test/products/1",
            "strength": "10 mg",
        })

        assertions = {item["field_name"]: item for item in rows["EX-1"]["evidence"]}
        self.assertEqual(assertions["strength"]["value"], "10 mg")
        self.assertEqual(
            assertions["strength"]["evidence_url"], "https://regulator.test/products/1"
        )
        self.assertEqual(
            assertions["strength"]["verification_status"], "VERIFIED_REGULATOR_RECORD"
        )
        self.assertEqual(assertions["strength"]["source_regulator"], "Example Regulator")

    def test_a_completed_field_credits_the_lenders_and_stays_unverified(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            # A row with no ATC code of its own, and a lender in the same
            # molecule group for completion to borrow one from.
            repository.save_product_detail({
                "substance": "example",
                "product": "Example 10 mg tablets",
                "source": "Example Regulator",
                "registration_number": "EX-2",
                "product_url": "https://regulator.test/products/2",
            })
            repository.save_product_detail({
                "substance": "example",
                "product": "Example lender 10 mg tablets",
                "source": "EMA",
                "registration_number": "EX-2-LENDER",
                "product_url": "https://ema.test/products/2",
                "atc_code": "N02BE01",
            })
            complete_fields()
            rows = {
                row["registration_number"]: row
                for row in repository.search_product_details("example")
            }

        atc = next(
            item for item in rows["EX-2"]["evidence"] if item["field_name"] == "atc_code"
        )
        # Agreed across a molecule group, so it must not borrow this
        # register's authority for a value the register never published.
        self.assertEqual(atc["value"], "N02BE01")
        self.assertEqual(atc["source_regulator"], "EMA")
        self.assertEqual(atc["verification_status"], "UNVERIFIED")

    def test_completion_replaces_the_assertion_it_makes_stale(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.save_product_detail({
                "substance": "example",
                "product": "Example 10 mg tablets",
                "source": "Example Regulator",
                "registration_number": "EX-6",
                "product_url": "https://regulator.test/products/6",
                "atc_code": "N02BE01",
                "therapeutic_category": "Hipocolesterolemiante",
            })
            repository.save_product_detail({
                "substance": "example",
                "product": "Example lender 10 mg tablets",
                "source": "EMA",
                "registration_number": "EX-6-LENDER",
                "product_url": "https://ema.test/products/6",
                "atc_code": "N02BE01",
            })
            complete_fields()
            rows = {
                row["registration_number"]: row
                for row in repository.search_product_details("example")
            }

        row = rows["EX-6"]
        categories = [
            item for item in row["evidence"]
            if item["field_name"] == "therapeutic_category"
        ]
        # Completion rewrites the category, so exactly one assertion should
        # describe it -- the superseded one would contradict the column.
        self.assertEqual(len(categories), 1)
        self.assertEqual(categories[0]["value"], row["therapeutic_category"])

    def test_a_manufacturer_read_from_a_document_cites_the_document(self):
        _, rows = self._backfill({
            "substance": "example",
            "product": "Example 10 mg tablets",
            "source": "MHRA",
            "registration_number": "EX-3",
            "product_url": "https://regulator.test/products/3",
            "smpc_url": "https://regulator.test/products/3/smpc.pdf",
            "manufacturer_name": "Example Manufacturing Ltd",
            "manufacturer_source": "MHRA document",
        })

        manufacturer = next(
            item for item in rows["EX-3"]["evidence"]
            if item["field_name"] == "manufacturer_name"
        )
        self.assertEqual(
            manufacturer["evidence_url"], "https://regulator.test/products/3/smpc.pdf"
        )
        self.assertEqual(
            manufacturer["verification_status"], "VERIFIED_OFFICIAL_DOCUMENT"
        )

    def test_a_row_with_no_address_asserts_nothing(self):
        summary, rows = self._backfill({
            "substance": "example",
            "product": "Example 10 mg tablets",
            "source": "Example Regulator",
            "registration_number": "EX-4",
            "strength": "10 mg",
        })

        self.assertEqual(summary["rows_without_source_url"], 1)
        self.assertEqual(rows["EX-4"]["evidence"], [])

    def test_only_a_parsed_registry_is_told_its_documents_are_pending(self):
        parsed = {
            "source": "MHRA",
            "smpc_url": "https://regulator.test/smpc.pdf",
            "manufacturer_name": "",
        }
        unparsed = {**parsed, "source": "Health Canada"}
        undocumented = {"source": "MHRA", "manufacturer_name": ""}

        self.assertEqual(missing_reason_for_row(parsed), "Pending document enrichment")
        self.assertEqual(missing_reason_for_row(unparsed), "Not collected for this source")
        self.assertEqual(missing_reason_for_row(undocumented), "NOT_PUBLISHED")

    def test_a_row_that_names_its_manufacturer_is_given_no_reason(self):
        # Role and batch release site are almost never published, so asking
        # for every manufacturer column would contradict the name in the
        # export row beside it.
        named = {
            "source": "Health Canada",
            "smpc_url": "https://regulator.test/smpc.pdf",
            "manufacturer_name": "Example Manufacturing Ltd",
            "manufacturer_role": "",
            "batch_release_manufacturer": "",
        }
        self.assertEqual(missing_reason_for_row(named), "")

    def test_a_second_pass_refreshes_rather_than_duplicates(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.save_product_detail({
                "substance": "example",
                "product": "Example 10 mg tablets",
                "source": "Example Regulator",
                "registration_number": "EX-5",
                "product_url": "https://regulator.test/products/5",
                "strength": "10 mg",
            })
            first = backfill_evidence()
            with repository.get_connection() as conn:
                after_first = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

            second = backfill_evidence()
            with repository.get_connection() as conn:
                after_second = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

        self.assertGreater(first["assertions_written"], 0)
        self.assertEqual(second["rows_scanned"], 0)
        self.assertEqual(after_first, after_second)


class CombinationAndMhraRepairTests(unittest.TestCase):
    """Rows stored before combinations were keyed apart and MHRA rows were checked."""

    def setUp(self):
        # An empty database is otherwise filled from the seed fixture.
        seed = patch.object(repository, "PRODUCT_DETAILS_SEED_PATH", Path("no-seed.jsonl"))
        seed.start()
        self.addCleanup(seed.stop)

    def _database(self, directory):
        return patch.object(repository, "DB_PATH", Path(directory) / "test.db")

    def test_the_stored_search_finds_a_molecule_inside_a_combination(self):
        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"substance": "SITAGLIPTINE, METFORMINE", "product": "Janumet 50 mg/1000 mg",
                 "source": "France BDPM", "country": "France"},
                {"substance": "metoprolol", "product": "Lopressor", "source": "FDA", "country": "United States"},
            ])
            found = [row["product"] for row in repository.search_product_details("metformin")]
        self.assertEqual(found, ["Janumet 50 mg/1000 mg"])

    def test_the_stored_search_finds_every_name_and_salt_a_register_used(self):
        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"substance": "cephalexin", "product": "APO-CEPHALEX", "source": "Health Canada", "country": "Canada"},
                {"substance": "CÉFALEXINE MONOHYDRATÉE", "product": "CEFALEXINE BIOGARAN",
                 "source": "France BDPM", "country": "France"},
                {"substance": "cefuroxime", "product": "Zinnat", "source": "MHRA", "country": "United Kingdom"},
            ])
            for typed in ("cefalexin", "céfalexine"):
                found = sorted(row["product"] for row in repository.search_product_details(typed))
                self.assertEqual(found, ["APO-CEPHALEX", "CEFALEXINE BIOGARAN"], typed)

    def test_mhra_rows_are_kept_only_when_their_licence_is_for_the_molecule(self):
        from services.data_repairs import verify_mhra_rows

        rows = [
            # An amlodipine report filed under atorvastatin.
            {"substance": "atorvastatin", "product": "Amlodipine 10mg Tablets - PL 15764/0016",
             "registration_number": "PL 15764/0016", "product_url": "https://blob/amlodipine",
             "source": "MHRA", "country": "United Kingdom"},
            # A real atorvastatin leaflet, stored under its file name, whose PDF
            # has since been replaced by a revised one.
            {"substance": "atorvastatin", "product": "leaflet MAH GENERIC_PL 49445-0400.pdf",
             "registration_number": "PL 49445/0400", "product_url": "https://blob/old-leaflet",
             "pil_url": "https://blob/old-leaflet", "document_type": "PIL",
             "source": "MHRA", "country": "United Kingdom"},
            # A licence no longer in the register.
            {"substance": "atorvastatin", "product": "spc-doc_PL 00001-0001.pdf",
             "registration_number": "PL 00001/0001", "product_url": "https://blob/gone",
             "source": "MHRA", "country": "United Kingdom"},
            # Named for its molecule, so not in doubt and never looked up.
            {"substance": "atorvastatin", "product": "Atorvastatin 20 mg Tablets",
             "registration_number": "PL 00001/0002", "product_url": "https://blob/fine",
             "source": "MHRA", "country": "United Kingdom"},
        ]
        documents = [
            {"metadata_storage_path": "https://blob/amlodipine", "pl_number": ["PL157640016"],
             "substance_name": ["AMLODIPINE BESILATE"], "title": "Amlodipine 10mg Tablets",
             "product_name": "", "doc_type": "Par"},
            {"metadata_storage_path": "https://blob/new-leaflet", "pl_number": ["PL494450400"],
             "substance_name": ["ATORVASTATIN CALCIUM TRIHYDRATE"],
             "title": "leaflet MAH GENERIC_PL 49445-0400.pdf",
             "product_name": "ATORVASTATIN 20 MG FILM-COATED TABLETS", "doc_type": "Pil"},
        ]
        looked_up = []

        def fetch(licences):
            looked_up.extend(licences)
            return documents

        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details(rows)
            result = verify_mhra_rows(fetch=fetch)
            with repository.get_connection() as conn:
                stored = {
                    row["registration_number"]: dict(row)
                    for row in conn.execute("SELECT * FROM product_details WHERE source = 'MHRA'")
                }

        self.assertNotIn("PL00010002", looked_up)
        self.assertEqual(sorted(stored), ["PL 00001/0002", "PL 49445/0400"])
        leaflet = stored["PL 49445/0400"]
        self.assertEqual(leaflet["product"], "ATORVASTATIN 20 MG FILM-COATED TABLETS")
        self.assertEqual(leaflet["substance"], "ATORVASTATIN CALCIUM TRIHYDRATE")
        self.assertEqual(leaflet["pil_url"], "https://blob/new-leaflet")
        self.assertEqual(result["removed_because"]["another_molecule"], 1)
        self.assertEqual(result["removed_because"]["licence_not_in_register"], 1)

    def test_a_failed_lookup_leaves_rows_untouched(self):
        from services.data_repairs import verify_mhra_rows

        def fetch(licences):
            raise requests.ConnectionError("down")

        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"substance": "atorvastatin", "product": "Amlodipine 10mg Tablets",
                 "registration_number": "PL 15764/0016", "source": "MHRA", "country": "United Kingdom"},
            ])
            result = verify_mhra_rows(fetch=fetch)
            with repository.get_connection() as conn:
                count = conn.execute("SELECT COUNT(*) FROM product_details").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(result["left_unchecked_lookup_failed"], 1)

    def test_codes_crossed_between_a_molecule_and_its_combination_are_corrected(self):
        from services.data_repairs import correct_group_atc_codes

        rows = [
            {"substance": "metformin", "product": f"Metformin {n} mg", "atc_code": "A10BA02",
             "source": "EMA", "country": "EU"} for n in (500, 850, 1000)
        ] + [
            # Metformin lent the combination's code.
            {"substance": "metformin", "product": "Metformin Teva 750 mg", "atc_code": "A10BD07",
             "source": "MHRA", "country": "United Kingdom"},
            # Janumet lent metformin's code, beside two carrying their own.
            {"substance": "metformin", "source_substance": "SITAGLIPTIN, METFORMIN",
             "product": "Janumet 50 mg/850 mg", "atc_code": "A10BA02", "source": "MHRA",
             "country": "United Kingdom"},
            {"substance": "sitagliptin;metformin", "product": "Janumet", "atc_code": "A10BD07",
             "source": "EMA", "country": "EU"},
            {"substance": "sitagliptin;metformin", "product": "Velmetia", "atc_code": "A10BD07",
             "source": "EMA", "country": "EU"},
            # A combination Spain did not state, lent the molecule's code.
            {"substance": "metformin", "product": "EUCREAS 50 MG/850 MG COMPRIMIDOS",
             "atc_code": "A10BA02", "source": "Spain CIMA", "country": "Spain"},
        ]
        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details(rows)
            result = correct_group_atc_codes()
            with repository.get_connection() as conn:
                codes = {row[0]: row[1] for row in conn.execute("SELECT product, atc_code FROM product_details")}

        self.assertEqual(codes["Metformin Teva 750 mg"], "A10BA02")
        self.assertEqual(codes["Janumet 50 mg/850 mg"], "A10BD07")
        self.assertEqual(codes["EUCREAS 50 MG/850 MG COMPRIMIDOS"], "")
        self.assertEqual(codes["Metformin 500 mg"], "A10BA02")
        self.assertEqual(result["single_molecule_rows_corrected"], 1)
        self.assertEqual(result["combination_rows_corrected"], 1)


class MhraCombinationTitleTests(unittest.TestCase):
    def test_a_report_with_no_substance_field_takes_the_combination_its_title_names(self):
        from sources.mhra import _extract_mhra_json_record

        record = {
            "title": "Amlodipine/Valsartan 5 mg/80 mg film-coated tablets - PL 12345/0001",
            "substance_name": [], "pl_number": ["PL123450001"], "doc_type": "Par",
            "metadata_storage_path": "https://blob/par",
        }
        row = _extract_mhra_json_record(record, "amlodipine")
        self.assertEqual(row["substance"], "Amlodipine/Valsartan")

    def test_a_single_molecule_report_keeps_the_molecule(self):
        from sources.mhra import _extract_mhra_json_record

        record = {
            "title": "Amlodipine 10mg Tablets (amlodipine besilate) - PL 15764/0016",
            "substance_name": [], "pl_number": ["PL157640016"], "doc_type": "Par",
            "metadata_storage_path": "https://blob/par",
        }
        self.assertEqual(_extract_mhra_json_record(record, "amlodipine")["substance"], "amlodipine besilate")


class VendorDisplayTests(unittest.TestCase):
    """What a vendor reads in the result table and the export."""

    def test_markup_and_placeholders_are_not_shown_as_values(self):
        from services.vendor_display import clean_atc_code, clean_text

        self.assertEqual(clean_text("AMLODIPINE BESILATE&lt;br&gt;VALSARTAN"), "AMLODIPINE BESILATE; VALSARTAN")
        self.assertEqual(clean_text("PROCTER &amp;amp; GAMBLE"), "PROCTER & GAMBLE")
        for placeholder in ("N/A", "-", "--", "Not yet assigned", "na"):
            self.assertEqual(clean_text(placeholder), "", placeholder)
        self.assertEqual(clean_atc_code("Not yet assigned"), "")
        self.assertEqual(clean_atc_code("A02BC011"), "")
        self.assertEqual(clean_atc_code("c10aa05"), "C10AA05")

    def test_company_names_lose_page_footers_split_words_and_leaflet_prose(self):
        from services.vendor_display import clean_company

        self.assertEqual(clean_company("The Boots Compan y PLC"), "The Boots Company PLC")
        self.assertEqual(clean_company("Sandoz Ltd Page 8 of 8"), "Sandoz Ltd")
        self.assertEqual(
            clean_company("Jarama, 111; 45007-Toledo; Espana; Esta informacion esta destinada unicamente a medicos"),
            "Jarama, 111; 45007-Toledo; Espana",
        )

    def test_the_manufacturer_column_names_the_holder_and_says_so(self):
        from services.result_formatter import formatted_result_row

        # Italy publishes the holder, not the plant.
        italy = formatted_result_row({
            "source": "AIFA Italy", "product": "ATOZET 10MG/10MG",
            "company": "ORGANON ITALIA S.R.L.", "country": "Italy",
        })
        self.assertEqual(italy["manufacturer_name"], "ORGANON ITALIA S.R.L. (licence holder)")
        # Where the register names the maker, it is shown plainly.
        vietnam = formatted_result_row({
            "source": "DAV Vietnam", "product": "Lipvar", "company": "Cong ty A",
            "manufacturer_name": "Daewoo Pharm. Co., Ltd.", "manufacturer_source": "register",
            "country": "Vietnam",
        })
        self.assertEqual(vietnam["manufacturer_name"], "Daewoo Pharm. Co., Ltd.")
        # With neither, nothing is invented.
        self.assertEqual(
            formatted_result_row({"source": "FDA", "product": "X", "country": "United States"})["manufacturer_name"],
            "Not published by regulator",
        )

    def test_leaflet_sentences_are_not_shown_as_a_company(self):
        from services.vendor_display import clean_company

        for sentence in (
            "breathing problems. There can also alcohol and",
            "or if you need to take the medicine more often. and Manufacturer",
        ):
            self.assertEqual(clean_company(sentence), "")
        # Real names in any language keep their place.
        for name in (
            "MEDIFARMA LABORATORIES", "LABORATOIRE DE L'HOMME DE FER",
            "Cong ty TNHH Lien doanh Hasan - Dermapharm", "Army & Air Force Exchange Service",
        ):
            self.assertEqual(clean_company(name), name)

    def test_every_registry_status_reads_in_one_vocabulary(self):
        from services.vendor_display import vendor_status

        cases = {
            "Commercialisée": "Marketed",
            "Non commercialisée": "Authorised, not marketed",
            "available, not_commercialised": "Marketed",
            "not_commercialised, unavailable": "Authorised, not marketed",
            "Cancelled Post Market": "Withdrawn",
            "Berlaku": "Authorised",
            "Berlaku (Khusus Ekspor)": "Authorised for export only",
            "Registered - Part 1 Poison": "Authorised",
            "Lapsed": "Expired",
            "Application withdrawn": "Application withdrawn",
        }
        for registry, shown in cases.items():
            self.assertEqual(vendor_status(registry), shown, registry)

    def test_dosage_forms_are_shown_in_english(self):
        from services.vendor_display import english_dosage_form

        cases = {
            "comprimé pelliculé sécable": "Film-coated tablet (scored)",
            "comprime pellicule et comprime pellicule": "Film-coated tablet",
            "comprime orodispersible": "Orodispersible tablet",
            "solution injectable": "Solution for injection",
            "KAPSUL; 75 mg": "Capsule",
            "Vien nen bao phim": "Film-coated tablet",
            "LIOF. ORAL": "Oral lyophilisate",
            "Film-coated tablet": "Film-coated tablet",
        }
        for registry, shown in cases.items():
            self.assertEqual(english_dosage_form(registry), shown, registry)

    def test_dates_are_shown_as_iso(self):
        from services.vendor_display import display_date

        self.assertEqual(display_date("31/12/2009"), "2009-12-31")
        self.assertEqual(display_date("2020-03-13T06:44:23Z"), "2020-03-13")

    def test_the_result_row_shows_the_holder_as_company_and_a_readable_missing_value(self):
        from services.result_formatter import formatted_result_row

        row = formatted_result_row({
            "source": "Health Canada", "product": "METFORMIN", "company": "APOTEX INC",
            "status": "Cancelled Post Market", "dosage_form": "Tablet",
        })
        self.assertEqual(row["company"], "APOTEX INC")
        self.assertEqual(row["registration_status"], "Withdrawn")
        self.assertNotIn("NOT_PUBLISHED", row.values())


class DeadMhraDocumentTests(unittest.TestCase):
    OLD = "https://mhraproducts4853.blob.core.windows.net/docs/old-leaflet"
    NEW = "https://mhraproducts4853.blob.core.windows.net/docs/new-leaflet"

    def test_only_404_and_410_count_as_dead(self):
        from sources import document_links

        answers = {"https://a/404": 404, "https://a/200": 200, "https://a/410": 410}

        def head(url, **_kwargs):
            if url == "https://a/timeout":
                raise requests.Timeout("slow")
            return MagicMock(status_code=answers[url])

        document_links._cache.clear()
        with patch("requests.Session.head", side_effect=head):
            dead = document_links.dead_links([*answers, "https://a/timeout"])
        self.assertEqual(dead, {"https://a/404", "https://a/410"})

    def test_a_live_search_drops_a_deleted_pdf_and_keeps_the_licence_current_one(self):
        from sources import mhra

        results = [
            {"registration_number": "PL 1/1", "document_type": "PIL", "pil_url": self.OLD,
             "url": self.OLD, "product_url": self.OLD, "product": "BETMIGA"},
            {"registration_number": "PL 1/1", "document_type": "PIL", "pil_url": self.NEW,
             "url": self.NEW, "product_url": self.NEW, "product": "BETMIGA"},
        ]
        with patch.object(mhra, "dead_links", return_value={self.OLD}):
            rows = mhra._finalize_mhra_results(results)
        self.assertEqual([row["pil_url"] for row in rows], [self.NEW])

    def test_stored_dead_links_take_the_current_document_or_are_removed(self):
        from services.data_repairs import relink_dead_mhra_documents

        documents = [{"metadata_storage_path": self.NEW, "pl_number": ["PLPI163780990"], "doc_type": "Pil",
                      "substance_name": ["MIRABEGRON"], "title": "", "product_name": "BETMIGA"}]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(repository, "DB_PATH", Path(directory) / "test.db"), \
                patch.object(repository, "PRODUCT_DETAILS_SEED_PATH", Path("no-seed.jsonl")):
            repository.save_product_details([
                {"product": "BETMIGA 50 MG", "substance": "mirabegron", "source": "MHRA", "country": "United Kingdom",
                 "registration_number": "PLPI 16378/0990", "document_type": "PIL",
                 "pil_url": self.OLD, "product_url": self.OLD},
                {"product": "OTHER 5 MG", "substance": "mirabegron", "source": "MHRA", "country": "United Kingdom",
                 "registration_number": "PL 00000/0001", "document_type": "SPC",
                 "smpc_url": self.OLD + "-spc", "product_url": self.OLD + "-spc"},
            ])
            result = relink_dead_mhra_documents(
                fetch=lambda licences: documents,
                check=lambda urls: {url for url in urls if "old-leaflet" in url},
            )
            with repository.get_connection() as conn:
                rows = {row["product"]: dict(row) for row in conn.execute("SELECT * FROM product_details")}

        self.assertEqual(rows["BETMIGA 50 MG"]["pil_url"], self.NEW)
        self.assertEqual(rows["BETMIGA 50 MG"]["product_url"], self.NEW)
        self.assertEqual(rows["OTHER 5 MG"]["smpc_url"], "")
        # Its SmPC, product and evidence links all pointed at the deleted file.
        self.assertEqual(result["links_removed_no_current_document"], 3)


class OnlyRegulatorValuesTests(unittest.TestCase):
    """A row shows what its own regulator published, nothing lent from another."""

    def setUp(self):
        seed = patch.object(repository, "PRODUCT_DETAILS_SEED_PATH", Path("no-seed.jsonl"))
        seed.start()
        self.addCleanup(seed.stop)

    def _rows(self):
        with repository.get_connection() as conn:
            return {row["product"]: dict(row) for row in conn.execute("SELECT * FROM product_details")}

    def test_lent_values_and_mhra_document_dates_are_withdrawn(self):
        from services.data_repairs import revoke_borrowed_values

        with tempfile.TemporaryDirectory() as directory, patch.object(repository, "DB_PATH", Path(directory) / "t.db"):
            repository.save_product_details([
                # MHRA publishes no ATC code: this one was lent.
                {"product": "APIXABAN TEVA", "source": "MHRA", "country": "United Kingdom",
                 "atc_code": "B01AF02", "therapeutic_category": "Anticoagulant",
                 "completion_source": "EMA", "registration_date": "2022-04-26T22:09:40Z",
                 "reference_smpc_url": "https://ema/eliquis"},
                # EMA publishes its own code.
                {"product": "Eliquis", "source": "EMA", "country": "EU", "atc_code": "B01AF02",
                 "therapeutic_category": "Anticoagulant"},
                # Spain publishes ATC codes, but this one was lent.
                {"product": "APIXABAN CINFA", "source": "Spain CIMA", "country": "Spain",
                 "atc_code": "B01AF02"},
            ])
            with repository.get_connection() as conn:
                # Field completion wrote these; the save path does not.
                conn.execute(
                    "UPDATE product_details SET completion_source = 'EMA', reference_smpc_url = 'https://ema/eliquis'"
                    " WHERE product IN ('APIXABAN TEVA', 'APIXABAN CINFA')"
                )
            revoke_borrowed_values()
            rows = self._rows()

        uk = rows["APIXABAN TEVA"]
        self.assertEqual((uk["atc_code"], uk["therapeutic_category"], uk["registration_date"]), ("", "", ""))
        self.assertFalse(uk["reference_smpc_url"])
        self.assertEqual(rows["Eliquis"]["atc_code"], "B01AF02")
        self.assertEqual(rows["APIXABAN CINFA"]["atc_code"], "")

    def test_canadian_rows_take_health_canadas_own_code_by_din(self):
        from services.data_repairs import restore_health_canada_from_register

        with tempfile.TemporaryDirectory() as directory, patch.object(repository, "DB_PATH", Path(directory) / "t.db"):
            repository.save_product_details([
                {"product": "GLUCOPHAGE", "source": "Health Canada", "country": "Canada",
                 "registration_number": "02099233"},
            ])
            result = restore_health_canada_from_register(register_rows=[
                {"registration_number": "02099233", "atc_code": "A10BA02", "therapeutic_category": "Metformin"},
            ])
            row = self._rows()["GLUCOPHAGE"]
        self.assertEqual((row["atc_code"], row["therapeutic_category"]), ("A10BA02", "Metformin"))
        self.assertEqual(result["atc_codes_restored_from_register"], 1)

    def test_live_results_share_nothing_across_regulators(self):
        from services.search_pipeline import propagate_molecule_fields

        rows = propagate_molecule_fields([
            {"source": "EMA", "substance": "apixaban", "product": "Eliquis", "atc_code": "B01AF02",
             "therapeutic_category": "Anticoagulant"},
            {"source": "MHRA", "substance": "apixaban", "product": "Eliquis"},
        ])
        self.assertEqual(rows[1].get("atc_code", ""), "")
        self.assertEqual(rows[1].get("therapeutic_category", ""), "")

    def test_mhra_rows_carry_no_registration_date(self):
        from sources.mhra import _extract_mhra_json_record

        row = _extract_mhra_json_record({
            "title": "Apixaban 5 mg Tablets", "substance_name": ["APIXABAN"], "pl_number": ["PL002892534"],
            "doc_type": "Spc", "metadata_storage_path": "https://blob/spc", "created": "2022-04-26T22:09:40Z",
        }, "apixaban")
        self.assertEqual(row["registration_date"], "")


class MiddleEastConnectorTests(unittest.TestCase):
    LEBANON_LISTING = """<table class="table"><thead><tr>
        <th><a href="#">ATC </a></th><th><a href="#">Name </a></th><th><a href="#">B/G </a></th>
        <th><a href="#">Ingredients </a></th><th><a href="#">Dosage </a></th><th><a href="#">Form </a></th>
        <th><a href="#">Price </a></th></tr></thead><tbody>
        <tr><td><a href="/en/Drugs/view/2029">C10BA06</a></td><td><a href="/en/Drugs/view/2029">LIPOCOMB </a></td>
        <td><a href="/en/Drugs/view/2029">B</a></td>
        <td><a href="/en/Drugs/view/2029">Rosuvastatin (calcium) - 10mg, Ezetimibe - 10mg</a></td>
        <td><a href="/en/Drugs/view/2029"></a></td><td><a href="/en/Drugs/view/2029">Capsule, hard</a></td>
        <td><a href="/en/Drugs/view/2029">1,811,500 L.L<!-- x --></a></td></tr></tbody></table>"""
    LEBANON_DETAIL = """<table class="table"><thead><tr><th>ATC</th><th>B/G</th><!-- <th></th> --><th>Ingredients</th>
        <th>code</th><th>Registration Nb</th><th>Name</th><th>Dosage</th><th>Presentation</th><th>Form</th>
        <th>Route</th><th>Agent</th><th>Laboratory</th><th>Country</th><th>Price</th><th>Pharmacist Margin</th>
        <th>Stratum</th><th>Responsible Party Name</th><th>Responsible Party Country</th><th>Exch_date</th>
        <th>%SUBSIDY</th></tr></thead><tbody><tr><td>C10BA06</td><td>B</td><!-- <td></td> -->
        <td>Rosuvastatin (calcium) - 10mg, Ezetimibe - 10mg</td><td>10635</td><td>92520/1</td><td>LIPOCOMB</td>
        <td></td><td>30</td><td>Capsule, hard</td><td>Oral</td><td>Khalil Fattal &amp; Fils S.A.L.</td>
        <td>Egis Pharmaceuticals PLC</td><td>Hungary</td><td>1,811,500 L.L</td><td>23.08</td><td>B</td>
        <td>Les Laboratoires Servier</td><td>France</td><td>9/3/2026</td><td></td></tr></tbody></table>"""

    def test_a_lebanese_registration_takes_its_drug_page_details(self):
        from sources import lebanon_moph

        def post(url, data=None, **_kwargs):
            return MagicMock(text=self.LEBANON_LISTING, raise_for_status=lambda: None)

        def get(url, **_kwargs):
            return MagicMock(text=self.LEBANON_DETAIL if "view" in url else "", raise_for_status=lambda: None)

        with patch("requests.Session.post", side_effect=post), patch("requests.Session.get", side_effect=get):
            rows = lebanon_moph.run_lebanon_moph_search("ezetimibe")

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["product"], "LIPOCOMB 10mg/10mg")
        self.assertEqual(row["substance"], "Rosuvastatin (calcium); Ezetimibe")
        self.assertEqual(row["registration_number"], "92520/1")
        self.assertEqual(row["company"], "Les Laboratoires Servier")
        self.assertEqual((row["manufacturer_name"], row["manufacturer_country"]), ("Egis Pharmaceuticals PLC", "Hungary"))
        self.assertEqual((row["atc_code"], row["pack_size"], row["authorisation_scope"]), ("C10BA06", "30", "Brand"))
        self.assertTrue(row_relevant_to_substance(row, "rosuvastatin + ezetimibe"))

    def test_the_sfda_list_and_details_pages_are_read(self):
        from sources.sfda_saudi import build_sfda_rows, parse_details, parse_list_page

        listing = """<table><tr><th>Scientific Name</th></tr><tr><td>VALSARTAN,HYDROCHLOROTHIAZIDE</td>
            <td>CO-VALISTA</td><td>160,25</td><td></td><td>30.3</td>
            <td><a href="/en/details_data?nid=17582&amp;id=1661&amp;page=2">Details</a></td></tr></table>"""
        records = parse_list_page(listing)
        self.assertEqual(records[0]["details"], "/en/details_data?nid=17582&id=1661&page=2")
        _match, row = next(build_sfda_rows(records, "2026-09-16"))
        self.assertEqual((row["product"], row["strength"], row["price"]), ("CO-VALISTA", "160,25", "30.3 SAR"))
        self.assertEqual(row["product_url"], "https://www.sfda.gov.sa/en/details_data?nid=17582&id=1661&page=2")

        details = parse_details(
            "<table><tbody><tr><th>Register Number</th><td>316-212-14</td></tr>"
            "<tr><th>Strength Unit</th><td>Array</td></tr><tr><th>ATC Code 1</th><td>C09DA03</td></tr></tbody></table>"
        )
        self.assertEqual(details, {"Register Number": "316-212-14", "Strength Unit": "", "ATC Code 1": "C09DA03"})


class RomanianPackTests(unittest.TestCase):
    PACKS = [
        ("16519/2026/01", "Cutie cu blist. PVC-PVDC/Al x 10 compr. film."),
        ("16519/2026/02", "Cutie cu blist. PVC-PVDC/Al x 12 compr. film."),
        ("16519/2026/03", "Cutie cu blist. doze unitare PVC-PVDC/Al x 20x1 compr. film."),
    ]

    def _records(self):
        return [
            {"product": "ABATIXENT 2,5 mg", "strength": "2,5mg", "dosage_form": "Film-coated tablet",
             "company": "SANDOZ", "registration_number": number, "pack_size": pack,
             "product_url": f"https://anm/{number}", "source": "ANMDMR Romania", "country": "Romania",
             "atc_code": "B01AF02"}
            for number, pack in self.PACKS
        ] + [{"product": "ABATIXENT 5 mg", "strength": "5mg", "dosage_form": "Film-coated tablet",
              "company": "SANDOZ", "registration_number": "16520/2026/01", "pack_size": "x 10 compr.",
              "product_url": "https://anm/16520", "source": "ANMDMR Romania", "country": "Romania"}]

    def test_packs_of_one_product_become_one_row_listing_every_pack(self):
        from sources.romania_anmdmr import fold_pack_registrations

        rows = fold_pack_registrations(self._records())
        self.assertEqual([row["product"] for row in rows], ["ABATIXENT 2,5 mg", "ABATIXENT 5 mg"])
        self.assertEqual(rows[0]["registration_number"], "16519/2026/01-03")
        self.assertEqual(
            rows[0]["pack_size"],
            "Cutie cu blist. PVC-PVDC/Al x 10 / 12 compr. film.; "
            "Cutie cu blist. doze unitare PVC-PVDC/Al x 20x1 compr. film.",
        )
        self.assertEqual(rows[0]["product_url"], "https://anm/16519/2026/01")
        self.assertEqual(rows[1]["registration_number"], "16520/2026/01")

    def test_stored_pack_rows_are_merged_and_a_later_search_updates_the_same_row(self):
        from services.data_repairs import fold_romanian_pack_rows
        from sources.romania_anmdmr import fold_pack_registrations

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(repository, "DB_PATH", Path(directory) / "t.db"), \
                patch.object(repository, "PRODUCT_DETAILS_SEED_PATH", Path("no-seed.jsonl")):
            repository.save_product_details(self._records())
            result = fold_romanian_pack_rows()
            repository.save_product_details(fold_pack_registrations(self._records()))
            with repository.get_connection() as conn:
                rows = [dict(row) for row in conn.execute(
                    "SELECT product, registration_number FROM product_details ORDER BY product"
                )]
        self.assertEqual(result["pack_rows_merged_away"], 2)
        self.assertEqual(rows, [
            {"product": "ABATIXENT 2,5 mg", "registration_number": "16519/2026/01-03"},
            {"product": "ABATIXENT 5 mg", "registration_number": "16520/2026/01"},
        ])


class WeeklyMhraLinkJobTests(unittest.TestCase):
    def test_each_run_backs_up_first_and_keeps_the_newest_four_backups(self):
        import sqlite3
        import time

        from tools.weekly_mhra_links import back_up

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "store.db"
            connection = sqlite3.connect(db)
            connection.execute("CREATE TABLE t (x)")
            connection.commit()
            connection.close()
            backups = Path(directory) / "backups"
            backups.mkdir()
            for day in range(1, 6):
                (backups / f"weekly-mhra-links-2026010{day}-000000.db").write_bytes(b"")
            (backups / "pharmasearch-before-vendor-cleanup.db").write_bytes(b"keep")
            time.sleep(0.01)
            made = back_up(db, backups, kept=4)
            weekly = sorted(path.name for path in backups.glob("weekly-mhra-links-*.db"))

            self.assertIn(made.name, weekly)
            self.assertEqual(len(weekly), 4)
            self.assertTrue((backups / "pharmasearch-before-vendor-cleanup.db").exists())


class ExcelControlCharacterTests(unittest.TestCase):
    def test_text_from_a_pdf_with_control_characters_still_exports(self):
        from export_service import write_excel_export

        leaflet_text = "Marketing Authorization Holder and \x0b Lyme disease \x03"
        with tempfile.TemporaryDirectory() as directory, patch("export_service.EXPORT_DIR", Path(directory)):
            path = write_excel_export("amoxicillin", [{
                "source": "MHRA", "product": "Amoxicillin 500 mg", "manufacturer_name": leaflet_text,
                "evidence": [{"field_name": "manufacturer_name", "value": leaflet_text}],
            }])
            self.assertTrue(path.exists())


class UsDocumentLinkTests(unittest.TestCase):
    def test_a_us_application_links_its_drugs_at_fda_page_as_the_assessment_report(self):
        from services.result_formatter import formatted_result_row

        row = formatted_result_row({
            "source": "FDA", "product": "Metformin 500 mg (NDC 0000-0000)", "registration_number": "ANDA076002",
            "smpc_url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=abc",
        })
        self.assertEqual(
            row["assessment_report_url"],
            "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo=076002",
        )

    def test_monographs_biologics_and_other_regulators_get_no_drugs_at_fda_link(self):
        from services.field_availability import drugs_at_fda_url

        for item in (
            {"source": "FDA", "registration_number": "M012"},
            {"source": "FDA", "registration_number": "BLA125856"},
            {"source": "MHRA", "registration_number": "NDA021202"},
        ):
            self.assertEqual(drugs_at_fda_url(item), "", item)

    def test_the_us_pil_is_said_to_be_in_the_label(self):
        from services.field_availability import US_PIL_IN_LABEL, missing_field_value

        self.assertEqual(missing_field_value({"source": "FDA", "smpc_url": "https://dailymed"}, "pil_url"), US_PIL_IN_LABEL)


class AccentedSearchTests(unittest.TestCase):
    def test_an_accented_name_matches_the_plain_spelling(self):
        from services.search_pipeline import row_relevant_to_substance

        self.assertTrue(row_relevant_to_substance({"product": "CEFALEXINE BIOGARAN 500 mg"}, "céfalexine"))

    def test_an_accented_name_also_searches_the_plain_and_english_spellings(self):
        terms = get_substance_search_terms("céfalexine")
        self.assertIn("cefalexine", terms)
        self.assertIn("cefalexin", terms)
        self.assertIn("cephalexin", terms)


class IndonesianNonMedicineTests(unittest.TestCase):
    def test_cosmetic_food_and_supplement_notifications_are_recognised(self):
        from sources.regional_live import bpom_is_non_medicine

        for number in ("NA18230105897", "NC24230100028", "MD 234512", "SD151234", "SI0123"):
            self.assertTrue(bpom_is_non_medicine(number), number)
        for number in ("GKL1234567", "DKL0101010", "DBL9876543"):
            self.assertFalse(bpom_is_non_medicine(number), number)


class VendorDataRepairTests(unittest.TestCase):
    def setUp(self):
        seed = patch.object(repository, "PRODUCT_DETAILS_SEED_PATH", Path("no-seed.jsonl"))
        seed.start()
        self.addCleanup(seed.stop)

    def _database(self, directory):
        return patch.object(repository, "DB_PATH", Path(directory) / "test.db")

    def _products(self):
        with repository.get_connection() as conn:
            return {row[0]: dict(row) for row in conn.execute(
                "SELECT product, * FROM product_details"
            )}

    def test_rows_no_buyer_should_see_are_removed(self):
        from services.data_repairs import remove_non_pharmaceutical_rows

        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"product": "Glucophage", "substance": "metformin", "source": ""},
                {"product": "Arnica Montana Boiron", "source": "France BDPM", "country": "France",
                 "substance": "ARNICA MONTANA POUR PRÉPARATIONS HOMÉOPATHIQUES"},
                {"product": "Allergy Relief", "source": "FDA", "country": "United States",
                 "substance": "sodium chloride", "strength": "12 [hp_X]"},
                {"product": "Daily Toner", "source": "BPOM Indonesia", "country": "Indonesia",
                 "registration_number": "NA18221205669"},
                {"product": "Amlodipine 5 mg", "source": "BPOM Indonesia", "country": "Indonesia",
                 "registration_number": "GKL1234567"},
            ])
            result = remove_non_pharmaceutical_rows()
            remaining = sorted(self._products())
        self.assertEqual(remaining, ["Amlodipine 5 mg"])
        self.assertEqual(result["homeopathic"], 2)

    def test_identical_rows_are_merged_and_the_keeper_takes_what_only_a_copy_had(self):
        from services.data_repairs import merge_identical_rows

        base = {"product": "Metformin 500 mg", "source": "FDA", "country": "United States",
                "registration_number": "ANDA123", "strength": "500 mg"}
        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            with repository.get_connection() as conn:
                repository.initialize_database()
                for extra in ({"atc_code": "A10BA02"}, {"smpc_url": "https://label"}):
                    record = {**base, **extra}
                    conn.execute(
                        f"INSERT INTO product_details ({', '.join(record)}) VALUES ({', '.join('?' for _ in record)})",
                        tuple(record.values()),
                    )
            result = merge_identical_rows()
            with repository.get_connection() as conn:
                rows = [dict(row) for row in conn.execute("SELECT atc_code, smpc_url FROM product_details")]
        self.assertEqual(result["copies_removed"], 1)
        self.assertEqual(rows, [{"atc_code": "A10BA02", "smpc_url": "https://label"}])

    def test_canadian_rows_point_at_the_record_that_still_answers(self):
        from services.data_repairs import relink_health_canada_products

        old = "https://health-products.canada.ca/dpd-bdpp/info.do?lang=en&code=12345"
        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"product": "APO-METFORMIN", "source": "Health Canada", "country": "Canada",
                 "product_url": old, "source_url": old},
            ])
            relink_health_canada_products()
            row = self._products()["APO-METFORMIN"]
        expected = "https://health-products.canada.ca/api/drug/drugproduct/?lang=en&type=json&id=12345"
        self.assertEqual(row["product_url"], expected)
        self.assertEqual(row["source_url"], expected)

    def test_stored_markup_goes_but_the_registry_wording_stays(self):
        from services.data_repairs import clean_stored_markup

        with tempfile.TemporaryDirectory() as directory, self._database(directory):
            repository.save_product_details([
                {"product": "Hydrocortisone &amp; Zinc Cream", "source": "BPOM Indonesia",
                 "country": "Indonesia", "atc_code": "Not yet assigned", "strength": "N/A",
                 "status": "Berlaku", "substance": "salmeterol;fluticasone"},
            ])
            clean_stored_markup()
            row = next(iter(self._products().values()))
        self.assertEqual(row["product"], "Hydrocortisone & Zinc Cream")
        self.assertEqual(row["atc_code"], "")
        self.assertEqual(row["strength"], "")
        self.assertEqual(row["status"], "Berlaku")
        self.assertEqual(row["substance"], "salmeterol;fluticasone")


class OpenDataRegisterTests(unittest.TestCase):
    def test_a_singapore_product_keeps_every_ingredient_strength_and_maker(self):
        from sources.open_data_registers import build_singapore_rows

        record = {
            "LicenceNo": "SIN14567P", "Productname": "ATOZET TABLET 10MG/10MG",
            "Licenseholder": "ORGANON SINGAPORE PTE. LTD.", "Approvaldate": "5/1/2015",
            "Forensicclassification": "Prescription Only", "ATCCode": "C10BA05",
            "Dosageform": "TABLET, FILM COATED", "RouteofAdministration": "ORAL",
            "Manufacturer": "MSD INTERNATIONAL GMBH&&ORGANON PHARMA (UK) LIMITED",
            "Countryofmanufacturer": "PUERTO RICO&&UNITED KINGDOM",
            "Activeingredients": "Atorvastatin Calcium&&Ezetimibe", "Strength": "10mg&&10mg",
        }
        (match, row), = build_singapore_rows([record], "2026-09-17")
        self.assertEqual(row["strength"], "10mg/10mg")
        self.assertEqual(row["active_substance"], "Atorvastatin Calcium; Ezetimibe")
        self.assertEqual(row["manufacturer_name"], "MSD INTERNATIONAL GMBH; ORGANON PHARMA (UK) LIMITED")
        self.assertEqual(row["manufacturer_country"], "Puerto Rico; United Kingdom")
        self.assertEqual((row["company"], row["registration_date"]), ("ORGANON SINGAPORE PTE. LTD.", "2015-01-05"))
        self.assertTrue(row_relevant_to_substance(row, "atorvastatin + ezetimibe"))

    def test_a_ukrainian_registration_is_read_into_english(self):
        from sources.open_data_registers import build_ukraine_rows

        record = {
            "Торгівельне найменування": "СЕВЕЛАМЕР-ВІСТА",
            "Міжнародне непатентоване найменування": "Sevelamer",
            "Форма випуску": "таблетки, вкриті плівковою оболонкою, по 800 мг; по 10 таблеток у блістері",
            "Склад (діючі)": "1 таблетка містить севеламеру карбонату 800 мг",
            "Код АТС 1": "V03AE02",
            "Заявник: назва українською": "Містрал Кепітал Менеджмент Лімітед",
            "Заявник: країна": "Велика Британія",
            "Виробник 1: назва українською": "Сінтон Хіспанія, С.Л. (виробництво, випуск серії)",
            "Виробник 1: країна ": "Іспанія",
            "Виробник 2: назва українською": "Роттендорф Фарма ГмбХ",
            "Виробник 2: країна ": "Німеччина",
            "Номер Реєстраційного посвідчення": "UA/19000/01/01",
            "Дата початку дії": "01.02.2022",
            "Дата закінчення": "необмежений",
            "Дострокове припинення": "Ні",
            "Гомеопатичний ЛЗ": "Ні",
        }
        (match, row), = build_ukraine_rows([record], "2026-09-17")
        self.assertEqual(row["active_substance"], "Sevelamer carbonate")
        self.assertEqual((row["strength"], row["dosage_form"]), ("800 mg", "Film-coated tablets"))
        self.assertEqual(row["ma_holder_country"], "United Kingdom")
        self.assertEqual(row["manufacturer_name"], "Сінтон Хіспанія, С.Л.; Роттендорф Фарма ГмбХ")
        self.assertEqual(row["manufacturer_country"], "Spain; Germany")
        self.assertEqual(row["manufacturers"][0]["role"], "виробництво, випуск серії")
        self.assertEqual((row["status"], row["registration_date"], row["expiry_date"]), ("Registered", "2022-02-01", "Unlimited"))
        self.assertTrue(row_relevant_to_substance(row, "sevelamer carbonate"))

        record["Дострокове припинення"] = "Так"
        (_match, stopped), = build_ukraine_rows([record], "2026-09-17")
        self.assertEqual(stopped["status"], "Registration terminated early")

    def test_an_irish_product_takes_its_holder_basis_and_strength(self):
        from sources.open_data_registers import build_ireland_rows

        record = {
            "LicenceNumber": "PA22683/004/001",
            "ProductName": "Ezetimibe/Atorvastatin 10 mg/10 mg film-coated tablets",
            "PAHolder": "Althera Laboratories Limited",
            "AuthorisedDate": "17/04/2025",
            "MarketInfo": "Marketed",
            "DosageForm": "Film-coated tablet",
            "ATCs": ["C10BA05"],
            "LegalBasis": "Generic application (Article 10(1) of Directive No 2001/83/EC)",
            "RoutesOfAdministration": ["Oral use"],
            "ActiveSubstances": ["Ezetimibe", "Atorvastatin calcium trihydrate"],
        }
        (match, row), = build_ireland_rows([record], "2026-09-17")
        self.assertEqual((row["company"], row["status"], row["authorisation_scope"]),
                         ("Althera Laboratories Limited", "Marketed", "Generic"))
        self.assertEqual((row["atc_code"], row["registration_date"]), ("C10BA05", "2025-04-17"))
        self.assertIn("10 mg", row["strength"])
        self.assertTrue(row_relevant_to_substance(row, "atorvastatin + ezetimibe"))

    def test_a_dropped_download_is_tried_again(self):
        from sources import open_registers

        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"data"]
        calls = []

        def get(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise requests.ConnectionError("reset")
            return response

        with tempfile.TemporaryDirectory() as folder, \
                patch("sources.open_registers.requests.get", side_effect=get), \
                patch("sources.open_registers.time.sleep"):
            target = Path(folder) / "file"
            open_registers.download_file(open_registers.ITALY, "https://example.org/x", target)
            self.assertEqual(target.read_bytes(), b"data")
        self.assertEqual(len(calls), 2)


class TaiwanRegisterTests(unittest.TestCase):
    def _workdir(self, folder):
        import csv as csv_module
        import io
        import zipfile

        licences = io.StringIO()
        fields = ["許可證字號", "註銷狀態", "註銷日期", "有效日期", "發證日期", "中文品名", "英文品名", "劑型",
                  "藥品類別", "主成分略述", "申請商名稱", "申請商統一編號", "製造商名稱", "製造廠國別", "製程"]
        writer = csv_module.DictWriter(licences, fieldnames=fields)
        writer.writeheader()
        base = {"許可證字號": "衛署藥輸字第025733號", "註銷狀態": "", "註銷日期": "", "有效日期": "2030/06/25",
                "發證日期": "2012/06/25", "中文品名": "磷減能口服懸液用粉劑", "英文品名": "Renvela powder for oral suspension",
                "劑型": "口服懸液用粉劑", "藥品類別": "須由醫師處方使用",
                "主成分略述": "ANHYDROUS SEVELAMER CARBONATE;;ANHYDROUS SEVELAMER CARBONATE",
                "申請商名稱": "賽諾菲股份有限公司", "申請商統一編號": "97168356"}
        writer.writerow({**base, "製造商名稱": "Rovi Pharma Industrial Services S.A.", "製造廠國別": "ES", "製程": "製造"})
        writer.writerow({**base, "製造商名稱": "永信藥品工業股份有限公司台中幼獅廠", "製造廠國別": "TW", "製程": "分包裝"})
        writer.writerow({"許可證字號": "衛部藥製字第058140號", "註銷狀態": "已註銷", "註銷日期": "2023/12/03",
                         "有效日期": "2023/12/03", "發證日期": "2013/12/03", "中文品名": "磷可停",
                         "英文品名": "Phosout F.C. Tablets 800mg", "劑型": "膜衣錠", "藥品類別": "須由醫師處方使用",
                         "主成分略述": "SEVELAMER HYDROCHLORIDE", "申請商名稱": "美時化學製藥股份有限公司",
                         "申請商統一編號": "11456110", "製造商名稱": "未登記藥廠", "製造廠國別": "TW", "製程": ""})
        path = Path(folder)
        with zipfile.ZipFile(path / "tfda_licences.zip", "w") as archive:
            archive.writestr("36_2.csv", "﻿" + licences.getvalue())
        (path / "trade_names.csv").write_text(
            "統一編號,廠商中文名稱,廠商英文名稱\n"
            "97168356,賽諾菲股份有限公司,SANOFI TAIWAN CO. LTD.\n"
            "56065601,永信藥品工業股份有限公司,YUNG SHIN PHARMACEUTICAL IND. CO. LTD.\n",
            encoding="utf-8",
        )
        return path

    def test_a_licence_takes_english_names_for_its_holder_and_plants(self):
        from sources import taiwan_fda

        with tempfile.TemporaryDirectory() as folder:
            records = list(taiwan_fda.read_records(taiwan_fda.TFDA_TAIWAN, self._workdir(folder)))
        rows = {row["product"]: row for _match, row in taiwan_fda.build_rows(records, "2026-09-17")}
        renvela = rows["Renvela powder for oral suspension"]
        self.assertEqual(renvela["company"], "SANOFI TAIWAN CO. LTD.")
        self.assertEqual(renvela["registration_number"], "DOH-PI 025733")
        self.assertEqual((renvela["dosage_form"], renvela["classification"]), ("Powder for oral suspension", "Prescription only"))
        self.assertEqual(renvela["active_substance"], "ANHYDROUS SEVELAMER CARBONATE")
        self.assertEqual(
            renvela["manufacturer_name"],
            "Rovi Pharma Industrial Services S.A.; YUNG SHIN PHARMACEUTICAL IND. CO. LTD. (manufacturing plant)",
        )
        self.assertEqual(renvela["manufacturer_country"], "Spain; Taiwan")
        self.assertEqual((renvela["status"], renvela["expiry_date"]), ("Valid", "2030-06-25"))
        self.assertTrue(row_relevant_to_substance(renvela, "sevelamer carbonate"))

        phosout = rows["Phosout F.C. Tablets 800mg"]
        self.assertEqual(phosout["status"], "Cancelled (2023-12-03)")
        self.assertEqual(phosout["registration_number"], "MOHW-PM 058140")
        # No registered English name: the Chinese name is kept, not invented.
        self.assertEqual(phosout["company"], "美時化學製藥股份有限公司")
        self.assertEqual(phosout["dosage_form"], "Film-coated tablet")


class WebSecurityTests(unittest.TestCase):
    def _request(self, scheme="http", headers=None, host="127.0.0.1"):
        request = MagicMock()
        request.url.scheme = scheme
        request.headers = headers or {}
        request.client.host = host
        return request

    def test_https_is_recognised_through_a_declared_proxy_only(self):
        from services import web_security

        forwarded = self._request(headers={"x-forwarded-proto": "https"})
        with patch.dict(os.environ, {"PHARMASEARCH_TRUSTED_PROXY": "", "PHARMASEARCH_FORCE_HTTPS": ""}, clear=False):
            # Anyone can send the header, so it counts for nothing on its own.
            self.assertFalse(web_security.request_is_secure(forwarded))
        with patch.dict(os.environ, {"PHARMASEARCH_TRUSTED_PROXY": "1"}, clear=False):
            self.assertTrue(web_security.request_is_secure(forwarded))
            self.assertFalse(web_security.request_is_secure(self._request(headers={"x-forwarded-proto": "http"})))
        with patch.dict(os.environ, {"PHARMASEARCH_FORCE_HTTPS": "1", "PHARMASEARCH_TRUSTED_PROXY": ""}, clear=False):
            self.assertTrue(web_security.request_is_secure(self._request()))

    def test_the_visitors_address_comes_from_the_proxy_when_there_is_one(self):
        from services import web_security

        request = self._request(headers={"x-forwarded-for": "203.0.113.9, 10.0.0.2"}, host="10.0.0.2")
        with patch.dict(os.environ, {"PHARMASEARCH_TRUSTED_PROXY": ""}, clear=False):
            self.assertEqual(web_security.client_address(request), "10.0.0.2")
        with patch.dict(os.environ, {"PHARMASEARCH_TRUSTED_PROXY": "1"}, clear=False):
            self.assertEqual(web_security.client_address(request), "10.0.0.2")

    def test_production_refuses_to_start_without_its_secrets(self):
        from services import web_security

        settings = {
            "PHARMASEARCH_ENV": "production", "PHARMASEARCH_ADMIN_USERNAME": "",
            "PHARMASEARCH_ADMIN_PASSWORD_HASH": "", "PHARMASEARCH_SESSION_SECRET": "",
            "PHARMASEARCH_ALLOWED_HOSTS": "", "PHARMASEARCH_FORCE_HTTPS": "", "PHARMASEARCH_TRUSTED_PROXY": "",
            "RENDER_EXTERNAL_HOSTNAME": "",
        }
        with patch.dict(os.environ, settings, clear=False):
            with self.assertRaises(RuntimeError) as refused:
                web_security.check_production_configuration()
        said = str(refused.exception)
        for setting in ("PHARMASEARCH_ADMIN_USERNAME", "PHARMASEARCH_SESSION_SECRET", "PHARMASEARCH_ALLOWED_HOSTS"):
            self.assertIn(setting, said)

        ready = {
            "PHARMASEARCH_ENV": "production", "PHARMASEARCH_ADMIN_USERNAME": "admin",
            "PHARMASEARCH_ADMIN_PASSWORD_HASH": "scrypt$16384$8$1$c2FsdA$ZGlnZXN0",
            "PHARMASEARCH_SESSION_SECRET": "x" * 40, "PHARMASEARCH_ALLOWED_HOSTS": "search.example.com",
            "PHARMASEARCH_FORCE_HTTPS": "1",
        }
        with patch.dict(os.environ, ready, clear=False):
            web_security.check_production_configuration()

    def test_render_supplies_the_host_name_when_none_is_configured(self):
        from services import web_security

        render = {"PHARMASEARCH_ALLOWED_HOSTS": "", "RENDER_EXTERNAL_HOSTNAME": "pharma-test.onrender.com"}
        with patch.dict(os.environ, render, clear=False):
            self.assertEqual(web_security.allowed_hosts(), ["pharma-test.onrender.com"])
        both = {"PHARMASEARCH_ALLOWED_HOSTS": "search.example.com", "RENDER_EXTERNAL_HOSTNAME": "pharma-test.onrender.com"}
        with patch.dict(os.environ, both, clear=False):
            self.assertEqual(web_security.allowed_hosts(), ["search.example.com"])

    def test_the_health_check_is_answered_under_any_host_and_nothing_else_is(self):
        import asyncio

        from fastapi import FastAPI
        from services import web_security

        def status(app, path, host):
            sent = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                "headers": [(b"host", host.encode())], "client": ("10.0.0.1", 1), "server": ("10.0.0.7", 10000),
            }
            asyncio.run(app(scope, receive, send))
            return next(m["status"] for m in sent if m["type"] == "http.response.start")

        with patch.dict(os.environ, {"PHARMASEARCH_ALLOWED_HOSTS": "search.example.com"}, clear=False):
            app = FastAPI()
            web_security.add_security(app)

            @app.get("/healthz")
            def healthz():
                return {"status": "ok"}

            @app.get("/page")
            def page():
                return {"page": True}

            self.assertEqual(status(app, "/healthz", "10.0.0.7:10000"), 200)
            self.assertEqual(status(app, "/page", "10.0.0.7:10000"), 400)
            self.assertEqual(status(app, "/page", "search.example.com"), 200)

    def test_development_starts_without_them(self):
        from services import web_security

        with patch.dict(os.environ, {"PHARMASEARCH_ENV": ""}, clear=False):
            web_security.check_production_configuration()

    def test_the_headers_name_what_a_page_may_load(self):
        from services.web_security import SECURITY_HEADERS

        self.assertEqual(SECURITY_HEADERS["X-Frame-Options"], "DENY")
        self.assertEqual(SECURITY_HEADERS["X-Content-Type-Options"], "nosniff")
        policy = SECURITY_HEADERS["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("form-action 'self'", policy)
        # The pages load Bootstrap from jsDelivr and nothing else off-site.
        self.assertIn("https://cdn.jsdelivr.net", policy)


class RussiaRegisterTests(unittest.TestCase):
    RECORD = {
        "number": "ЛП-№(000500)-(РГ-RU)", "registered": "01.02.2022", "expires": "", "cancelled": "",
        "holder": "Акционерное общество \"Санофи Россия\" (АО \"Санофи Россия\")", "holder_country": "Россия",
        "trade_name": "Зенон®", "inn": "Розувастатин+Эзетимиб",
        "forms": "таблетки, покрытые пленочной оболочкой, 10 мг+10 мг - блистеры (10) - пачки картонные - По рецепту; ",
        "production": "Производитель (Все стадии производства),АО \"КРКА, д.д., Ново место\", Шмарьешка цеста 6, "
                      "8501 Ново место, Словения_x000D_\nВыпускающий контроль качества,Санофи Винтроп Индастриа, "
                      "1 рю де ла Вьерж, Франция",
        "group": "", "state": "Registered",
    }

    def test_a_russian_registration_is_read_and_found_by_its_english_inn(self):
        from sources.grls_russia import build_rows, fold

        (match, row), = build_rows([dict(self.RECORD)], "2026-09-17")
        self.assertEqual(row["company"], 'JSC "Sanofi Rossiya"')
        self.assertEqual((row["strength"], row["dosage_form"]), ("10 mg+10 mg", "Film-coated tablets"))
        self.assertEqual(row["manufacturer_name"], 'JSC "KRKA, d.d., Novo mesto"')
        self.assertEqual(row["manufacturer_country"], "Slovenia")
        self.assertEqual(row["manufacturers"][1]["role"], "Batch release")
        self.assertEqual((row["registration_date"], row["status"]), ("2022-02-01", "Registered"))
        self.assertIn(f" {fold('rosuvastatin')} ", match)
        self.assertTrue(row_relevant_to_substance(row, "rosuvastatin + ezetimibe"))
        self.assertFalse(row_relevant_to_substance(row, "atorvastatin + ezetimibe"))

    def test_the_folded_spelling_joins_russian_and_english_inns(self):
        from sources.grls_russia import fold, latin
        from sources.open_registers import inn_tokens

        for russian, english in (("Амоксициллин", "amoxicillin"), ("Гидрохлоротиазид", "hydrochlorothiazide"),
                                 ("Кветиапин", "quetiapine"), ("Эзетимиб", "ezetimibe")):
            self.assertEqual([fold(t) for t in inn_tokens(latin(russian))], [fold(t) for t in inn_tokens(english)], russian)


class ConnectorAuditFixTests(unittest.TestCase):
    def test_drugs_at_fda_lists_discontinued_applications_with_their_holder(self):
        from sources.fda_drugsfda import build_rows

        record = {
            "application_number": "NDA213072",
            "sponsor_name": "ALTHERA PHARMS",
            "submissions": [
                {"submission_type": "SUPPL", "submission_status": "AP", "submission_status_date": "20230101"},
                {"submission_type": "ORIG", "submission_status": "AP", "submission_status_date": "20210323"},
            ],
            "products": [{
                "product_number": "001", "brand_name": "ROSZET", "dosage_form": "TABLET", "route": "ORAL",
                "marketing_status": "Discontinued", "te_code": "", "reference_drug": "Yes",
                "active_ingredients": [
                    {"name": "EZETIMIBE", "strength": "10MG"},
                    {"name": "ROSUVASTATIN CALCIUM", "strength": "EQ 5MG BASE **Federal Register determination that product was not discontinued**"},
                ],
            }],
        }
        (match, row), = build_rows([record], "2026-09-17")
        self.assertEqual(row["product"], "ROSZET 10MG/EQ 5MG BASE")
        self.assertEqual((row["company"], row["status"], row["authorisation_scope"]), ("ALTHERA PHARMS", "Discontinued", "NDA"))
        self.assertEqual((row["registration_date"], row["application_number"]), ("2021-03-23", "NDA213072"))
        self.assertTrue(row["product_url"].endswith("ApplNo=213072"))
        self.assertTrue(row_relevant_to_substance(row, "rosuvastatin + ezetimibe"))

    def test_a_uk_licence_takes_the_holder_its_company_number_belongs_to(self):
        from sources import mhra

        stored = [
            ("PL 17780/0880", "Zentiva Pharma UK Limited"),
            ("PL 17780/0123", "Zentiva Pharma UK Limited"),
            ("PL 17780/0456", "and a different medicine for your diabetes. and"),
            ("PL 04569/1623", "Generics [UK] Limited t/a Mylan"),
            ("PL 04569/1000", "Generics [UK] Limited t/a Viatris"),
            ("PL 99999/0001", "Alpha Pharma Ltd"),
            ("PL 99999/0002", "Beta Pharma Ltd"),
        ]
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = stored
        connection = MagicMock()
        connection.__enter__.return_value = conn
        mhra._holder_cache.clear()
        try:
            with patch("repository.get_connection", return_value=connection):
                rows = mhra._with_holders([
                    {"registration_number": "PL 17780/0999", "company": ""},
                    {"registration_number": "PL 04569/9999", "company": ""},
                    {"registration_number": "PL 99999/0003", "company": ""},
                    {"registration_number": "PLPI 18799/3699", "company": ""},
                ])
        finally:
            mhra._holder_cache.clear()
        self.assertEqual([row["company"] for row in rows], [
            "Zentiva Pharma UK Limited", "Generics [UK] Limited", "", "",
        ])

    def test_a_manufacturer_is_shown_without_the_address_written_after_it(self):
        from services.vendor_display import clean_manufacturer

        self.assertEqual(
            clean_manufacturer("Sanoﬁ Winthrop Industrie; 1 rue de la Vierge; Ambares et Lagrave; France"),
            "Sanofi Winthrop Industrie",
        )
        self.assertEqual(
            clean_manufacturer("Pharmathen International S. A; Industrial Park Sapes; Rodopi 69300; Greece; Pharmathen S. A ."),
            "Pharmathen International S. A; Pharmathen S. A .",
        )
        self.assertEqual(
            clean_manufacturer(
                "and Product Licence Holder Manufactured by: Sanofi Winthrop Industrie, 1 rue de la Vierge, France. "
                "OR Genzyme Ireland Limited, IDA Industrial Park, Waterford, Ireland. Procured from within the EU by "
                "Product Licence holder: Star Pharmaceuticals Ltd, 5 Sandridge Close, Harrow"
            ),
            "Sanofi Winthrop Industrie; Genzyme Ireland Limited",
        )
        self.assertEqual(clean_manufacturer("Laboratorios Cinfa, S.A."), "Laboratorios Cinfa, S.A.")

    def test_an_sfda_details_page_for_another_product_is_not_used(self):
        from sources import sfda_saudi

        page = ("<table><tr><th>Trade Name</th><td>ZETRON 250 MG CAPSULE</td></tr>"
                "<tr><th>Register Number</th><td>0606222134</td></tr><tr><th>ATC Code 1</th><td>J01FA10</td></tr></table>")
        rows = [{"product": "Lamsev 800", "product_url": "https://www.sfda.gov.sa/en/details_data?id=1&page=2"}]
        with patch("requests.Session.get", return_value=MagicMock(text=page, raise_for_status=lambda: None)):
            result = sfda_saudi._with_details(rows)
        self.assertNotIn("registration_number", result[0])
        self.assertNotIn("atc_code", result[0])


class SearchedStrengthTests(unittest.TestCase):
    def test_a_strength_after_the_molecule_is_not_part_of_its_name(self):
        from services.search_pipeline import split_searched_strength

        self.assertEqual(split_searched_strength("sevelamer carbonate 800"), ("sevelamer carbonate", "800"))
        self.assertEqual(split_searched_strength("sevelamer carbonate 800 MG"), ("sevelamer carbonate", "800 mg"))
        self.assertEqual(split_searched_strength("vitamin b12"), ("vitamin b12", ""))
        self.assertEqual(split_searched_strength("atorvastatin + ezetimibe"), ("atorvastatin + ezetimibe", ""))

    def test_rows_are_kept_at_that_strength_in_any_unit_or_when_none_is_published(self):
        from services.search_pipeline import matches_searched_strength

        for strength in ("800MG/TAB", "0.8 G", ".8 g", "800 mg/930mg", ""):
            self.assertTrue(matches_searched_strength({"strength": strength, "product": "Renvela"}, "800"), strength)
        for strength in ("2.4 g", "1,6G/SACHET", "400MG/TAB"):
            self.assertFalse(matches_searched_strength({"strength": strength}, "800"), strength)

    def test_a_concentration_after_the_molecule_is_read_as_a_strength(self):
        from services.search_pipeline import split_searched_strength

        self.assertEqual(split_searched_strength("metformin 10 mg/ml"), ("metformin", "10 mg/ml"))
        self.assertEqual(split_searched_strength("amoxicillin 250 mg / 5 ml"), ("amoxicillin", "250 mg/5ml"))
        self.assertEqual(split_searched_strength("salbutamol 100 mcg/dose"), ("salbutamol", "100 mcg/dose"))

    def test_a_concentration_matches_the_same_amount_per_ml_however_it_is_written(self):
        from services.search_pipeline import matches_searched_strength

        for strength in ("10 mg/ml", "10MG/ML", "50 mg/5 ml", "0.01 g/ml", ""):
            self.assertTrue(matches_searched_strength({"strength": strength}, "10 mg/ml"), strength)
        for strength in ("20 mg/ml", "500 mg", "250mg/5ml"):
            self.assertFalse(matches_searched_strength({"strength": strength}, "10 mg/ml"), strength)
        for strength in ("100 micrograms/dose", "100mcg/dose", "100 µg/dose"):
            self.assertTrue(matches_searched_strength({"strength": strength}, "100 mcg/dose"), strength)
        self.assertFalse(matches_searched_strength({"strength": "200 mcg/dose"}, "100 mcg/dose"))

    def test_the_search_asks_the_registers_for_the_molecule_only(self):
        asked = []

        def combined(substance, **_kwargs):
            asked.append(substance)
            return [
                {"product": "Renvela", "active_substance": "sevelamer carbonate", "strength": "800 mg", "country": "Greece", "source": "EOF Greece"},
                {"product": "Renvela", "active_substance": "sevelamer carbonate", "strength": "2.4 g", "country": "Greece", "source": "EOF Greece"},
            ]

        with patch("services.search_pipeline.combined_search", side_effect=combined):
            rows, _, _ = filtered_search_results("sevelamer carbonate 800", live=False, sources=["EOF Greece"], include_lookup_rows=False)
        self.assertEqual(asked, ["sevelamer carbonate"])
        self.assertEqual([row["strength"] for row in rows], ["800 mg"])


class GreeceConnectorTests(unittest.TestCase):
    RESULTS = """<partial-response><changes><update id="frmMain:tblResults"><![CDATA[
        <tr data-ri="0" class="ui-widget-content"><td role="gridcell"><span class="ui-column-title">Κωδικός</span>3168501</td>
        <td role="gridcell"><span class="ui-column-title">Ονομασία / Περιεκτικότητα</span>SEVELAMER/FARAN F.C.TAB 800MG/TAB</td>
        <td role="gridcell"><span class="ui-column-title">Κατάσταση Α.Κ.</span><span>Εγκεκριμένο</span></td>
        <td role="gridcell"><span class="ui-column-title">Αρ. άδειας</span>81664/4-11-2016</td>
        <td role="gridcell"><span class="ui-column-title">Διαδικασία</span>Αμοιβαίας Αναγνώρισης</td>
        <td role="gridcell"><span class="ui-column-title">Αρ. διαδικασίας</span>DK/H/1948/001/E/002</td>
        <td><a href="view.xhtml?id=ad0191ce">x</a></td></tr>]]></update></changes></partial-response>"""
    PRODUCT = """<div class="surface-section"><div class="text-2xl">Γενικά</div><ul>
        <li><div>Κωδικός</div><div>3168501</div></li>
        <li><div>Κατάσταση Α.Κ.</div><div><span>Εγκεκριμένο</span></div></li>
        <li><div>Κάτοχος Αδείας Κυκλοφορίας</div>
            <div>ΦΑΡΑΝ ΑΝΩΝΥΜΗ ΒΙΟΜΗΧΑΝΙΚΗ ΕΤΑΙΡΕΙΑ Δ.Τ. ΦΑΡΑΝ Α.Β.Ε.Ε., ΕΛΛΑΔΑ</div>
            <div><i></i>Αχαίας 5, 145 64 Ν. Κηφισιά<br/>210.6254175</div></li></ul></div>
        <div class="surface-section"><div class="text-2xl">Συσκευασίες</div><ul>
        <li><div>2803168501017</div><div>BTx BOTTLE (HDPE) x 180 TABS- με υλικό αφύγρανσης</div>
            <div>180<span title="ΤΕΜΑΧΙΟ">ΤΕ</span></div><div><span>Εγκεκριμένο</span></div><div></div>
            <div>€ <span id="j_idt66:0:txtGRP">62,02</span></div></li></ul></div>
        <div class="surface-section"><div class="text-2xl">Φαρμακοτεχνική μορφή - Περιεκτικότητα</div><ul>
        <li><div>F.C.TAB</div><div>ΕΠΙΚΑΛΥΜΜΕΝΟ ΜΕ ΛΕΠΤΟ ΥΜΕΝΙΟ ΔΙΣΚΙΟ</div><div>800MG/TAB</div></li></ul></div>
        <div class="surface-section"><div class="text-2xl">Ταξινόμηση ATC</div><ul>
        <li><div>V03AE02</div><div>SEVELAMER</div></li></ul></div>
        <div class="surface-section"><div class="text-2xl">Οδός χορήγησης</div><ul>
        <li><div></div><div>ΑΠΟ ΤΟΥ ΣΤΟΜΑΤΟΣ</div></li></ul></div>
        <div class="surface-section"><div class="text-2xl">Δραστική ουσία</div><ul>
        <li><div></div><div>SEVELAMER CARBONATE</div><div>800<span>MG</span></div></li></ul></div>
        <div class="surface-section"><div class="text-2xl">Τεκμηρίωση</div><ul>
        <li><div>Φύλλο Οδηγιών για το Χρήστη</div><div><a href="./download?id=2783&amp;type=246">PL</a></div></li></ul></div>"""

    def test_a_greek_product_is_read_into_english(self):
        from sources.eof_greece import build_row, parse_product_page, parse_result_rows

        listing = parse_result_rows(self.RESULTS)[0]
        self.assertEqual((listing["code"], listing["view_id"]), ("3168501", "ad0191ce"))
        row = build_row(listing, parse_product_page(self.PRODUCT), "sevelamer carbonate")

        self.assertEqual(row["product"], "SEVELAMER/FARAN F.C.TAB 800MG/TAB")
        self.assertEqual((row["company"], row["ma_holder_country"]), ("FARAN S.A.", "Greece"))
        self.assertEqual((row["status"], row["authorisation_procedure"]), ("Authorised", "Mutual recognition"))
        self.assertEqual((row["dosage_form"], row["strength"], row["route"]), ("Film-coated tablet", "800MG/TAB", "Oral"))
        self.assertEqual(row["pack_size"], "BTX BOTTLE (HDPE) X 180 TABS- WITH DESICCANT (180 units)")
        self.assertEqual(row["price"], "EUR 62,02 (retail incl. VAT)")
        self.assertEqual((row["atc_code"], row["registration_number"]), ("V03AE02", "81664/4-11-2016"))
        self.assertEqual(row["pil_url"], "https://services.eof.gr/human-search/download?id=2783&type=246")
        self.assertTrue(row_relevant_to_substance(row, "sevelamer carbonate"))

    def test_a_page_showing_another_product_is_not_used(self):
        from sources import eof_greece

        register = MagicMock()
        register.page.return_value = eof_greece.parse_result_rows(self.RESULTS)
        register.product_page.return_value = self.PRODUCT.replace("3168501", "2434002")
        (listing, detail), = eof_greece._read_pages(register, [0], deadline=float("inf"))
        self.assertEqual(listing["code"], "3168501")
        self.assertEqual(detail, {})

    def test_greek_letters_are_transliterated(self):
        from sources.eof_greece import english_company, transliterate

        self.assertEqual(transliterate("ΥΔΡΟΧΛΩΡΙΚΗ ΣΕΒΕΛΑΜΕΡΗ"), "YDROCHLORIKI SEVELAMERI")
        self.assertEqual(english_company("ΦΑΡΜΑΤΕΝ ΑΒΕΕ"), "FARMATEN S.A.")
        self.assertEqual(english_company("SANOFI-AVENTIS ΜΟΝΟΠΡΟΣΩΠΗ Α.Ε. Δ.Τ. SANOFI"), "SANOFI")


class AustraliaRepositoryTests(unittest.TestCase):
    PICMI = """<div class='tblResults'><table><thead><th>Trade Name</th></thead><tbody>
        <tr><td>ARX-Sevelamer</td><td><a href='pdf?OpenAgent&id=CP-2019-CMI-02288-1'>CMI</a>
            <a href='pdf?OpenAgent&id=CP-2018-PI-02585-1'>PI</a></td><td>sevelamer carbonate</td></tr>
        <tr><td>Renagel</td><td><a href='pdf?OpenAgent&id=CP-2011-PI-01839-3'>PI</a></td><td>Sevelamer hydrochloride</td></tr>
        <tr><td>Sevelamer Lupin</td><td><a href='pdf?OpenAgent&id=CP-2018-PI-02588-1'>PI</a></td><td>sevelamer carbonate</td></tr>
        <tr><td>SEVELAMER LUPIN</td><td><a href='pdf?OpenAgent&id=CP-2020-CMI-01212-1'>CMI</a></td><td>sevelamer carbonate</td></tr>
        </tbody></table></div>"""

    def test_the_repository_answers_when_the_artg_does_not(self):
        from sources import tga

        def get(url, **_kwargs):
            if "picmi" not in url:
                raise requests.ReadTimeout("ARTG hangs")
            return MagicMock(text=self.PICMI, url=url, raise_for_status=lambda: None)

        with patch("sources.tga.requests.get", side_effect=get):
            rows = tga.run_tga_search("sevelamer carbonate")

        by_name = {row["product"]: row for row in rows}
        # One product whatever the case its documents are filed under.
        self.assertEqual(sorted(by_name), ["ARX-Sevelamer", "Renagel", "Sevelamer Lupin"])
        lupin = by_name["Sevelamer Lupin"]
        self.assertTrue(lupin["smpc_url"].endswith("CP-2018-PI-02588-1"))
        self.assertTrue(lupin["pil_url"].endswith("CP-2020-CMI-01212-1"))
        # The repository does not name the sponsor, and nothing stands in for it.
        self.assertEqual(lupin["company"], "")
        self.assertTrue(row_relevant_to_substance(lupin, "sevelamer carbonate"))
        self.assertFalse(row_relevant_to_substance(by_name["Renagel"], "sevelamer carbonate"))

    def test_artg_pages_link_only_real_documents(self):
        from sources.tga import _parse_artg_detail

        detail = _parse_artg_detail("""<main><div>Sponsor</div><div>Dr Reddys Laboratories Australia Pty Ltd</div>
            <div>Licence status</div><div>A</div>
            <a href="/products/about-artg/product-information-pi">Product information</a>
            <a href="https://www.ebs.tga.gov.au/ebs/picmi/picmirepository.nsf/pdf?OpenAgent&id=CP-2018-PI-02585-1">X-Product information-[PDF]</a></main>""")
        self.assertEqual(detail["smpc_url"], "https://www.ebs.tga.gov.au/ebs/picmi/picmirepository.nsf/pdf?OpenAgent&id=CP-2018-PI-02585-1")
        self.assertEqual(detail["licence_status"], "A")


class JapanPriceListTests(unittest.TestCase):
    KEGG = [
        "dr_ja:D01966\tエゼチミブ (JAN); Ezetimibe (JAN/USP/INN)",
        "dr_ja:D02258\tアトルバスタチンカルシウム水和物 (JP19); Atorvastatin calcium (USP)",
        "dr_ja:D08512\tセベラマー; Sevelamer (INN)",
    ]

    def _row(self, **record):
        from sources import japan_nhi

        names = japan_nhi.parse_kegg_names(self.KEGG)
        record.setdefault("_workbook", "01")
        record.setdefault("_file", "https://www.mhlw.go.jp/x.xlsx")
        record["_english"] = japan_nhi.english_ingredients(record["成分名"], names)
        rows = list(japan_nhi.build_nhi_rows([record], "2026-09-17"))
        return rows[0] if rows else None

    def test_a_generic_is_named_in_english_with_its_makers_mark(self):
        match, row = self._row(**{
            "成分名": "アトルバスタチンカルシウム水和物", "規格": "１０ｍｇ１錠", "品名": "アトルバスタチン錠１０ｍｇ「サワイ」",
            "メーカー名": "沢井製薬", "診療報酬において加算等の算定対象となる後発医薬品": "後発品", "薬価": 11.5,
            "薬価基準収載医薬品コード": "2189015F2054",
        })
        self.assertEqual(row["product"], "Atorvastatin calcium 10 mg Tablet [Sawai Pharmaceutical]")
        self.assertEqual(row["company"], "Sawai Pharmaceutical")
        self.assertEqual((row["authorisation_scope"], row["price"]), ("Generic", "JPY 11.5 per tablet"))
        self.assertEqual(row["registration_number"], "2189015F2054")
        self.assertIn(" atorvastatin ", match)

    def test_a_brand_is_romanised_and_says_so(self):
        _match, row = self._row(**{
            "成分名": "エゼチミブ・アトルバスタチンカルシウム水和物", "規格": "１錠", "品名": "アトーゼット配合錠ＬＤ",
            "メーカー名": "オルガノン", "先発医薬品": "先発品", "薬価": 50.4,
        })
        self.assertEqual(row["product"], "Atozetto (romanised brand) - Ezetimibe; Atorvastatin calcium Combination tablet LD")
        self.assertEqual((row["company"], row["authorisation_scope"]), ("Organon", "Originator"))
        self.assertTrue(row_relevant_to_substance(row, "atorvastatin + ezetimibe"))

    def test_an_ingredient_not_in_the_dictionary_is_left_out(self):
        self.assertIsNone(self._row(**{"成分名": "黄連湯エキス", "規格": "１ｇ", "品名": "黄連湯エキス顆粒"}))

    def test_a_company_without_an_english_name_keeps_its_own(self):
        from sources.japan_nhi import english_company

        self.assertEqual(english_company("栃本天海堂"), "栃本天海堂")


class RegisterRefreshJobTests(unittest.TestCase):
    """The daily job that re-downloads the registers regulators publish whole."""

    def _register(self, max_age_seconds=7 * 24 * 3600):
        from sources.open_registers import OpenRegister

        return OpenRegister(
            source="Test Register",
            country="Testland",
            region="EU",
            url="https://example.invalid/register.csv",
            slug="test_register",
            max_age_seconds=max_age_seconds,
            build_rows=lambda records, fetched_at: iter(()),
        )

    def test_a_copy_older_than_the_regulators_own_cadence_is_due(self):
        import time

        from tools import refresh_registers

        register = self._register(max_age_seconds=86400)
        fresh = {"fetched_epoch": str(time.time() - 3600)}
        stale = {"fetched_epoch": str(time.time() - 200000)}

        with patch.object(refresh_registers, "index_info", return_value=fresh):
            self.assertFalse(refresh_registers.is_due(register))
        with patch.object(refresh_registers, "index_info", return_value=stale):
            self.assertTrue(refresh_registers.is_due(register))
        with patch.object(refresh_registers, "index_info", return_value=None):
            self.assertTrue(refresh_registers.is_due(register))  # never downloaded

    def test_one_register_failing_does_not_stop_the_rest_and_the_run_says_so(self):
        from tools import refresh_registers

        good, bad = self._register(), self._register()
        object.__setattr__(bad, "source", "Broken Register")
        asked = []

        def refresh(register):
            asked.append(register.source)
            if register.source == "Broken Register":
                raise RuntimeError("regulator timed out")
            return Path("index.sqlite")

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PHARMASEARCH_LOG_DIR": directory}
        ), patch.object(refresh_registers, "LOG_PATH", Path(directory) / "refresh.log"), patch.object(
            refresh_registers, "REGISTERS", {"Broken Register": bad, "Test Register": good}
        ), patch.object(
            refresh_registers, "refresh_register", refresh
        ), patch.object(
            refresh_registers, "index_info", return_value=None
        ):
            refreshed, failed = refresh_registers.refresh_due()

        self.assertEqual(refreshed, ["Test Register"])
        self.assertEqual(failed, ["Broken Register"])
        self.assertEqual(len(asked), 2)  # the failure did not end the run

    def test_an_unknown_register_name_stops_the_run_rather_than_refreshing_everything(self):
        from tools import refresh_registers

        with patch.object(refresh_registers, "REGISTERS", {"Test Register": self._register()}):
            with self.assertRaises(SystemExit):
                refresh_registers.selected_registers(["Germany BfArM"])


class LiveSourceRefreshJobTests(unittest.TestCase):
    """The weekly job that re-asks the regulators searched one molecule at a time."""

    def test_registers_published_as_a_file_are_not_asked_molecule_by_molecule(self):
        from tools import refresh_live_sources

        names = refresh_live_sources.live_sources(include_all=True)

        self.assertIn("MHRA", names)
        self.assertNotIn("AIFA Italy", names)  # refreshed whole by refresh_registers
        self.assertNotIn("HPRA Ireland", names)

    def test_a_source_that_only_hands_back_a_registry_link_is_left_out(self):
        from tools import refresh_live_sources

        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.initialize_database()
            for _ in range(3):
                repository.save_source_run("Link Only", "metformin", "SOURCE_UNSUPPORTED")
                repository.save_source_run("Answers", "metformin", "SUCCESS", records_found=4)

            self.assertFalse(refresh_live_sources._answers_with_products("Link Only"))
            self.assertTrue(refresh_live_sources._answers_with_products("Answers"))
            # Never asked at all: worth one run rather than a permanent skip.
            self.assertTrue(refresh_live_sources._answers_with_products("Brand New"))

    def test_the_molecules_checked_longest_ago_come_first(self):
        from tools import refresh_live_sources

        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.initialize_database()
            for substance, checked in (
                ("Recent Molecule", "2026-09-17T00:00:00+00:00"),
                ("Old Molecule", "2026-01-02T00:00:00+00:00"),
                ("old molecule", "2026-01-03T00:00:00+00:00"),
            ):
                repository.save_product_detail({
                    "substance": substance,
                    "product": f"{substance} 10 mg",
                    "country": "United Kingdom",
                    "source": "MHRA",
                    "last_checked": checked,
                })

            molecules = [name.lower() for name in refresh_live_sources.oldest_molecules(5000)]

        self.assertLess(molecules.index("old molecule"), molecules.index("recent molecule"))
        # The same molecule under two spellings is one question, not two.
        self.assertEqual(molecules.count("old molecule"), 1)


GERMAN_RESULT_HTML = """
<table id="searchResultsForm:searchResultsComponent:titles">
  <tr><th>Nr.</th><th>Arzneimittelbezeichnung</th><th>Darreichungsform</th>
      <th>Zulassungsinhaber*</th><th>Eingangsnummer</th><th>Zul.-Nr.</th><th>Version</th></tr>
  <tr>
    <td>1.</td>
    <td><a href="/amguifree/am/docoutput/jpadocdisplay.xhtml?globalDocId=ABC&docid=1">Renagel 800 mg Filmtabletten</a></td>
    <td>Filmtablette</td>
    <td>Sanofi Winthrop Industrie Rechtsform: S.A.</td>
    <td>2702387</td><td>EU/1/99/123/011</td><td>19.0.0</td>
  </tr>
</table>
<span>Gefundene Dokumente: 115</span>
"""

GERMAN_DOCUMENT_HTML = """
<table><tr><td>Eingangsnummer</td><td>2702387</td></tr>
       <tr><td>Arzneimittelbezeichnung</td><td>Renagel 800 mg Filmtabletten</td></tr>
       <tr><td>Darreichungsform</td><td>Filmtablette</td></tr></table>
<table><tr><td>Indikation/ATC-Code</td><td>V03AE02</td></tr></table>
<table><tr><td>Verkehrsf&auml;higkeit</td><td>Ja</td></tr></table>
<table><tr><td>Zulassungsnummer/ Registrierungsnummer</td><td>EU/1/99/123/011</td></tr>
       <tr><td>Datum der Zulassung/Registrierung (Wirksamkeitsdatum)</td><td>18.10.2001</td></tr>
       <tr><td>Verfahrenstyp</td><td>CP - europ&auml;isches zentralisiertes Verfahren</td></tr>
       <tr><td>Status</td><td>verl&auml;ngert</td></tr></table>
<h3>Pharmazeutische Unternehmer</h3>
<h4>Zulassungsinhaber*</h4>
<table><tr><td>PU-Nummer</td><td>3323442</td></tr>
       <tr><td>Name</td><td>Sanofi Winthrop Industrie Rechtsform: S.A.</td></tr>
       <tr><td>Stra&szlig;e Hausnummer</td><td>82 Avenue Raspail -</td></tr>
       <tr><td>PLZ Ort</td><td>94250 GENTILLY</td></tr>
       <tr><td>Land</td><td>Frankreich</td></tr></table>
<h4>Hersteller Endfreigabe</h4>
<table><tr><td>PU-Nummer</td><td>8080895</td></tr>
       <tr><td>Name</td><td>Genzyme Ireland Limited</td></tr>
       <tr><td>Stra&szlig;e Hausnummer</td><td>IDA Industrial Park -</td></tr>
       <tr><td>PLZ Ort</td><td>X91 TP27 WATERFORD</td></tr>
       <tr><td>Land</td><td>Irland</td></tr></table>
<table><tr><td>PU-Nummer</td><td>8001499</td></tr>
       <tr><td>Name</td><td>Genzyme Ltd.</td></tr>
       <tr><td>Stra&szlig;e Hausnummer</td><td>37 Hollands Road -</td></tr>
       <tr><td>PLZ Ort</td><td>CB9 8PU HAVERHILL</td></tr>
       <tr><td>Land</td><td>Vereinigtes K&ouml;nigreich</td></tr></table>
<span>Wirkstoffe</span>
<table><tr><th>ASK-Nr.</th><th>Stoffname</th><th>Potenz</th><th>Mengen- operator</th>
           <th>Stoffmenge</th><th>Stoffmenge maximal</th><th>Ma&szlig;einheit</th><th>Bemerkung</th><th>Nr.</th></tr>
       <tr><td>30043</td><td>Sevelamer</td><td>-</td><td>n/a</td><td>800</td><td>-</td><td>Milligramm</td><td>-</td><td>1</td></tr></table>
<h3>Packungsgr&ouml;&szlig;en-Gruppe/Verkaufsabgrenzung</h3>
<table><tr><td>Packungsgr&ouml;&szlig;en-ID</td><td>2702387_1</td></tr>
       <tr><td>Packungsgr&ouml;&szlig;e</td><td>Originalpackung 100 St&uuml;ck</td></tr>
       <tr><td>Verkaufsabgrenzung</td><td>verschreibungspflichtig</td></tr></table>
"""


class GermanyConnectorTests(unittest.TestCase):
    """BfArM's AMIce public database, read live and shown but never kept."""

    def test_the_result_list_gives_the_product_the_holder_and_its_document(self):
        from sources.germany_bfarm import found_documents, parse_result_rows

        rows = parse_result_rows(GERMAN_RESULT_HTML)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product"], "Renagel 800 mg Filmtabletten")
        self.assertEqual(rows[0]["registration_number"], "EU/1/99/123/011")
        self.assertIn("globalDocId=ABC", rows[0]["document_url"])
        self.assertEqual(found_documents(GERMAN_RESULT_HTML), 115)

    def test_the_holder_and_the_release_sites_are_kept_apart(self):
        from sources.germany_bfarm import parse_document

        detail = parse_document(GERMAN_DOCUMENT_HTML)
        roles = {company["role"] for company in detail["companies"]}
        makers = [company for company in detail["companies"] if company["role"] == "Batch release"]

        self.assertEqual(roles, {"holder", "Batch release"})
        self.assertEqual([maker["name"] for maker in makers], ["Genzyme Ireland Limited", "Genzyme Ltd."])
        self.assertEqual([maker["country"] for maker in makers], ["Ireland", "United Kingdom"])

    def test_the_document_is_read_in_english(self):
        from sources.germany_bfarm import parse_document

        detail = parse_document(GERMAN_DOCUMENT_HTML)

        self.assertEqual(detail["dosage_form"], "Film-coated tablet")
        self.assertEqual(detail["strength"], "800 mg")
        self.assertEqual(detail["status"], "Authorised (renewed)")
        self.assertEqual(detail["authorisation_scope"], "Centralised")
        self.assertEqual(detail["classification"], "Prescription only")
        self.assertEqual(detail["pack_size"], "Original pack 100 units")
        self.assertEqual(detail["registration_date"], "2001-10-18")

    def test_the_legal_form_label_is_not_part_of_the_company_name(self):
        from sources.germany_bfarm import company_name

        self.assertEqual(
            company_name("Sanofi Winthrop Industrie Rechtsform: S.A."),
            "Sanofi Winthrop Industrie S.A.",
        )

    def test_a_row_says_it_may_be_read_but_not_kept(self):
        from sources.germany_bfarm import build_row, parse_document, parse_result_rows

        listed = parse_result_rows(GERMAN_RESULT_HTML)[0]
        row = build_row(listed, parse_document(GERMAN_DOCUMENT_HTML), "sevelamer", listed["document_url"], "2026-09-18T00:00:00Z")

        self.assertTrue(row["view_only"])
        self.assertEqual(row["copyright"], "© BfArM, Bonn")
        self.assertEqual(row["country"], "Germany")
        self.assertEqual(row["company"], "Sanofi Winthrop Industrie S.A.")
        self.assertEqual(row["manufacturer_name"], "Genzyme Ireland Limited; Genzyme Ltd.")
        self.assertEqual(row["manufacturer_country"], "Ireland; United Kingdom")

    def test_a_document_about_another_product_is_ignored_rather_than_mixed_in(self):
        from sources.germany_bfarm import _document

        class Session:
            def get(self, url, **kwargs):
                response = MagicMock()
                response.text = GERMAN_DOCUMENT_HTML
                response.raise_for_status = lambda: None
                return response

        _, detail = _document(Session(), {"product": "A different medicine", "document_url": "https://x/doc"})

        self.assertEqual(detail, {})

    def test_the_search_asks_for_the_active_ingredient_and_medicines_on_sale(self):
        from sources.germany_bfarm import FILTER_PREFIX, FORM_PREFIX, SUBSTANCE_FIELD, search_payload

        form = (
            '<form id="searchForm">'
            f'<input name="{FORM_PREFIX}:searchRows:0:searchTerm" value="">'
            f'<select name="{FORM_PREFIX}:searchRows:0:searchField"><option value="MPD_NAME">Name</option></select>'
            '<input type="hidden" name="jakarta.faces.ViewState" value="42">'
            "</form>"
        )
        payload = search_payload(form, "sevelamer")

        self.assertEqual(payload[f"{FORM_PREFIX}:searchRows:0:searchTerm"], "sevelamer")
        self.assertEqual(payload[f"{FORM_PREFIX}:searchRows:0:searchField"], SUBSTANCE_FIELD)
        self.assertEqual(payload[f"{FILTER_PREFIX}:verkehrsfaehige:verkehrsfaehige"], "AND")
        self.assertEqual(payload["jakarta.faces.ViewState"], "42")  # JSF rejects a post without it


class ViewOnlyRowTests(unittest.TestCase):
    """A regulator's data that may be shown but not kept or passed on."""

    def _row(self):
        return {
            "substance": "sevelamer",
            "product": "Renagel 800 mg",
            "company": "Sanofi Winthrop Industrie S.A.",
            "country": "Germany",
            "source": "BfArM Germany",
            "view_only": True,
        }

    def test_the_store_does_not_keep_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            repository, "DB_PATH", Path(directory) / "test.db"
        ):
            repository.initialize_database()
            saved = repository.save_product_detail(self._row())
            with repository.get_connection() as connection:
                german = connection.execute(
                    "SELECT COUNT(*) FROM product_details WHERE source = 'BfArM Germany'"
                ).fetchone()[0]

        self.assertEqual(saved.get("persistence_status"), "SOURCE_RUN_ONLY")
        self.assertEqual(german, 0)

    def test_the_export_leaves_it_out(self):
        stored = {**self._row(), "country": "Ireland", "source": "HPRA Ireland"}
        del stored["view_only"]

        rows = build_export_rows("sevelamer", [self._row(), stored])

        self.assertEqual([row["Country"] for row in rows], ["Ireland"])


class ScheduledJobClockTests(unittest.TestCase):
    """The one clock the server runs the three jobs on."""

    def _job(self, **kwargs):
        from tools.scheduled_jobs import Job

        fields = {"name": "test", "hour": 3, "minute": 0, "run": lambda: 0, **kwargs}
        return Job(**fields)

    def test_a_daily_job_is_due_once_its_hour_has_passed_and_not_again_that_day(self):
        from datetime import datetime

        from tools.scheduled_jobs import is_due

        job = self._job()
        morning = datetime(2026, 9, 18, 4, 0)
        ran_today = {"test": "2026-09-18T03:05:00"}
        ran_yesterday = {"test": "2026-09-17T03:05:00"}

        self.assertFalse(is_due(job, morning, ran_today))
        self.assertTrue(is_due(job, morning, ran_yesterday))
        # Before today's 03:00, yesterday's run is still the current one.
        self.assertFalse(is_due(job, datetime(2026, 9, 18, 2, 0), ran_yesterday))

    def test_a_weekly_job_missed_while_the_machine_was_off_runs_at_the_next_check(self):
        from datetime import datetime

        from tools.scheduled_jobs import is_due

        sunday_job = self._job(weekday=6, hour=2)
        # Monday: Sunday's moment has passed and the last run was the Sunday before.
        self.assertTrue(is_due(sunday_job, datetime(2026, 9, 21, 9, 0), {"test": "2026-09-13T02:10:00"}))
        self.assertFalse(is_due(sunday_job, datetime(2026, 9, 21, 9, 0), {"test": "2026-09-20T02:10:00"}))

    def test_a_job_that_has_never_run_is_due_at_once(self):
        from datetime import datetime

        from tools.scheduled_jobs import is_due

        self.assertTrue(is_due(self._job(weekday=6), datetime(2026, 9, 18, 12, 0), {}))

    def test_a_failing_job_records_its_run_so_it_is_not_retried_in_a_loop(self):
        from tools import scheduled_jobs

        def explode() -> int:
            raise RuntimeError("regulator down")

        job = self._job()
        object.__setattr__(job, "run", explode)
        state: dict[str, str] = {}

        with tempfile.TemporaryDirectory() as directory, patch.object(
            scheduled_jobs, "LOG_PATH", Path(directory) / "jobs.log"
        ), patch.object(scheduled_jobs, "STATE_PATH", Path(directory) / "jobs.json"):
            code = scheduled_jobs.run_job(job, state)

        self.assertEqual(code, 1)
        self.assertIn("test", state)



class NationalRegisterTests(unittest.TestCase):
    """Netherlands, Poland, Czechia and Denmark, read from their published files."""

    def _tokens(self, text, latin=False):
        return set(national_registers.national_match_text(text, latin=latin).split())

    def _finds(self, register_text, query, latin=False):
        from sources.open_registers import query_tokens

        return set(query_tokens(query)) <= self._tokens(register_text, latin)

    def test_latin_names_match_the_english_search(self):
        self.assertTrue(self._finds("Metformini hydrochloridum", "metformin", latin=True))
        self.assertTrue(self._finds("Atorvastatinum; Amlodipinum", "atorvastatin", latin=True))
        self.assertTrue(self._finds("Acidum zoledronicum", "zoledronic acid", latin=True))
        self.assertTrue(self._finds("Insulinum glarginum", "insulin glargine", latin=True))

    def test_salts_and_acids_glued_to_the_name_are_split_off(self):
        self.assertTrue(self._finds("METFORMINEHYDROCHLORIDE", "metformin hydrochloride"))
        self.assertTrue(self._finds("AMLODIPINEBESILAAT", "amlodipine"))
        self.assertTrue(self._finds("ATORVASTATINCALCIUM (trihydrat)", "atorvastatin"))
        self.assertTrue(self._finds("ZOLEDRONINEZUUR", "zoledronic acid"))
        self.assertTrue(self._finds("Zoledronsyre", "zoledronic acid"))
        self.assertTrue(self._finds("VALPROINEZUUR", "valproic acid"))
        # A short name is not cut down to nothing.
        self.assertEqual(national_registers._unglue("calcium"), ["calcium"])

    def test_latin_is_undone_only_where_the_register_writes_latin(self):
        self.assertIn("metformini", self._tokens("metformini", latin=False))

    def test_rows_carry_the_tokens_the_page_checks_a_combination_against(self):
        _, row = next(national_registers.build_poland_rows([{
            "Rodzaj preparatu": "Ludzki", "Nazwa Produktu Leczniczego": "Coroswera",
            "Nazwa powszechnie stosowana": "Rosuvastatinum + Ezetimibum", "Moc": "10 mg + 10 mg",
            "Postać farmaceutyczna": "Tabletki powlekane", "Numer pozwolenia": "25209",
        }], "2026-09-22"))
        self.assertTrue(row_relevant_to_substance(row, "rosuvastatin + ezetimibe"))
        self.assertFalse(row_relevant_to_substance(row, "rosuvastatin + fenofibrate"))

    def test_netherlands_row(self):
        match, row = next(national_registers.build_netherlands_rows([{
            "REGISTRATIENUMMER": "103638", "SOORT": "RVG ",
            "PRODUCTNAAM": "Metformine HCl Viatris 500 mg, filmomhulde tabletten",
            "INSCHRIJVINGSDATUM": "2010/01/13", "HANDELSVERGUNNINGHOUDER": "Viatris Limited",
            "AFLEVERSTATUS": "Uitsluitend recept", "FARMACEUTISCHEVORM": "Filmomhulde tablet",
            "POTENTIE": "", "PROCEDURENUMMER": "NL/H/1570/001", "TOEDIENINGSWEG": "Oraal gebruik",
            "ATC": "A10BA02 - Metformin", "WERKZAMESTOFFEN": "METFORMINEHYDROCHLORIDE",
            "PRODUCTDETAIL_LINK": "https://www.geneesmiddeleninformatiebank.nl/nl/rvg103638",
            "SMPC_FILENAAM": "https://www.geneesmiddeleninformatiebank.nl/smpc/h103638_smpc.pdf",
            "BIJSLUITER_FILENAAM": "https://www.geneesmiddeleninformatiebank.nl/bijsluiters/h103638.pdf",
        }], "2026-09-22"))
        self.assertIn(" metformin ", match)
        self.assertEqual(row["registration_number"], "RVG 103638")
        self.assertEqual(row["dosage_form"], "Film-coated tablet")
        self.assertEqual(row["atc_code"], "A10BA02")
        self.assertEqual(row["authorisation_scope"], "Mutual recognition / decentralised")
        self.assertEqual(row["registration_date"], "2010-01-13")
        self.assertEqual(row["strength"], "500 mg")
        self.assertTrue(row["smpc_url"].endswith("h103638_smpc.pdf"))

    def test_netherlands_keeps_eu_numbers_as_published(self):
        _, row = next(national_registers.build_netherlands_rows([{
            "REGISTRATIENUMMER": "EU/1/14/944", "SOORT": "", "PRODUCTNAAM": "ABASAGLAR 100 E/ml",
            "WERKZAMESTOFFEN": "INSULINE GLARGINE", "PROCEDURENUMMER": "",
        }], "2026-09-22"))
        self.assertEqual(row["registration_number"], "EU/1/14/944")
        self.assertEqual(row["authorisation_scope"], "Centralised")

    def test_poland_row_names_the_manufacturers_in_english(self):
        rows = list(national_registers.build_poland_rows([
            {
                "Rodzaj preparatu": "Ludzki", "Nazwa Produktu Leczniczego": "Metformax 500",
                "Nazwa powszechnie stosowana": "Metformini hydrochloridum", "Moc": "500 mg",
                "Postać farmaceutyczna": "Tabletki", "Typ procedury": "NAR", "Numer pozwolenia": "01263",
                "Ważność pozwolenia": "Bezterminowe", "Kod ATC": "A10BA02",
                "Podmiot odpowiedzialny": "Teva Pharmaceuticals Polska Sp. z o.o.",
                "Opakowanie": "05909990012634 ¦ Rp ¦ 1\n30 tabl.\n05909990012641 ¦ Rp ¦ 2\n60 tabl.",
                "Nazwa wytwórcy": "Teva Operations Polska Sp. z o.o.\nPliva Hrvatska d.o.o.",
                "Kraj wytwórcy": "Polska\nChorwacja",
            },
            {"Rodzaj preparatu": "Weterynaryjny", "Nazwa Produktu Leczniczego": "Vet",
             "Nazwa powszechnie stosowana": "Metformini hydrochloridum"},
        ], "2026-09-22"))
        self.assertEqual(len(rows), 1)  # the veterinary product is left out
        _, row = rows[0]
        self.assertEqual(row["manufacturer_name"], "Teva Operations Polska Sp. z o.o.; Pliva Hrvatska d.o.o.")
        self.assertEqual(row["manufacturer_country"], "Poland; Croatia")
        self.assertEqual(row["manufacturer_source"], "Polish Register of Medicinal Products")
        self.assertEqual(row["pack_size"], "30 tabl.; 60 tabl.")
        self.assertEqual(row["expiry_date"], "Unlimited")
        self.assertEqual(row["authorisation_scope"], "National")
        self.assertEqual(row["product"], "Metformax 500")

    def test_poland_tells_central_products_apart_by_strength_and_form(self):
        rows = [
            row for _, row in national_registers.build_poland_rows([
                {"Rodzaj preparatu": "Ludzki", "Nazwa Produktu Leczniczego": "Zometa",
                 "Nazwa powszechnie stosowana": "Acidum zoledronicum", "Moc": strength,
                 "Postać farmaceutyczna": form, "Typ procedury": "CEN", "Numer pozwolenia": ""}
                for strength, form in (("4 mg/5 ml", "Koncentrat do sporządzania roztworu do infuzji"),
                                       ("4 mg/100 ml", "Roztwór do infuzji"))
            ], "2026-09-22")
        ]
        self.assertEqual(
            [row["product"] for row in rows],
            ["Zometa 4 mg/5 ml Concentrate for solution for infusion", "Zometa 4 mg/100 ml Solution for infusion"],
        )

    def test_czech_register_groups_packs_and_uses_sukls_english_names(self):
        def table(header, *rows):
            lines = [";".join(header)] + [";".join(row) for row in rows]
            return ("\r\n".join(lines) + "\r\n").encode("cp1250")

        pack = ["KOD_SUKL", "NAZEV", "SILA", "FORMA", "CESTA", "DOPLNEK", "DRZ", "ZEMDRZ", "REG",
                "V_PLATDO", "NEOMEZ", "ATC_WHO", "RC", "REG_PROC", "DODAVKY", "MRP_CISLO"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dlp.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("dlp_latky.csv", table(["KOD_LATKY", "NAZEV_INN", "NAZEV_EN", "NAZEV"],
                                                        ["2417", "METFORMINUM", "METFORMIN HYDROCHLORIDE", "METFORMIN"]))
                archive.writestr("dlp_slozeni.csv", table(["KOD_SUKL", "KOD_LATKY", "SQ", "S"],
                                                          ["0011114", "2417", "1", "L"], ["0011120", "2417", "1", "L"],
                                                          ["0099999", "2417", "1", "L"]))
                archive.writestr("dlp_formy.csv", table(["FORMA", "NAZEV", "NAZEV_EN"], ["TBL FLM", "Potahovaná tableta", "Film-coated tablet"]))
                archive.writestr("dlp_cesty.csv", table(["CESTA", "NAZEV", "NAZEV_EN"], ["POR", "Perorální podání", "Oral use"]))
                archive.writestr("dlp_zeme.csv", table(["ZEM", "NAZEV", "NAZEV_EN"], ["CZ", "ČESKÁ REPUBLIKA", "CZECH REPUBLIC"]))
                archive.writestr("dlp_organizace.csv", table(["ZKR_ORG", "ZEM", "NAZEV"], ["TPP", "CZ", "Teva Pharmaceuticals CR, s.r.o., Praha"]))
                archive.writestr("dlp_lecivepripravky.csv", table(
                    pack,
                    ["0011114", "METFORMIN TEVA", "850MG", "TBL FLM", "POR", "850MG TBL FLM 30", "TPP", "CZ", "R", "", "X", "A10BA02", "18/386/05-C", "CMS", "1", "DE/H/4523/002"],
                    ["0011120", "METFORMIN TEVA", "850MG", "TBL FLM", "POR", "850MG TBL FLM 40", "TPP", "CZ", "R", "", "X", "A10BA02", "18/386/05-C", "CMS", "0", "DE/H/4523/002"],
                    # A food for special medical purposes is not a marketing authorisation.
                    ["0099999", "NUTRI", "", "TBL FLM", "POR", "", "TPP", "CZ", "P", "", "", "", "", "", "0", ""],
                ))
            records = list(national_registers.read_czech(national_registers.SUKL_CZECH, path))
            rows = [row for _, row in national_registers.build_czech_rows(records, "2026-09-22")]

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["product"], "METFORMIN TEVA 850MG")
        self.assertEqual(row["substance"], "Metformin hydrochloride")
        self.assertEqual(row["pack_size"], "850MG TBL FLM 30; 850MG TBL FLM 40")
        self.assertEqual(row["dosage_form"], "Film-coated tablet")
        self.assertEqual(row["route"], "Oral use")
        self.assertEqual(row["company"], "Teva Pharmaceuticals CR, s.r.o., Praha")
        self.assertEqual(row["registration_number"], "18/386/05-C")
        self.assertEqual(row["status"], "Registered, supplied in the last six months")
        self.assertEqual(row["expiry_date"], "Unlimited")
        self.assertEqual(row["authorisation_scope"], "Mutual recognition / decentralised")

    def test_denmark_reads_the_workbook_and_leaves_out_veterinary_medicines(self):
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Drugid", None, "Navn", None, "Lægemiddelform", "Styrketekst", "AktiveSubstanser",
                      "MftIndehaver", "ATC-kode", "Godkendt procedure", "Godkendt rolle", "Registreringsdato",
                      "Er i Medicinpriser"])
        sheet.append([None] * 13)
        sheet.append([28100000001, None, "Eltroxin", None, "tabletter", "100 mikrogram", "LEVOTHYROXINNATRIUM",
                      "Aspen Pharma Trading Ltd.", "H03AA01", "National", "DKMA", datetime(1955, 3, 22), "Ja"])
        sheet.append([28100000002, None, "Eltroxin", None, "tabletter", "100 mikrogram", "LEVOTHYROXINNATRIUM",
                      "Orifarm A/S", "H03AA01", "Par-Imp", "DKMA", datetime(2014, 8, 15), "Ja"])
        sheet.append([28100000003, None, "Adequan Vet.", None, "injektionsvæske, opløsning", "250 mg/ml",
                      "POLYSULFATERET GLYCOSAMINOGLYCAN", "Vet Co", "QM01AX12", "National", "DKMA", None, "Nej"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dkma_authorised.xlsx"
            workbook.save(path)
            records = list(national_registers.read_denmark(national_registers.DKMA_DENMARK, path))
        rows = [row for _, row in national_registers.build_denmark_rows(records, "2026-09-22")]

        self.assertEqual([row["product"] for row in rows], [
            "Eltroxin 100 mikrogram tabletter",
            "Eltroxin 100 mikrogram tabletter (parallel import: Orifarm A/S)",
        ])
        self.assertEqual(rows[0]["dosage_form"], "Tablet")
        self.assertEqual(rows[0]["registration_date"], "1955-03-22")
        self.assertEqual(rows[0]["status"], "Authorised, on the price list")
        self.assertEqual(rows[1]["authorisation_scope"], "Parallel import")
        self.assertEqual(rows[0]["registration_number"], "")  # the list carries no authorisation number

    def test_the_four_registers_are_wired_into_search(self):
        from services.search_pipeline import DEFAULT_SOURCES

        names = {item["name"] for item in connector_metadata()}
        for source, country in (("CBG Netherlands", "Netherlands"), ("URPL Poland", "Poland"),
                                ("SUKL Czech Republic", "Czech Republic"), ("DKMA Denmark", "Denmark")):
            self.assertIn(source, names)
            self.assertIn(source, sources_for_scope(DEFAULT_SOURCES, country=country))
            self.assertIn(source, sources_for_scope(DEFAULT_SOURCES, region="EU"))


class EuropeRegisterTests(unittest.TestCase):
    """Norway, Slovakia, Latvia and Turkey, read from their published files."""

    def test_turkish_spellings_fold_to_the_english_search(self):
        from sources.grls_russia import fold
        from sources.open_registers import query_tokens

        def finds(turkish, english):
            have = set(europe_registers.turkish_tokens(turkish))
            return all(fold(token) in have for token in query_tokens(english))

        self.assertTrue(finds("dekzametazon", "dexamethasone"))
        self.assertTrue(finds("parasetamol", "paracetamol"))
        self.assertTrue(finds("siprofloksasin hidroklorür monohidrat", "ciprofloxacin"))
        self.assertTrue(finds("setirizin dihidroklorür", "cetirizine"))
        self.assertTrue(finds("zoledronik asit monohidrat", "zoledronic acid"))
        self.assertTrue(finds("levotiroksin sodyum", "levothyroxine sodium"))
        self.assertTrue(finds("metformin hidroklorür", "metformin hydrochloride"))
        self.assertFalse(finds("metformin hidroklorür", "atorvastatin"))

    def _workbook(self, directory, name, rows, sheet_title=None):
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        if sheet_title:
            sheet.title = sheet_title
        for row in rows:
            sheet.append(row)
        path = Path(directory) / name
        workbook.save(path)
        return path

    def test_turkey_groups_packs_under_the_licence_and_reads_suspension(self):
        header = ["SIRA NO", "BARKOD", "ÜRÜN ADI", "ETKİN MADDE", "ATC KODU", "RUHSAT SAHİBİ", "RUHSAT TARİHİ",
                  "RUHSAT NUMARASI", "DEĞİŞİKLİK", "", "DEĞİŞİKLİK TARİHİ", "RUHSATI ASKIDA OLMAYAN ÜRÜN: 0", "ASKIYA ALINMA TARİHİ"]
        with tempfile.TemporaryDirectory() as directory:
            path = self._workbook(directory, "titck.xlsx", [
                ["RUHSATLI BEŞERİ TIBBİ ÜRÜNLER LİSTESİ"], header,
                [1, 8699508010752, "ONADRON 0.75 MG TABLET, 100 TABLET", "dekzametazon", "H02AB02",
                 "MENARİNİ SAĞLIK VE İLAÇ SAN. TİC. A.Ş.", datetime(1960, 10, 1), "55/44", "", "", "", 0, ""],
                [2, 8699508010134, "ONADRON 0.75 MG TABLET, 20 TABLET", "dekzametazon", "H02AB02",
                 "MENARİNİ SAĞLIK VE İLAÇ SAN. TİC. A.Ş.", datetime(1960, 10, 1), "55/44", "", "", "", 0, ""],
                [3, 8699000000000, "EZEROS 10 MG/10 MG FILM KAPLI TABLET, 30 TABLET", "rosuvastatin ve ezetimib", "C10BA06",
                 "NUVOMED", datetime(2016, 1, 1), "231/97", "", "", "", 2, ""],
            ], sheet_title=europe_registers.TURKEY_SHEET)
            records = list(europe_registers.read_turkey(europe_registers.TITCK_TURKEY, path))
        rows = [row for _, row in europe_registers.build_turkey_rows(records, "2026-09-22")]

        self.assertEqual(len(rows), 2)
        onadron = rows[0]
        self.assertEqual(onadron["product"], "ONADRON 0.75 MG TABLET")
        self.assertEqual(onadron["pack_size"], "100 TABLET; 20 TABLET")
        self.assertEqual(onadron["registration_number"], "55/44")
        self.assertEqual(onadron["registration_date"], "1960-10-01")
        self.assertEqual(onadron["status"], "Licensed")
        self.assertEqual(rows[1]["substance"], "rosuvastatin; ezetimib")
        self.assertEqual(rows[1]["status"], "Suspended (pharmacovigilance)")
        self.assertTrue(row_relevant_to_substance(rows[1], "rosuvastatin + ezetimibe"))

    def test_slovakia_reads_the_monthly_list_and_its_code_tables(self):
        import datetime as dt
        import io
        import zipfile

        import openpyxl

        header = ["vym_id", "ŠÚKL kód", "Názov", "Doplnok", "lie_sila", "lie_balenie", "Kód držiteľa",
                  "Kód štátu", "ATC kód", "atc_nazov_sk", "Indikačná skupina", "Registračné číslo", "Exspirácia",
                  "Výdaj", "Typ registrácie", "Dátum registrácie",
                  "Platnosť do (D-neobmedzená, P-žiadosť o predĺženie)", "Bezpečnostný prvok", "pod_kod", "form_kod"]
        pack = ["", "1", "Ebymect 5 mg/1000 mg filmom obalené tablety", "tbl flm 196", "5 mg/1000 mg", 196,
                "AZ", "SE", "A10BD15", "Metformín a dapagliflozín", "18", "EU/1/15/1051/012", "24", "R", "EU",
                dt.datetime(2015, 11, 16), "D", "Áno", "por", "TBL FLM"]
        second = list(pack)
        second[3], second[11] = "tbl flm 56", "EU/1/15/1051/013"
        workbook = openpyxl.Workbook()
        for row in (header, pack, second):
            workbook.active.append(row)
        buffer = io.BytesIO()
        workbook.save(buffer)
        tables = {
            "firmy.xls": [["AZ", "AstraZeneca AB", "", "", "", "SE"]],
            "atc.xls": [["A10BD15", "Metformin and dapagliflozin", "Metformín a dapagliflozín", 1, 1]],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sukl.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("zoznam_liekov.xlsx", buffer.getvalue())
                for name in tables:
                    archive.writestr(name, b"")
            with patch.object(europe_registers, "_xls_rows", side_effect=lambda _archive, name: tables[name]):
                records = list(europe_registers.read_slovakia(europe_registers.SUKL_SLOVAKIA, path))
        rows = [row for _, row in europe_registers.build_slovakia_rows(records, "2026-10-04")]

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["registration_number"], "EU/1/15/1051")
        self.assertEqual(row["pack_size"], "tbl flm 196; tbl flm 56")
        self.assertEqual(row["substance"], "Metformin; dapagliflozin")
        self.assertEqual(row["company"], "AstraZeneca AB")
        self.assertEqual(row["status"], "Registered, unlimited validity")
        self.assertEqual(row["authorisation_scope"], "Centralised")
        self.assertEqual(row["dosage_form"], "Film-coated tablet")
        self.assertEqual(row["classification"], "Prescription only")
        self.assertEqual(row["registration_date"], "2015-11-16")
        self.assertTrue(row_relevant_to_substance(row, "metformin"))

    def test_latvia_names_eu_strengths_and_parallel_imports_apart(self):
        base = {
            "original_name": "Abasaglar", "active_substance": "Insulinum glarginum",
            "marketing_authorisation_holder": "Eli Lilly Nederland B.V., Netherlands",
            "manufacturer": "Lilly France, France", "parallel_importer_en": "", "status": "1", "prd_removed": "0",
            "atc_code": "A10AE04", "authorisation_procedure": "Eiropas centralizētā reģistrācijas procedūra",
            "strength": "100 units/ml", "pharmaceutical_form": "Solution for injection",
            "date_of_authorisation": "09-SEP-14", "summary_of_product_characteristics": "https://dati.zva.gov.lv/x/smpc",
        }
        packs = [
            {**base, "authorisation_no": "EU/1/14/944/003", "package_en": "Cartridge, N5"},
            {**base, "authorisation_no": "EU/1/14/944/007", "package_en": "Pre-filled pen, N5"},
            {**base, "authorisation_no": "EU/1/14/944/008", "package_en": "Pre-filled pen, N10", "prd_removed": "1"},
            {**base, "original_name": "Atoris 10 mg film-coated tablets", "active_substance": "Atorvastatinum",
             "authorisation_no": "04-0142", "authorisation_procedure": "Paralēlais imports",
             "parallel_importer_en": "Orivas UAB, Lithuania", "strength": "10 mg",
             "pharmaceutical_form": "Film-coated tablet", "package_en": "Blister, N30"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zva.json.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("HumanProducts.json", json.dumps(packs))
            records = list(europe_registers.read_latvia(europe_registers.ZVA_LATVIA, path))
        rows = [row for _, row in europe_registers.build_latvia_rows(records, "2026-09-22")]

        self.assertEqual([row["product"] for row in rows], [
            "Abasaglar 100 units/ml Solution for injection",
            "Atoris 10 mg film-coated tablets (parallel import: Orivas UAB, Lithuania)",
        ])
        abasaglar = rows[0]
        self.assertEqual(abasaglar["registration_number"], "EU/1/14/944")
        self.assertEqual(abasaglar["pack_size"], "Cartridge, N5; Pre-filled pen, N5")  # the removed pack is left out
        self.assertEqual(abasaglar["registration_date"], "2014-09-09")
        self.assertEqual(abasaglar["authorisation_scope"], "Centralised")
        self.assertEqual(abasaglar["manufacturer_name"], "Lilly France, France")
        self.assertEqual(rows[1]["authorisation_scope"], "Parallel import")
        self.assertTrue(row_relevant_to_substance(abasaglar, "insulin glargine"))

    def test_norway_resolves_english_substances_and_keeps_only_authorised_products(self):
        ns = "http://www.kith.no/xmlstds/eresept/forskrivning/2014-12-01"

        def product(item_id, name, company, kind, ref, form="Tablett"):
            return (
                f'<OppfLegemiddelMerkevare><Id>{item_id}</Id><Status V="A" DN="Aktiv oppføring" />'
                f'<LegemiddelMerkevare xmlns="{ns}"><Atc V="A10BA02" DN="Metformin" />'
                f'<NavnFormStyrke>{name}</NavnFormStyrke><Preparattype V="7" DN="{kind}" />'
                f'<LegemiddelformLang>{form}</LegemiddelformLang>'
                f'<SortertVirkestoffMedStyrke><RefVirkestoffMedStyrke>{ref}</RefVirkestoffMedStyrke></SortertVirkestoffMedStyrke>'
                f'<ProduktInfo><Produsent>{company}</Produsent></ProduktInfo>'
                f'<Preparatomtaleavsnitt><Lenke><Www V="https://produktinformasjon.legemiddelsok.no/{item_id}.pdf" /></Lenke></Preparatomtaleavsnitt>'
                f'</LegemiddelMerkevare></OppfLegemiddelMerkevare>'
            )

        xml = (
            '<?xml version="1.0" encoding="utf-8"?><FEST xmlns="http://www.kith.no/xmlstds/eresept/m30/2014-12-01">'
            "<KatLegemiddelMerkevare>"
            + product("P1", "Glucophage tab 500 mg", "Merck", "Legemiddel", "S1")
            + product("P2", "Glucophage tab 500 mg", "Orifarm AS", "Legemiddel", "S1")
            + product("P3", "Metformin Exempt tab 500 mg", "Importer", "Krever godkj. Fritak", "S1")
            + "</KatLegemiddelMerkevare><KatVirkestoff>"
            f'<OppfVirkestoff><Id>O1</Id><VirkestoffMedStyrke xmlns="{ns}"><Id>S1</Id><RefVirkestoff>V1</RefVirkestoff></VirkestoffMedStyrke></OppfVirkestoff>'
            f'<OppfVirkestoff><Id>O2</Id><Virkestoff xmlns="{ns}"><Id>V1</Id><Navn>Metforminhydroklorid</Navn><NavnEngelsk>Metformin hydrochloride</NavnEngelsk></Virkestoff></OppfVirkestoff>'
            "</KatVirkestoff></FEST>"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fest.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("fest251.xml", xml)
            records = list(europe_registers.read_norway(europe_registers.DMP_NORWAY, path))
        rows = [row for _, row in europe_registers.build_norway_rows(records, "2026-09-22")]

        self.assertEqual([row["product"] for row in rows], [
            "Glucophage tab 500 mg (Merck)", "Glucophage tab 500 mg (Orifarm AS)",
        ])
        self.assertEqual(rows[0]["substance"], "Metformin hydrochloride")
        self.assertEqual(rows[0]["dosage_form"], "Tablet")
        self.assertEqual(rows[0]["strength"], "500 mg")
        self.assertEqual(rows[0]["classification"], "Medicinal product")
        self.assertTrue(rows[0]["smpc_url"].endswith("P1.pdf"))

    def test_serbian_spellings_fold_to_the_english_search(self):
        from sources.grls_russia import fold
        from sources.open_registers import query_tokens

        def finds(serbian, english):
            have = set(europe_registers.serbian_tokens(serbian))
            return all(fold(token) in have for token in query_tokens(english))

        self.assertTrue(finds("deksametazon", "dexamethasone"))
        self.assertTrue(finds("ciprofloksacin", "ciprofloxacin"))
        self.assertTrue(finds("zoledronska kiselina", "zoledronic acid"))
        self.assertTrue(finds("acetilsalicilna kiselina", "acetylsalicylic acid"))
        self.assertTrue(finds("levotiroksin-natrijum", "levothyroxine sodium"))
        self.assertTrue(finds("metformin-hidrohlorid", "metformin hydrochloride"))
        self.assertFalse(finds("deksametazon", "dexpanthenol"))

    def test_serbia_groups_packs_under_the_decision_and_splits_the_manufacturer(self):
        def line(pack):
            return ";".join(f'"{value}"' for value in [
                "OBNOVA", "Arkinaza&#174;", "metformin", "R", f"film tableta; 1000mg; {pack}",
                "515-01-03379-22-001", "2023-08-03", "2028-08-03", "HEMOFARM AD VR&#352;AC - Republika Srbija ",
                "HEMOFARM AD VR&#352;AC", "A10BA02", "8600097408486", "1043062", "Humani lekovi", "494354", "/", "",
                "1081", "1081", "Beogradski put bb, Vr&#353;ac 26300, Republika Srbija", "oralno",
            ]) + ";"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lekovi_humani.csv"
            path.write_text("\n".join([line("blister, 2x15kom"), line("blister, 4x15kom")]), encoding="utf-8")
            records = list(europe_registers.read_serbia(europe_registers.ALIMS_SERBIA, path))
        rows = [row for _, row in europe_registers.build_serbia_rows(records, "2026-09-22")]

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["product"], "Arkinaza® 1000mg film tableta")
        self.assertEqual(row["pack_size"], "blister, 2x15kom; blister, 4x15kom")
        self.assertEqual(row["company"], "HEMOFARM AD VRŠAC")
        self.assertEqual((row["manufacturer_name"], row["manufacturer_country"]), ("HEMOFARM AD VRŠAC", "Republika Srbija"))
        self.assertEqual(row["expiry_date"], "2028-08-03")
        self.assertEqual(row["route"], "Oral use")
        self.assertTrue(row_relevant_to_substance(row, "metformin"))

    def test_malta_strips_quotes_and_splits_ingredients_from_strengths(self):
        header = ["[Medicine Name]", " [Active Ingredients]", " [Pharmaceutical Forms]", " [Therapeutic Class]",
                  " [Classification]", " [ATC Code]", " [Status]", " [Authorisation Number]", " [Authorisation Date]",
                  " [Authorization Holder]", " [Authorization Holder Address]", " [MAH Contact Details 1]",
                  " [MAH Contact Details 2]", " [Licence Number]"]
        with tempfile.TemporaryDirectory() as directory:
            path = self._workbook(directory, "malta_medicines.xlsx", [
                header,
                ["'Ezehron Duo 10 mg/10 mg, tablet'", " 'ROSUVASTATIN 10 milligram(s) | EZETIMIBE 10 milligram(s)'",
                 " 'TABLET'", " 'LIPID MODIFYING AGENTS'", " 'POM'", " 'C10BA06'", " 'Authorised'",
                 " 'MA948/00402'", " '24/01/2024'", " 'Adamed Sp. Z.o.o.'", " ''", " ''", " ''", " 'NOT APPLICABLE'"],
                ["'Eltroxin 100 microgram Tablets'", " 'LEVOTHYROXINE SODIUM 100 microgram(s)'", " 'TABLET'",
                 " 'THYROID THERAPY'", " 'POM'", " 'H03AA01'", " 'Authorised'", " 'PI1513/02103A'", " '01/02/2020'",
                 " 'Pharma Regulatory Services Ltd'", " ''", " ''", " ''", " 'United Kingdom - PL 39699/0035'"],
            ])
            records = list(europe_registers.read_malta(europe_registers.MALTA_MEDICINES, path))
        rows = [row for _, row in europe_registers.build_malta_rows(records, "2026-09-22")]

        combo, parallel = rows
        self.assertEqual(combo["substance"], "Rosuvastatin; Ezetimibe")
        self.assertEqual(combo["strength"], "10 milligram(s) / 10 milligram(s)")
        self.assertEqual(combo["registration_date"], "2024-01-24")
        self.assertEqual(combo["status"], "Authorised")
        self.assertTrue(row_relevant_to_substance(combo, "rosuvastatin + ezetimibe"))
        self.assertEqual(parallel["authorisation_scope"], "Parallel import")
        self.assertIn("United Kingdom - PL 39699/0035", parallel["document_type"])

    def test_bulgaria_groups_packs_and_restores_the_atc_code(self):
        self.assertEqual(europe_registers.bulgarian_atc("J01MA 2"), "J01MA02")
        self.assertEqual(europe_registers.bulgarian_atc("A10BD 15"), "A10BD15")
        header = ["Рег. №", "Идентификатор", "Търговско име", "Описание", "Лек. форма", "Лек. форма EN",
                  "Количество на акт.в-во", "Опаковка", "Обем/Дозова единица", "Количество в крайна опаковка",
                  "Притежател на РУ", "Държава /BG/", "INN", "АТС-Код", "Режим на предписване"]

        def pack(identifier, count):
            return [20020760, identifier, "Sophamet", "", "филмирани таблетки", "film-coated tablets",
                    "500 mg film-coated tablets", "Blister PVC/Al", "", count, "Софарма АД", "България",
                    "Metformin", "A10BA 2", "По лекарско предписание"]

        with tempfile.TemporaryDirectory() as directory:
            path = self._workbook(directory, "bda.xlsx", [
                header, pack(" BG / 20020760 / 001", 50), pack(" BG / 20020760 / 002", 100),
                [20190001, " BG / 20190001 / 001", "Co-Roswera", "", "", "film-coated tablets",
                 "10 mg/10 mg film-coated tablets", "Blister", "", 30, "KRKA", "Словения",
                 "Rosuvastatin and ezetimibe", "C10BA 6", "По лекарско предписание"],
            ])
            records = list(europe_registers.read_bulgaria(europe_registers.BDA_BULGARIA, path))
        rows = [row for _, row in europe_registers.build_bulgaria_rows(records, "2026-09-22")]

        sophamet, combo = rows
        self.assertEqual(sophamet["product"], "Sophamet 500 mg film-coated tablets")
        self.assertEqual(sophamet["pack_size"], "Blister PVC/Al x 50; Blister PVC/Al x 100")
        self.assertEqual(sophamet["atc_code"], "A10BA02")
        self.assertEqual(sophamet["registration_number"], "20020760")
        self.assertEqual(combo["substance"], "Rosuvastatin; ezetimibe")
        self.assertIn("holder in Slovenia", combo["document_type"])
        self.assertTrue(row_relevant_to_substance(combo, "rosuvastatin + ezetimibe"))

    def test_the_registers_are_wired_into_search(self):
        from services.search_pipeline import DEFAULT_SOURCES

        names = {item["name"] for item in connector_metadata()}
        for source, country, region in (("DMP Norway", "Norway", "EU"), ("SUKL Slovakia", "Slovakia", "EU"),
                                        ("ZVA Latvia", "Latvia", "EU"), ("TITCK Turkey", "Turkey", "ME"),
                                        ("ALIMS Serbia", "Serbia", "EU"), ("Malta Medicines Authority", "Malta", "EU"),
                                        ("BDA Bulgaria", "Bulgaria", "EU")):
            self.assertIn(source, names)
            self.assertIn(source, sources_for_scope(DEFAULT_SOURCES, country=country))
            self.assertIn(source, sources_for_scope(DEFAULT_SOURCES, region=region))


class MatchingVersionTests(unittest.TestCase):
    """The shared stemmer, and indexes built under older matching rules."""

    def test_ch_and_the_italian_idro_prefix_meet_the_english_spelling(self):
        from sources.grls_russia import fold, latin
        from sources.open_registers import inn_tokens, query_tokens

        wanted = set(query_tokens("losartan + hydrochlorothiazide"))
        for register_text in ("LOSARTAN POTASSICO/IDROCLOROTIAZIDE", "losartana potássica + hidroclorotiazida",
                              "LOSARTAN POTASICO; HIDROCLOROTIAZIDA"):
            self.assertTrue(wanted <= set(inn_tokens(register_text)), register_text)
        self.assertTrue(set(query_tokens("chlorhexidine")) <= set(inn_tokens("CLORHEXIDINA DIGLUCONATO")))
        # Russian keeps meeting English through the fold: Г for H, Х for CH.
        for russian, english in (("Гидрохлоротиазид", "hydrochlorothiazide"), ("Гепарин", "heparin"),
                                 ("Хлоргексидин", "chlorhexidine")):
            self.assertEqual([fold(t) for t in inn_tokens(latin(russian))], [fold(t) for t in inn_tokens(english)], russian)

    def test_an_index_built_under_older_rules_is_stale(self):
        import time

        from sources.open_registers import ITALY, MATCH_VERSION, _is_stale

        fresh = {"fetched_epoch": str(time.time()), "match_version": str(MATCH_VERSION)}
        self.assertFalse(_is_stale(ITALY, fresh))
        self.assertTrue(_is_stale(ITALY, {**fresh, "match_version": str(MATCH_VERSION - 1)}))
        self.assertTrue(_is_stale(ITALY, {"fetched_epoch": str(time.time())}))  # built before versions


class LatinAmericaTests(unittest.TestCase):
    def test_colombia_puts_a_product_back_together_from_its_rows(self):
        header = ["expediente", "producto", "titular", "registrosanitario", "fechaexpedicion", "fechavencimiento",
                  "estadoregistro", "descripcioncomercial", "estadocum", "atc", "viaadministracion",
                  "principioactivo", "cantidad", "unidadmedida", "unidadreferencia", "formafarmaceutica",
                  "nombrerol", "tiporol", "modalidad"]
        base = {"expediente": "20048021", "producto": "COUPET 10/20 MG TABLETAS", "titular": "LAFRANCOL S.A.S.",
                "registrosanitario": "INVIMA 2019M-0019123", "fechaexpedicion": "03/05/2019",
                "fechavencimiento": "03/05/2029", "estadoregistro": "Vigente", "estadocum": "Activo",
                "atc": "C10BA06", "viaadministracion": "ORAL", "unidadreferencia": "TABLETA",
                "formafarmaceutica": "TABLETA RECUBIERTA", "modalidad": "IMPORTAR Y VENDER"}
        rows = []
        for pack in ("CAJA X 30 TABLETAS", "CAJA X 10 TABLETAS"):
            for ingredient, amount in (("ROSUVASTATINA CALCICA", "20"), ("EZETIMIBA", "10")):
                for role, name in (("FABRICANTE", "LABORATORIOS ABC"), ("IMPORTADOR", "LAFRANCOL S.A.S.")):
                    rows.append({**base, "descripcioncomercial": pack, "principioactivo": ingredient,
                                 "cantidad": amount, "unidadmedida": "mg", "nombrerol": name, "tiporol": role})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invima_cum.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=header)
                writer.writeheader()
                writer.writerows(rows)
            records = list(americas_registers.read_colombia(americas_registers.INVIMA_COLOMBIA, path))
        built = [row for _, row in americas_registers.build_colombia_rows(records, "2026-09-22")]

        self.assertEqual(len(built), 1)
        row = built[0]
        self.assertEqual(row["substance"], "Rosuvastatina calcica; Ezetimiba")
        self.assertEqual(row["strength"], "20 mg/TABLETA / 10 mg/TABLETA")
        self.assertEqual(row["pack_size"], "CAJA X 30 TABLETAS; CAJA X 10 TABLETAS")
        self.assertEqual(row["manufacturer_name"], "LABORATORIOS ABC")
        self.assertIn("importer: LAFRANCOL S.A.S.", row["document_type"])
        # Month first: 3 May, not 5 March.
        self.assertEqual((row["registration_date"], row["expiry_date"]), ("2019-03-05", "2029-03-05"))
        self.assertEqual(row["status"], "Current")
        self.assertEqual(row["region"], "LA")
        self.assertTrue(row_relevant_to_substance(row, "rosuvastatin + ezetimibe"))

    def test_the_latin_america_region_includes_brazil(self):
        from services.search_pipeline import DEFAULT_SOURCES, _country_region, filter_rows

        self.assertEqual(sources_for_scope(DEFAULT_SOURCES, region="LA"), ["ANVISA Brazil", "INVIMA Colombia"])
        self.assertEqual(sources_for_scope(DEFAULT_SOURCES, country="Colombia"), ["INVIMA Colombia"])
        self.assertEqual((_country_region("Colombia"), _country_region("Brazil")), ("LA", "BR"))
        rows = [{"country": "Brazil", "region": "BR", "product": "a", "source": "ANVISA Brazil"},
                {"country": "Colombia", "region": "LA", "product": "b", "source": "INVIMA Colombia"},
                {"country": "Italy", "region": "EU", "product": "c", "source": "AIFA Italy"}]
        self.assertEqual([row["country"] for row in filter_rows(rows, region="LA")], ["Brazil", "Colombia"])
        self.assertEqual([row["country"] for row in filter_rows(rows, region="BR")], ["Brazil"])


class KazakhstanTests(unittest.TestCase):
    def test_russian_acids_meet_the_english_search(self):
        from sources.grls_russia import fold, match_text
        from sources.open_registers import query_tokens

        for russian, english in (("Золедроновая кислота", "zoledronic acid"),
                                 ("Ацетилсалициловая кислота", "acetylsalicylic acid"),
                                 ("Фолиевая кислота", "folic acid"), ("Вальпроевая кислота", "valproic acid"),
                                 ("Аскорбиновая кислота", "ascorbic acid")):
            have = set(match_text(russian).split())
            self.assertTrue(all(fold(token) in have for token in query_tokens(english)), russian)
        self.assertNotIn("acid", match_text("Метформин"))

    def test_kazakhstan_row_is_put_into_english(self):
        record = {
            "id": 1, "sourceId": "abc", "drugTradeName": "Лориста® Н", "regNumber": "ЛП-№001972-РГ-KZ",
            "mnn": "Гидрохлоротиазид + Лозартан", "lekFormName": "таблетки, покрытые пленочной оболочкой",
            "producerName": "1. КРКА, д.д., Ново место, 2. КРКА-РУС", "countryName": "1. СЛОВЕНИЯ, 2. РОССИЯ",
            "regDate": "2021-03-02T00:00:00", "expirationDate": None, "recipeSign": True, "sourceType": "Eaes",
            "atcCode": "C09DA01",
        }
        rows = list(asia_registers.build_kazakhstan_rows([record, dict(record)], "2026-09-22"))

        self.assertEqual(len(rows), 1)  # the same record twice is one row
        match, row = rows[0]
        self.assertEqual(row["manufacturer_country"], "Slovenia; Russia")
        self.assertEqual(len(row["manufacturer_name"].split("; ")), 2)
        self.assertEqual(row["company"], "")  # the register names the producer, not the holder
        self.assertEqual(row["authorisation_scope"], "EAEU registration")
        self.assertEqual((row["registration_date"], row["expiry_date"]), ("2021-03-02", "Unlimited"))
        self.assertEqual(row["classification"], "Prescription")
        # Transliterated; "№" is part of the number as NDDA writes it.
        self.assertNotRegex(row["registration_number"], "[А-Яа-яЁё]")
        self.assertTrue(row_relevant_to_substance(row, "losartan + hydrochlorothiazide"))

    def test_kazakhstan_is_wired_into_asia(self):
        from services.search_pipeline import DEFAULT_SOURCES

        self.assertIn("NDDA Kazakhstan", {item["name"] for item in connector_metadata()})
        self.assertEqual(sources_for_scope(DEFAULT_SOURCES, country="Kazakhstan"), ["NDDA Kazakhstan"])
        self.assertIn("NDDA Kazakhstan", sources_for_scope(DEFAULT_SOURCES, region="AS"))

if __name__ == "__main__":
    unittest.main()
