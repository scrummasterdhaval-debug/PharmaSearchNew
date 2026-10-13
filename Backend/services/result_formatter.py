from typing import Any
import re

from repository import region_for_country
from services.english_normalizer import english_row
from services.field_availability import drugs_at_fda_url, field_value
from services.therapeutic_category import short_therapeutic_category
from sources.parser import (
    clean_product_name,
    extract_dosage_form,
    extract_pack_size,
    extract_registration_number,
    extract_strength,
)


MISSING_VALUE = "Not available"


def first_value(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def display_value(*values: object) -> str:
    return first_value(*values) or MISSING_VALUE


def normalized_value(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def company_identity_key(value: object) -> str:
    text = normalized_value(value)
    text = re.sub(r"\b(?:limited|ltd|plc|inc|llc|gmbh|bv|b v|b\.v|sa|s a|s\.a|ag|kft|pty|pvt|private|company|co)\b", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def same_company_identity(left: object, right: object) -> bool:
    left_key = company_identity_key(left)
    right_key = company_identity_key(right)
    if not left_key or not right_key:
        return False
    return left_key == right_key or left_key.startswith(right_key) or right_key.startswith(left_key)


def clean_brand_name(product: object) -> str:
    return clean_product_name(product)


def distinct_url(value: object, *blocked_values: object) -> str:
    url = first_value(value)
    if not url:
        return ""
    normalized_url = url.rstrip("/").lower()
    blocked = {
        first_value(item).rstrip("/").lower()
        for item in blocked_values
        if first_value(item)
    }
    return "" if normalized_url in blocked else url


def product_details_url(item: dict[str, Any]) -> str:
    smpc_url = item.get("smpc_url", "")
    pil_url = item.get("pil_url", "")
    assessment_url = item.get("assessment_report_url", "")
    product_url = distinct_url(
        item.get("product_url") or item.get("url", ""),
        smpc_url,
        pil_url,
        assessment_url,
    )
    return first_value(product_url, item.get("source_url"))


def smpc_document_url(item: dict[str, Any]) -> str:
    smpc_url = item.get("smpc_url", "")
    product_url = item.get("product_url") or item.get("url", "")
    document_type = str(item.get("document_type") or "").upper()
    if smpc_url:
        return smpc_url
    if document_type in {"SPC", "SMPC"}:
        return product_url
    return ""


def pil_or_assessment_url(item: dict[str, Any]) -> str:
    smpc_url = smpc_document_url(item)
    pil_url = distinct_url(item.get("pil_url", ""), smpc_url)
    return first_value(pil_url, assessment_report_url(item))


def pil_document_url(item: dict[str, Any]) -> str:
    smpc_url = smpc_document_url(item)
    return distinct_url(item.get("pil_url", ""), smpc_url)


def assessment_report_url(item: dict[str, Any]) -> str:
    smpc_url = smpc_document_url(item)
    pil_url = item.get("pil_url", "")
    return distinct_url(item.get("assessment_report_url", ""), smpc_url, pil_url) or drugs_at_fda_url(item)


HOLDER_SUFFIX = "(licence holder)"


def manufacturer_or_holder(item: dict[str, Any], manufacturer: str) -> str:
    """The maker, or the licence holder said to be one.

    Most registers publish only the marketing authorisation holder: Italy names
    Organon for Atozet, not the plant that makes it. Leaving the column empty
    sends a buyer to look the company up elsewhere, and filling it silently
    would present a holder as a factory, so the holder is named and labelled.
    """
    from services.vendor_display import clean_manufacturer

    # Names from a structured list or a leaflet can carry addresses and
    # sentences; the column shows the names.
    manufacturer = clean_manufacturer(manufacturer)
    if manufacturer:
        return manufacturer
    holder = first_value(
        company_display_value(item), item.get("company"), item.get("mah"), item.get("ma_holder")
    )
    return f"{holder} {HOLDER_SUFFIX}" if holder else ""


def manufacturer_name_value(item: dict[str, Any]) -> str:
    manufacturer = first_value(item.get("manufacturer_name"))
    company = first_value(item.get("company"), item.get("mah"), item.get("ma_holder"))
    explicitly_sourced = bool(first_value(item.get("manufacturer_source")))
    if not manufacturer:
        return ""
    manufacturer_parts = [
        part.strip()
        for part in re.split(r"\s*;\s*", manufacturer)
        if part.strip()
    ]
    filtered_parts = [
        part
        for part in manufacturer_parts
        if not same_company_identity(part, company)
    ]
    if manufacturer_parts and not filtered_parts and not explicitly_sourced:
        return ""
    cleaned_manufacturer = "; ".join(filtered_parts) if filtered_parts else manufacturer
    if same_company_identity(cleaned_manufacturer, company) and not explicitly_sourced:
        return ""
    return cleaned_manufacturer


def company_display_value(item: dict[str, Any]) -> str:
    company = first_value(
        item.get("commercial_company"),
        item.get("brand_owner"),
        item.get("labeler_name"),
        item.get("sponsor"),
        item.get("applicant"),
    )
    holder = first_value(item.get("company"), item.get("mah"), item.get("ma_holder"))
    if company and not same_company_identity(company, holder):
        return company
    return ""


def manufacturer_country_value(item: dict[str, Any]) -> str:
    manufacturer_country = first_value(item.get("manufacturer_country"))
    product_country = first_value(item.get("country"))
    manufacturer = manufacturer_name_value(item)
    if not manufacturer:
        return ""
    if normalized_value(manufacturer_country) == normalized_value(product_country):
        explicit_source = first_value(item.get("manufacturer_source"))
        if not explicit_source:
            return ""
    return manufacturer_country


def structured_manufacturer_value(item: dict[str, Any], field: str) -> str:
    values = []
    for manufacturer in item.get("manufacturers") or []:
        if not isinstance(manufacturer, dict):
            continue
        value = first_value(manufacturer.get(field))
        if value and value not in values:
            values.append(value)
    return "; ".join(values)


def formatted_result_row(item: dict[str, Any], searched_substance: str = "") -> dict[str, str]:
    item = english_row(item)
    product = clean_brand_name(item.get("product", ""))
    country = first_value(item.get("country"))
    company = first_value(item.get("company"), item.get("mah"), item.get("ma_holder"))
    details_url = product_details_url(item)
    smpc_url = smpc_document_url(item)
    pil_url = pil_document_url(item)
    assessment_url = assessment_report_url(item)
    return {
        "molecule": display_value(
            item.get("searched_substance"),
            item.get("substance"),
            searched_substance,
        ),
        "product": display_value(product),
        "strength": field_value(item, "strength", item.get("strength"), extract_strength(product)),
        "dosage_form": field_value(item, "dosage_form", item.get("dosage_form"), extract_dosage_form(product)),
        "pack_size": field_value(item, "pack_size", item.get("pack_size"), extract_pack_size(product)),
        "atc_code": field_value(item, "atc_code", item.get("atc_code")),
        "therapeutic_category": field_value(
            item,
            "therapeutic_category",
            short_therapeutic_category(
                item.get("therapeutic_category"),
                item.get("searched_substance") or item.get("substance") or searched_substance,
                item.get("atc_code"),
            )
        ),
        # The commercial company where a registry names one apart from the
        # holder, otherwise the holder: an empty Company column on every row
        # hid the company a vendor is looking for.
        "company": field_value(item, "company", company_display_value(item), company),
        "ma_holder": field_value(item, "company", company),
        "manufacturer_name": field_value(
            item,
            "manufacturer_name",
            manufacturer_or_holder(
                item,
                first_value(structured_manufacturer_value(item, "name"), manufacturer_name_value(item)),
            ),
        ),
        "manufacturer_country": field_value(
            item, "manufacturer_country", structured_manufacturer_value(item, "country"), manufacturer_country_value(item)
        ),
        "manufacturer_address": field_value(
            item, "manufacturer_address", structured_manufacturer_value(item, "address")
        ),
        "manufacturer_role": field_value(
            item, "manufacturer_role", structured_manufacturer_value(item, "role")
        ),
        "applicant_sponsor": display_value(item.get("applicant_sponsor")),
        "verification_status": display_value(item.get("verification_status"), "UNVERIFIED"),
        "registration_status": display_value(item.get("status")),
        "registration_number": display_value(
            item.get("registration_number"),
            extract_registration_number(product),
        ),
        "registration_date": display_value(
            item.get("registration_date"),
            item.get("approval_date"),
            item.get("authorisation_date"),
        ),
        "country": display_value(country),
        "region": display_value(item.get("region"), region_for_country(country)),
        "source": display_value(item.get("source")),
        "product_details_url": details_url,
        "smpc_url": smpc_url,
        "pil_url": pil_url,
        "pil_assessment_url": first_value(pil_url, assessment_url),
        "assessment_report_url": assessment_url,
    }
