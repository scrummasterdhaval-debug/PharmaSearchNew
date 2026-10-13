from __future__ import annotations

import re
from typing import Any


NOT_SUPPLIED = "NOT_PUBLISHED"
# What a reader sees for NOT_SUPPLIED. The code stays in the store and in
# evidence; a table full of "NOT_PUBLISHED" reads as a system error.
NOT_SUPPLIED_LABEL = "Not published by regulator"
PENDING_ENRICHMENT = "Pending document enrichment"
NOT_APPLICABLE = "Not applicable for this source"
NOT_COLLECTED = "Not collected for this source"
# The US has no separate patient leaflet: the Medication Guide or patient
# information is a section of the label DailyMed publishes, already linked as
# the SmPC. "Not collected" said the data was missing when it is one click away.
US_PIL_IN_LABEL = "Included in the US label (see SmPC link)"

DRUGS_AT_FDA_URL = "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={number}"


def drugs_at_fda_url(item: dict[str, Any]) -> str:
    """The Drugs@FDA page for an NDA or ANDA: approval letters, labels and FDA's reviews.

    It is the US counterpart of an assessment report. OTC monograph products
    (M012, 505G(a)(3)) have no application and so no page. Biologics are left
    out: Drugs@FDA covers only those CDER regulates, so a BLA's page may be
    empty, and the number does not say which.
    """
    if str(item.get("source") or "").strip() != "FDA":
        return ""
    number = str(item.get("registration_number") or item.get("application_number") or "").upper()
    match = re.fullmatch(r"\s*(?:NDA|ANDA)\s*0*(\d{3,6})\s*", number)
    return DRUGS_AT_FDA_URL.format(number=match.group(1).zfill(6)) if match else ""

# The registries whose documents are actually opened and read for the fields
# below. Only MHRA has a parser: enrich_mhra_document_metadata runs for MHRA
# rows and no others. A row from any other registry is never going to be
# enriched, so it must not be labelled as waiting for enrichment -- "pending"
# promises a job that will never run, and it was saying so on 88% of rows.
DOCUMENT_PARSED_SOURCES = {"MHRA"}

DOCUMENT_FIELDS = {"smpc_url", "pil_url", "assessment_report_url"}
DOCUMENT_ENRICHMENT_FIELDS = {
    "pack_size",
    "manufacturer_name",
    "manufacturer_country",
    "manufacturer_address",
    "manufacturer_role",
    "batch_release_manufacturer",
}
DOCUMENT_CAPABLE_SOURCES = {
    "MHRA",
    "EMA",
    "EU MRI Product Index",
    "Belgium FAMHP",
    "Ireland medicines.ie",
    "Spain CIMA",
    "ANMDMR Romania",
}


def missing_field_value(item: dict[str, Any], field: str) -> str:
    """Explain why a normalized regulatory field has no value."""
    source = str(item.get("source") or "").strip()
    if source == "FDA" and field == "pil_url" and item.get("smpc_url"):
        return US_PIL_IN_LABEL
    if str(item.get("connector_mode") or "").strip() == "manual_registry":
        return "SOURCE_UNSUPPORTED"
    if item.get("access_blocked"):
        return "ACCESS_BLOCKED"
    if item.get("parser_failed"):
        return "PARSER_FAILED"
    if (
        field in DOCUMENT_FIELDS | DOCUMENT_ENRICHMENT_FIELDS
        and item.get("document_enrichment_attempted")
    ):
        return first_nonempty(item.get(f"{field}_missing_reason"), item.get("missing_reason"), NOT_SUPPLIED)
    if field in DOCUMENT_FIELDS | DOCUMENT_ENRICHMENT_FIELDS and any(
        item.get(name)
        for name in ("product_url", "url", "source_url", "smpc_url", "pil_url", "assessment_report_url")
    ):
        # There is a document to read, but only say it is pending where
        # something will actually read it.
        if source in DOCUMENT_PARSED_SOURCES:
            return PENDING_ENRICHMENT
        return NOT_COLLECTED
    return NOT_SUPPLIED


def first_nonempty(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def field_value(item: dict[str, Any], field: str, *values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    missing = missing_field_value(item, field)
    return NOT_SUPPLIED_LABEL if missing == NOT_SUPPLIED else missing
