from __future__ import annotations

from typing import Any

from services.result_formatter import (
    assessment_report_url,
    first_value,
    manufacturer_country_value,
    manufacturer_name_value,
    pil_document_url,
    product_details_url,
    smpc_document_url,
)
from sources.parser import (
    extract_dosage_form,
    extract_pack_size,
    extract_registration_number,
    extract_strength,
)


ENRICHMENT_FIELD_LABELS = {
    "strength": "Strength",
    "dosage_form": "Dosage Form",
    "pack_size": "Pack Size",
    "ma_holder": "MA Holder",
    "manufacturer_name": "Manufacturer Name",
    "manufacturer_country": "Manufacturer Country",
    "registration_number": "Registration Number",
    "registration_date": "Registration Date",
    "product_details_url": "Product Details URL",
    "smpc_url": "SmPC URL",
    "pil_url": "PIL URL",
    "assessment_report_url": "Assessment Report URL",
}


def _field_values(row: dict[str, Any]) -> dict[str, str]:
    product = first_value(row.get("product"))
    return {
        "strength": first_value(row.get("strength"), extract_strength(product)),
        "dosage_form": first_value(row.get("dosage_form"), extract_dosage_form(product)),
        "pack_size": first_value(row.get("pack_size"), extract_pack_size(product)),
        "ma_holder": first_value(row.get("company"), row.get("mah"), row.get("ma_holder")),
        "manufacturer_name": manufacturer_name_value(row),
        "manufacturer_country": manufacturer_country_value(row),
        "registration_number": first_value(
            row.get("registration_number"),
            extract_registration_number(product),
        ),
        "registration_date": first_value(row.get("registration_date"), row.get("approval_date")),
        "product_details_url": product_details_url(row),
        "smpc_url": smpc_document_url(row),
        "pil_url": pil_document_url(row),
        "assessment_report_url": assessment_report_url(row),
    }


def missing_enrichment_fields(row: dict[str, Any]) -> list[str]:
    values = _field_values(row)
    return [
        label
        for field, label in ENRICHMENT_FIELD_LABELS.items()
        if not values.get(field)
    ]


def enrichment_metadata(row: dict[str, Any]) -> dict[str, str]:
    if row.get("connector_mode") == "manual_registry":
        return {
            "data_confidence": "Needs manual review",
            "enrichment_status": "Official registry handoff only",
            "missing_fields": "Product record not extracted",
            "ai_next_action": "Open official registry and extract product details.",
        }

    missing = missing_enrichment_fields(row)
    values = _field_values(row)
    ai_enriched = str(row.get("ai_enriched") or "").lower() == "true"
    ai_confidence = first_value(row.get("ai_confidence"))
    has_document = bool(values["smpc_url"] or values["pil_url"] or values["assessment_report_url"])
    has_identity = bool(row.get("product") and row.get("country") and row.get("source"))
    has_registration_or_url = bool(values["registration_number"] or values["product_details_url"])

    if ai_enriched and len(missing) <= 5:
        confidence = ai_confidence or "AI extracted"
    elif not has_identity:
        confidence = "Needs manual review"
    elif len(missing) <= 3 and has_registration_or_url:
        confidence = "High"
    elif len(missing) <= 7 and (has_registration_or_url or has_document):
        confidence = "Medium"
    else:
        confidence = "Low"

    if ai_enriched and missing:
        status = "AI extracted from regulatory document; review remaining missing fields"
    elif ai_enriched:
        status = "AI extracted from regulatory document"
    elif has_document and any(field in missing for field in ["Manufacturer Name", "Pack Size", "SmPC URL", "PIL URL"]):
        status = "Document parser enriched; AI review recommended for missing fields"
    elif has_document:
        status = "Document/parser enriched"
    elif missing:
        status = "Connector result; AI enrichment recommended"
    else:
        status = "Connector result complete; evidence verification required"

    next_action = ""
    if missing:
        next_action = "Run AI/PDF extraction for: " + ", ".join(missing[:6])

    return {
        "data_confidence": confidence,
        "enrichment_status": status,
        "missing_fields": ", ".join(missing) if missing else "None",
        "ai_next_action": next_action,
    }


def attach_ai_enrichment_metadata(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched_rows = []
    for row in rows:
        enriched = dict(row)
        metadata = enrichment_metadata(enriched)
        for key, value in metadata.items():
            enriched.setdefault(key, value)
        enriched_rows.append(enriched)
    return enriched_rows
