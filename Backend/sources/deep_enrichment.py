from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from io import BytesIO
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from core.logging_config import get_logger
from services.ai_client import ai_extract_regulatory_fields, ai_enabled
from sources.spain_cima import fetch_cima_detail
from sources.parser import (
    extract_atc_code,
    extract_dosage_form,
    extract_pack_size,
    extract_route,
    extract_strength,
)


EMA_BASE_URL = "https://www.ema.europa.eu"
MAX_EMA_PRODUCT_PAGES = 30
MAX_CIMA_DETAIL_PAGES = 60
MAX_PDF_URLS_PER_EXPORT = 60
MAX_PDF_PAGES = 5
PDF_EDGE_PAGES = 6
REQUEST_TIMEOUT = 12
logger = get_logger(__name__)
PDF_ENRICHMENT_SKIP_SOURCES = {
    "BPOM Indonesia",
    "DAV Vietnam",
    "NPRA Malaysia",
}
COUNTRY_NAMES = [
    "Australia",
    "Austria",
    "Belgium",
    "Bulgaria",
    "Canada",
    "Croatia",
    "Cyprus",
    "Czech Republic",
    "Denmark",
    "Estonia",
    "Finland",
    "France",
    "Germany",
    "Greece",
    "Hungary",
    "Iceland",
    "Ireland",
    "Italy",
    "Japan",
    "Latvia",
    "Lithuania",
    "Luxembourg",
    "Malta",
    "Netherlands",
    "Norway",
    "Poland",
    "Portugal",
    "Romania",
    "Slovakia",
    "Slovenia",
    "Spain",
    "Sweden",
    "Switzerland",
    "The Netherlands",
    "United Kingdom",
    "United States",
]


def _pdf_page_indices(page_count):
    front = range(min(PDF_EDGE_PAGES, page_count))
    back_start = max(PDF_EDGE_PAGES, page_count - PDF_EDGE_PAGES)
    back = range(back_start, page_count)
    return sorted(set([*front, *back]))


@lru_cache(maxsize=256)
def _ema_document_links(product_url):
    try:
        response = requests.get(
            product_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("EMA deep enrichment failed for %s: %s", product_url, exc)
        return {}

    soup = BeautifulSoup(response.text, "html.parser")
    links = {}
    for anchor in soup.select("a[href]"):
        href = urljoin(EMA_BASE_URL, anchor.get("href", ""))
        lower_href = href.lower()
        if "_en.pdf" not in lower_href:
            continue
        if "/product-information/" in lower_href and not links.get("product_information"):
            links["product_information"] = href
        elif "/assessment-report/" in lower_href and not links.get("assessment_report_url"):
            links["assessment_report_url"] = href

    product_information = links.get("product_information", "")
    if product_information:
        links.setdefault("smpc_url", product_information)
        links.setdefault("pil_url", product_information)
    return links


@lru_cache(maxsize=256)
def _pdf_text(pdf_url):
    try:
        response = requests.get(
            pdf_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "pdf" not in content_type and not response.content.startswith(b"%PDF"):
            logger.warning("PDF enrichment skipped non-PDF response: %s", pdf_url)
            return ""

        reader = PdfReader(BytesIO(response.content), strict=False)
        parts = []
        for page_index in _pdf_page_indices(len(reader.pages)):
            try:
                parts.append(reader.pages[page_index].extract_text() or "")
            except Exception as exc:
                logger.debug("PDF page extraction failed on page %s for %s: %s", page_index + 1, pdf_url, exc)
        return "\n".join(parts)
    except Exception as exc:
        logger.warning("PDF text extraction failed for %s: %s", pdf_url, exc)
        return ""


def _find_email(text):
    match = re.search(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        text or "",
        flags=re.IGNORECASE,
    )
    return match.group(0) if match else ""


def _find_phone(text):
    match = re.search(
        r"(?:Tel(?:ephone)?|Phone|Contact)[:\s]+(\+?\d[\d\s()./-]{6,}\d)",
        text or "",
        flags=re.IGNORECASE,
    )
    return " ".join(match.group(1).split()) if match else ""


def _clean_block(value):
    return " ".join(str(value or "").split()).strip(" :;-")


def _normalized_value(value):
    return _clean_block(value).lower()


def _extract_country(value):
    text = f" {_clean_block(value)} "
    for country in sorted(COUNTRY_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(country)}\b", text, flags=re.IGNORECASE):
            return "Netherlands" if country.lower() == "the netherlands" else country
    return ""


def _company_like(value):
    return bool(
        re.search(
            r"\b(?:ltd|limited|plc|gmbh|kft|s\.?a\.?|b\.?v\.?|inc|llc|pharma|pharmaceuticals?|laboratories|company)\b",
            str(value or ""),
            flags=re.IGNORECASE,
        )
    )


def _company_name_from_address_line(value):
    value = _clean_block(value)
    if not value:
        return ""
    first_part = _clean_block(value.split(",", 1)[0])
    if first_part and "," in value:
        return first_part
    if first_part and _company_like(first_part):
        return first_part
    return value


def _company_names_from_block(value):
    names = []
    for part in [item.strip() for item in str(value or "").split(";") if item.strip()]:
        name = _company_name_from_address_line(part)
        if _address_line_like(name):
            continue
        if name and name not in names and _company_like(name):
            names.append(name)
    return names


def _address_line_like(value):
    return bool(
        re.search(
            r"^(?:\d+|[a-z]?-?\d{3,}|floor\b|flat\b|unit\b|suite\b|building\b|"
            r"street\b|road\b|avenue\b|lane\b|drive\b)",
            _clean_block(value),
            flags=re.IGNORECASE,
        )
    )


def _extract_countries(value):
    countries = []
    text = f" {_clean_block(value)} "
    for country in sorted(COUNTRY_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(country)}\b", text, flags=re.IGNORECASE):
            normalized = "Netherlands" if country.lower() == "the netherlands" else country
            if normalized not in countries:
                countries.append(normalized)
    return "; ".join(countries)


def _section_after_exact_label(text, labels, max_lines=14, max_chars=700):
    lines = [_clean_block(line) for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    normalized_labels = {label.lower().rstrip(":") for label in labels}
    stop_pattern = re.compile(
        r"^(?:marketing authorisation holder|marketing authorization holder|ma holder|"
        r"holder of the marketing authorisation|for any information|this leaflet was last revised|"
        r"date of revision|package leaflet|contents of the pack|what .* contains|"
        r"\d+\.|annex|b\.|c\.)\b",
        flags=re.IGNORECASE,
    )

    for index, line in enumerate(lines):
        line_lower = line.lower().rstrip(":")
        if "marketing authorisation holder" in line_lower or "marketing authorization holder" in line_lower:
            continue

        label = next(
            (
                item
                for item in normalized_labels
                if line_lower == item or line_lower.startswith(f"{item}:")
            ),
            "",
        )
        if not label:
            continue

        remainder = _clean_block(line[len(label) :]).lstrip(": ")
        values = [remainder] if remainder else []
        for next_line in lines[index + 1 : index + 1 + max_lines]:
            next_lower = next_line.lower().rstrip(":")
            if stop_pattern.search(next_line):
                break
            if next_lower in normalized_labels:
                continue
            values.append(next_line)

        value = _clean_block("; ".join(item for item in values if item))
        if value:
            return value[:max_chars]
    return ""


def _manufacturer_from_pdf(text):
    if not text:
        return {}
    manufacturer = _section_after_exact_label(
        text,
        [
            "Manufacturer",
            "Manufacturers",
            "Manufacturer responsible for batch release",
            "Manufacturers responsible for batch release",
            "Manufacturer of the medicinal product",
            "Manufacturers of the medicinal product",
        ],
    )
    if manufacturer:
        company_names = _company_names_from_block(manufacturer)
        return {
            "manufacturer_name": "; ".join(company_names) if company_names else manufacturer,
            "manufacturer_country": _extract_countries(manufacturer),
            "manufacturer_address": manufacturer,
            "manufacturer_role": "MANUFACTURER_OR_BATCH_RELEASE_SITE",
            "manufacturer_source": "Regulatory document",
        }

    patterns = [
        r"(?:^|\n)\s*Manufacturer(?:s)?(?: responsible for batch release| of the medicinal product)?\s*:?\s*(.*?)(?:\n\s*(?:Marketing Authorisation Holder|For any information|This leaflet was last revised|Detailed information|Package leaflet|ANNEX|B\.\s|C\.\s)|$)",
        r"(?:^|\n)\s*Manufacturer\s*\n\s*(.*?)(?:\n\s*(?:Marketing Authorisation Holder|For any information|This leaflet was last revised|Detailed information|Package leaflet|ANNEX|B\.\s|C\.\s)|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        manufacturer = _clean_block(match.group(1))
        manufacturer = re.sub(
            r"^(?:responsible for batch release|of the medicinal product)\s*:?\s*",
            "",
            manufacturer,
            flags=re.IGNORECASE,
        )
        if manufacturer and len(manufacturer) >= 4:
            company_names = _company_names_from_block(manufacturer)
            return {
                "manufacturer_name": ("; ".join(company_names) if company_names else manufacturer)[:500],
                "manufacturer_country": _extract_countries(manufacturer),
                "manufacturer_address": manufacturer[:700],
                "manufacturer_role": "MANUFACTURER_OR_BATCH_RELEASE_SITE",
                "manufacturer_source": "Regulatory document",
            }
    return {}


def _pack_size_from_pdf(text):
    if not text:
        return ""
    lower = text.lower()
    available_match = re.search(
        r"Available in\s+(.*?)(?:Not all pack sizes may be marketed|"
        r"Marketing Authorisation Holder|Manufacturer:|This leaflet was last revised)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if available_match:
        value = " ".join(available_match.group(1).split())
        if value:
            return value[:500]

    for marker in ["pack size", "pack sizes", "package size", "package sizes"]:
        index = lower.find(marker)
        if index >= 0:
            value = extract_pack_size(text[index : index + 1200])
            if value:
                return value
    return extract_pack_size(text[:5000])


def _clean_therapeutic_text(value):
    text = " ".join(str(value or "").split())
    text = re.sub(r"^(therapeutic indications?|what .*? is used for)\s*[:.-]?\s*", "", text, flags=re.IGNORECASE)
    text = text.strip(" :-")
    if len(text) > 500:
        text = text[:500].rsplit(" ", 1)[0] + "..."
    return text


def _therapeutic_indication_from_pdf(text):
    if not text:
        return ""

    patterns = [
        r"(?:4\.1\s*)?Therapeutic indications?\s*(.*?)(?:\n\s*(?:4\.2|Posology|Dose|Method of administration|Contraindications)\b)",
        r"What .{0,80}? is used for\s*(.*?)(?:\n\s*(?:2\.|Before you|What you need to know|How to take|Warnings)\b)",
        r"\b(?:is|are) used (?:for|to treat)\s+(.*?)(?:\.|\n)",
        r"\b(?:indicated|indicated for)\s+(.*?)(?:\.|\n)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            cleaned = _clean_therapeutic_text(match.group(1))
            if len(cleaned) >= 12:
                return cleaned
    return ""


def _enrich_from_pdf_text(row, text, evidence_url=""):
    if not text:
        return row
    product = row.get("product", "")
    combined = f"{product}\n{text[:12000]}"
    if not row.get("strength"):
        row["strength"] = extract_strength(combined)
    if not row.get("dosage_form"):
        row["dosage_form"] = extract_dosage_form(combined)
    if not row.get("route"):
        row["route"] = extract_route(combined)
    if not row.get("atc_code"):
        row["atc_code"] = extract_atc_code(combined)
    if not row.get("pack_size"):
        row["pack_size"] = _pack_size_from_pdf(text)
    if not row.get("therapeutic_category"):
        row["therapeutic_category"] = _therapeutic_indication_from_pdf(text)
    # A manufacturer the connector read from a dedicated regulator field is
    # authoritative. Registries such as ANMDMR publish it separately from the
    # authorisation holder, and the two legitimately match when a company makes
    # its own product, so neither overwrite nor discard it here.
    explicitly_sourced = bool(str(row.get("manufacturer_source") or "").strip())
    manufacturer_metadata = _manufacturer_from_pdf(text)
    manufacturer_name = manufacturer_metadata.get("manufacturer_name")
    if manufacturer_name and not explicitly_sourced and (
        not row.get("manufacturer_name")
        or _normalized_value(row.get("manufacturer_name")) == _normalized_value(row.get("company"))
    ):
        row["manufacturer_name"] = manufacturer_name
        row["manufacturer_source"] = manufacturer_metadata.get("manufacturer_source", "")
    if manufacturer_metadata.get("manufacturer_country") and not row.get("manufacturer_country"):
        row["manufacturer_country"] = manufacturer_metadata["manufacturer_country"]
    if manufacturer_name:
        row.setdefault("manufacturers", []).append(
            {
                "name": manufacturer_name,
                "address": manufacturer_metadata.get("manufacturer_address", ""),
                "country": manufacturer_metadata.get("manufacturer_country", ""),
                "role": manufacturer_metadata.get("manufacturer_role", "MANUFACTURER_UNKNOWN_ROLE"),
                "verification_status": "VERIFIED_OFFICIAL_DOCUMENT",
            }
        )
        row.setdefault("evidence", []).append(
            {
                "field_name": "manufacturer_name",
                "value": manufacturer_name,
                "role": manufacturer_metadata.get("manufacturer_role", ""),
                "source_regulator": row.get("source", ""),
                "document_type": row.get("document_type", "Regulatory document"),
                "evidence_url": evidence_url,
                "evidence_section": "Manufacturer / batch release section",
                "extraction_method": "PDF_TEXT_SECTION_PARSER",
                "verification_status": "VERIFIED_OFFICIAL_DOCUMENT",
            }
        )
    if not row.get("manufacturer_email"):
        row["manufacturer_email"] = _find_email(text)
    if not row.get("manufacturer_phone"):
        row["manufacturer_phone"] = _find_phone(text)
    if not explicitly_sourced and _normalized_value(row.get("manufacturer_name")) == _normalized_value(
        row.get("company")
    ):
        row["manufacturer_name"] = ""
        row["manufacturer_country"] = ""
        row["manufacturer_source"] = ""
    if ai_enabled():
        ai_values = ai_extract_regulatory_fields(row, text)
        field_map = {
            "ma_holder": "company",
            "manufacturer_name": "manufacturer_name",
            "manufacturer_country": "manufacturer_country",
            "strength": "strength",
            "dosage_form": "dosage_form",
            "route": "route",
            "pack_size": "pack_size",
            "therapeutic_category": "therapeutic_category",
            "registration_number": "registration_number",
            "registration_date": "registration_date",
            "expiry_date": "expiry_date",
            "atc_code": "atc_code",
        }
        applied_fields = []
        for ai_field, row_field in field_map.items():
            value = _clean_block(ai_values.get(ai_field))
            if value and not row.get(row_field):
                row[row_field] = value
                applied_fields.append(row_field)
        if applied_fields:
            row["ai_enriched"] = "true"
            row["ai_enriched_fields"] = ", ".join(sorted(set(applied_fields)))
            row["ai_language_detected"] = ai_values.get("language_detected", "")
            row["ai_confidence"] = ai_values.get("confidence", "")
            row["ai_evidence"] = ai_values.get("evidence", "")
    return row


def _pdf_urls_for_row(row):
    if row.get("source") in PDF_ENRICHMENT_SKIP_SOURCES:
        return []
    urls = []
    for field in ["smpc_url", "pil_url", "assessment_report_url", "product_url", "url"]:
        value = str(row.get(field, ""))
        lower_value = value.lower()
        if value and (lower_value.endswith(".pdf") or "/docs/" in lower_value) and value not in urls:
            urls.append(value)
    return urls


def _pdf_url_for_row(row):
    urls = _pdf_urls_for_row(row)
    return urls[0] if urls else ""


def _enrich_pdf_fields(rows):
    pdf_urls = []
    seen = set()
    for row in rows:
        for pdf_url in _pdf_urls_for_row(row):
            if not pdf_url or pdf_url in seen:
                continue
            seen.add(pdf_url)
            pdf_urls.append(pdf_url)
            if len(pdf_urls) >= MAX_PDF_URLS_PER_EXPORT:
                break
        if len(pdf_urls) >= MAX_PDF_URLS_PER_EXPORT:
            break

    if not pdf_urls:
        return rows

    text_by_url = {}
    with ThreadPoolExecutor(max_workers=min(6, len(pdf_urls))) as executor:
        jobs = {executor.submit(_pdf_text, url): url for url in pdf_urls}
        for job in as_completed(jobs):
            text_by_url[jobs[job]] = job.result()

    for row in rows:
        for pdf_url in _pdf_urls_for_row(row):
            before = dict(row)
            _enrich_from_pdf_text(row, text_by_url.get(pdf_url, ""), evidence_url=pdf_url)
            if (
                row.get("strength")
                and row.get("dosage_form")
                and row.get("pack_size")
                and row.get("therapeutic_category")
            ):
                break
            if row == before:
                continue
    return rows


def _enrich_spain_cima_details(rows):
    """Fill ATC code and pack size from the CIMA per-product endpoint."""
    numbers = []
    for row in rows:
        if row.get("source") != "Spain CIMA":
            continue
        number = str(row.get("registration_number") or "").strip()
        if number and number not in numbers and not row.get("atc_code"):
            numbers.append(number)
        if len(numbers) >= MAX_CIMA_DETAIL_PAGES:
            break
    if not numbers:
        return

    detail_by_number = {}
    with ThreadPoolExecutor(max_workers=min(8, len(numbers))) as executor:
        jobs = {executor.submit(fetch_cima_detail, number): number for number in numbers}
        for job in as_completed(jobs):
            detail_by_number[jobs[job]] = job.result()

    for row in rows:
        if row.get("source") != "Spain CIMA":
            continue
        detail = detail_by_number.get(str(row.get("registration_number") or "").strip())
        if not detail:
            continue
        for field in ("atc_code", "pack_size", "registration_date"):
            if detail.get(field) and not row.get(field):
                row[field] = detail[field]


def enrich_deep_results(rows):
    _enrich_spain_cima_details(rows)
    ema_urls = sorted(
        {
            row.get("product_url") or row.get("url")
            for row in rows
            if row.get("source") == "EMA" and (row.get("product_url") or row.get("url"))
        }
    )[:MAX_EMA_PRODUCT_PAGES]

    link_by_url = {}
    if ema_urls:
        with ThreadPoolExecutor(max_workers=min(6, len(ema_urls))) as executor:
            jobs = {executor.submit(_ema_document_links, url): url for url in ema_urls}
            for job in as_completed(jobs):
                link_by_url[jobs[job]] = job.result()

    for row in rows:
        if row.get("source") != "EMA":
            continue
        product_url = row.get("product_url") or row.get("url")
        links = link_by_url.get(product_url, {})
        for field in ["smpc_url", "pil_url", "assessment_report_url"]:
            if links.get(field) and not row.get(field):
                row[field] = links[field]
        if links.get("product_information") and not row.get("document_type"):
            row["document_type"] = "Product information"

    return _enrich_pdf_fields(rows)
