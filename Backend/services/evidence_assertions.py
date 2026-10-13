"""What a stored row's own columns say about where each of its values came from.

A row records the regulator that supplied it and the URL it was read from, and
where a value came from somewhere other than that register it says so:
``manufacturer_source`` when a parser opened a document, ``completion_source``
when the value was agreed across a molecule group. That is enough to state, per
field, what the value rests on -- and the rules here claim nothing more than
those columns support.

Kept free of any database import so both the save path and the backfill can use
it: repository imports this module, and importing repository back would cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.field_availability import (
    DOCUMENT_PARSED_SOURCES,
    NOT_COLLECTED,
    NOT_SUPPLIED,
    PENDING_ENRICHMENT,
)


REGISTER_METHOD = "REGISTER_RECORD_BACKFILL"
DOCUMENT_METHOD = "DOCUMENT_FIELD_BACKFILL"
COMPLETION_METHOD = "CROSS_SOURCE_COMPLETION_BACKFILL"

REGISTER_SECTION = "Regulator register entry"
COMPLETION_SECTION = "Agreed across regulators for the same molecule"

VERIFIED_RECORD = "VERIFIED_REGULATOR_RECORD"
VERIFIED_DOCUMENT = "VERIFIED_OFFICIAL_DOCUMENT"
UNVERIFIED = "UNVERIFIED"

# Read straight off the regulator's own record by the connector that harvested
# the row. Nothing else fills them, so the register entry is their evidence.
REGISTER_FIELDS = (
    "product",
    "company",
    "applicant_sponsor",
    "status",
    "strength",
    "dosage_form",
    "pack_size",
    "registration_number",
    "registration_date",
    "expiry_date",
    "authorisation_scope",
)

# Listed by the register, which is what the assertion rests on -- the value is
# the document's address, not something read out of the document.
DOCUMENT_URL_FIELDS = ("smpc_url", "pil_url", "assessment_report_url")

# field_completion may agree these across a molecule group rather than read
# them from this row's own register, and completion_source says when it did.
COMPLETABLE_FIELDS = ("atc_code", "therapeutic_category")

# Filled either by a register field or by opening a document; the row's
# manufacturer_source is its own record of which.
MANUFACTURER_FIELDS = (
    "manufacturer_name",
    "manufacturer_country",
    "manufacturer_address",
    "manufacturer_role",
    "batch_release_manufacturer",
)

MANUFACTURER_ROLE = "MANUFACTURER_OR_BATCH_RELEASE_SITE"

ASSERTED_FIELDS = (
    *REGISTER_FIELDS,
    *DOCUMENT_URL_FIELDS,
    *COMPLETABLE_FIELDS,
    *MANUFACTURER_FIELDS,
)


def _text(value: object) -> str:
    return str(value or "").strip()


def register_url(row: dict[str, Any]) -> str:
    """Where the row was read from, most specific address first."""
    return _text(row.get("product_url")) or _text(row.get("source_url"))


def document_url(row: dict[str, Any]) -> str:
    """The document a parser would have opened for this row."""
    for field in ("smpc_url", "pil_url", "assessment_report_url", "product_url"):
        url = _text(row.get(field))
        if url:
            return url
    return ""


def missing_reason_for_row(row: dict[str, Any]) -> str:
    """Why this row has no manufacturer.

    The reasoning field_availability applies at display time, over the columns
    a stored row actually has. It answers for the manufacturer, which only a
    document parser fills and only for the registries that have one -- calling
    the rest "pending" would promise a job that will never run.

    It turns on the manufacturer's name alone. Asking for every manufacturer
    column would put a reason on rows that plainly name their manufacturer,
    because role and batch release site are almost never published, and a
    "Missing Reason" contradicting a filled "Manufacturer Name" in the same
    export row reads as a bug. Which individual columns are absent is
    field_availability's account to give, per field, at display time.
    """
    if _text(row.get("manufacturer_name")):
        return ""
    if not document_url(row):
        return NOT_SUPPLIED
    if _text(row.get("source")) in DOCUMENT_PARSED_SOURCES:
        return PENDING_ENRICHMENT
    return NOT_COLLECTED


def assertions_for_row(
    row: dict[str, Any],
    product_detail_id: int | None = None,
    skip_fields: set[str] | None = None,
) -> list[tuple]:
    """The per-field assertions the row's own provenance columns support.

    ``skip_fields`` leaves alone the fields a connector has already spoken for,
    so a value read out of a document keeps that stronger claim instead of
    being restated as a register record.
    """
    product_detail_id = product_detail_id if product_detail_id is not None else row["id"]
    skip = skip_fields or set()
    regulator = _text(row.get("source"))
    url = register_url(row)
    document_type = _text(row.get("document_type"))
    extracted_at = _text(row.get("last_checked")) or datetime.now(timezone.utc).isoformat()
    completion_source = _text(row.get("completion_source"))
    manufacturer_source = _text(row.get("manufacturer_source"))

    # Without an address to point at there is nothing to assert: an assertion
    # carrying no evidence_url is the unsourced value we are replacing.
    if not url:
        return []

    assertions: list[tuple] = []

    def add(
        field: str,
        value: str,
        *,
        evidence_url: str,
        section: str,
        method: str,
        status: str,
        source_regulator: str = "",
        role: str = "",
    ) -> None:
        assertions.append((
            product_detail_id, None, field, value, role,
            source_regulator or regulator, document_type, evidence_url, "",
            section, method, extracted_at, status, "",
        ))

    for field in (*REGISTER_FIELDS, *DOCUMENT_URL_FIELDS):
        value = _text(row.get(field))
        if value and field not in skip:
            add(
                field, value, evidence_url=url, section=REGISTER_SECTION,
                method=REGISTER_METHOD, status=VERIFIED_RECORD,
            )

    for field in COMPLETABLE_FIELDS:
        value = _text(row.get(field))
        if not value or field in skip:
            continue
        if completion_source:
            # Agreed across the molecule group, so it rests on those
            # regulators' records rather than on this row's register entry.
            add(
                field, value, evidence_url=url, section=COMPLETION_SECTION,
                method=COMPLETION_METHOD, status=UNVERIFIED,
                source_regulator=completion_source,
            )
        else:
            add(
                field, value, evidence_url=url, section=REGISTER_SECTION,
                method=REGISTER_METHOD, status=VERIFIED_RECORD,
            )

    from_document = "document" in manufacturer_source.lower()
    for field in MANUFACTURER_FIELDS:
        value = _text(row.get(field))
        if not value or field in skip:
            continue
        if from_document:
            add(
                field, value, evidence_url=document_url(row) or url,
                section=manufacturer_source, method=DOCUMENT_METHOD,
                status=VERIFIED_DOCUMENT, role=MANUFACTURER_ROLE,
            )
        else:
            add(
                field, value, evidence_url=url,
                section=manufacturer_source or REGISTER_SECTION,
                method=REGISTER_METHOD, status=VERIFIED_RECORD,
                role=MANUFACTURER_ROLE,
            )

    return assertions
