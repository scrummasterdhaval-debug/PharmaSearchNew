from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from io import BytesIO
import re
import unicodedata
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from core.logging_config import get_logger
from sources.parser import extract_pack_size


MHRA_BASE_URL = "https://products.mhra.gov.uk"
REQUEST_TIMEOUT = 20
MAX_PDF_PAGES = 8
PDF_EDGE_PAGES = 8
MAX_WORKERS = 6
logger = get_logger(__name__)
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
    "United Kingdom",
    "United States",
]


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip(" :;-")


def _pdf_page_indices(page_count: int) -> list[int]:
    front = range(min(PDF_EDGE_PAGES, page_count))
    back_start = max(PDF_EDGE_PAGES, page_count - PDF_EDGE_PAGES)
    back = range(back_start, page_count)
    return sorted(set([*front, *back]))


# The section headings that name the authorisation holder and the maker, in
# the languages the registries publish in. Every registry writes its SmPC in
# its own language, so looking only for the English heading found nothing at
# all in a Spanish, French or Belgian document: the parser read MHRA and
# Ireland and returned empty-handed everywhere else. Accented and unaccented
# spellings are both listed because PDF text extraction loses accents often
# enough to matter.
MA_HOLDER_LABELS = [
    "Marketing Authorisation Holder",
    "Marketing Authorization Holder",
    "MA Holder",
    "Holder of the marketing authorisation",
    "Titular de la autorización de comercialización",
    "Titular de la autorizacion de comercializacion",
    "Titulaire de l'autorisation de mise sur le marché",
    "Titulaire de l’autorisation de mise sur le marché",
    "Titulaire de l'autorisation de mise sur le marche",
    "Houder van de vergunning voor het in de handel brengen",
    "Houder van de vergunning",
    "Pharmazeutischer Unternehmer",
    "Zulassungsinhaber",
]

MANUFACTURER_LABELS = [
    "Manufacturer",
    "Manufacturers",
    "Manufacturer responsible for batch release",
    "Manufacturers responsible for batch release",
    "Manufacturer of the medicinal product",
    "Manufacturers of the medicinal product",
    "Responsable de la fabricación",
    "Responsable de la fabricacion",
    "Responsables de la fabricación",
    "Fabricante",
    "Fabricantes",
    "Fabricant",
    "Fabricants",
    "Fabrikant",
    "Fabrikanten",
    "Hersteller",
]

# The words that mark the start of a different section, used to stop a block
# before it swallows the next heading.
_HOLDER_TERMS = [label.lower() for label in MA_HOLDER_LABELS]
_MANUFACTURER_TERMS = [label.lower() for label in MANUFACTURER_LABELS]


def _squash(value: object) -> str:
    """A heading reduced to the letters and digits a reader would recognise.

    Extracting a Spanish SmPC gives back
    "7. TITULAR DE LA AUT     ORIZACI?N DE COMERCI    ALIZACI?N": the accented
    letters arrive as replacement characters and the words are broken by runs
    of spaces, so no literal heading can ever match. Comparing only the
    alphanumerics, with accents folded away, matches the heading as printed.
    """
    text = unicodedata.normalize("NFKD", str(value or "")).lower()
    return "".join(
        character
        for character in text
        if character.isalnum() and not unicodedata.combining(character)
    )


# Accent-free openings of the headings above. A label is matched as a prefix of
# the squashed heading, so these stop short of the first accented letter: the
# accent is exactly what PDF extraction tends to destroy.
_HOLDER_PREFIXES = [_squash(label) for label in MA_HOLDER_LABELS] + [
    # Spanish loses the accent in "autorización", French in "marché", so each
    # prefix runs up to that letter and no further. Stopping earlier would
    # match, but would leave the rest of the heading sitting in the value.
    _squash("Titular de la autorizaci"),
    _squash("Titulaire de l'autorisation de mise sur le march"),
    # The French register's own page shortens the heading to "Titulaire de
    # l'autorisation : KENVUE France", so the SmPC-length prefix never matches
    # there. Both forms are listed and the longer wins where both apply.
    _squash("Titulaire de l'autorisation"),
]
_MANUFACTURER_PREFIXES = [_squash(label) for label in MANUFACTURER_LABELS] + [
    _squash("Responsable de la fabricaci"),
    _squash("Responsables de la fabricaci"),
]


def _matches_label(line: str, prefixes: list[str]) -> str:
    """The prefix this heading begins with, or empty if it is not a heading.

    A leading section number is dropped first, since registries number their
    sections and the number is not part of the heading.
    """
    squashed = _squash(re.sub(r"^\s*\d+[\.\)]?\s*", "", line))
    if not squashed:
        return ""
    return next(
        (prefix for prefix in sorted(prefixes, key=len, reverse=True)
         if prefix and squashed.startswith(prefix)),
        "",
    )


def _label_alternation(labels: list[str]) -> str:
    """A regex alternation of labels, longest first so the fuller heading wins."""
    return "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))


def _is_metadata_label(line: str) -> bool:
    labels = _label_alternation(MA_HOLDER_LABELS + MANUFACTURER_LABELS)
    return bool(
        re.search(
            rf"(?:{labels}|batch release|contents of the pack|pack sizes?|package sizes?)",
            line,
            flags=re.IGNORECASE,
        )
    )


def _value_after_label(text: str, labels: list[str], max_lines: int = 6, max_chars: int = 400) -> str:
    lines = [_clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    normalized_labels = [label.lower() for label in labels]
    for index, line in enumerate(lines):
        line_lower = line.lower().rstrip(":")
        label = next((item for item in normalized_labels if item in line_lower), "")
        if not label:
            continue

        remainder = _clean_text(re.sub(re.escape(label), "", line, flags=re.IGNORECASE))
        remainder = remainder.lstrip(": ")
        candidates = [remainder] if remainder else []
        for next_line in lines[index + 1 : index + 1 + max_lines]:
            if _is_metadata_label(next_line):
                break
            candidates.append(next_line)

        combined_candidates = _clean_text("; ".join(item for item in candidates if item))
        embedded_companies = _company_names_from_block(combined_candidates)
        company_candidate = (
            "; ".join(embedded_companies)
            if embedded_companies
            else next((item for item in candidates if _company_like(item)), "")
        )
        value = company_candidate or " ".join(item for item in candidates if item)
        if value:
            return value[:max_chars].strip()
    return ""


def _section_after_exact_label(
    text: str,
    labels: list[str],
    max_lines: int = 14,
    max_chars: int = 700,
    excluded_terms: list[str] | None = None,
    continuation_terms: list[str] | None = None,
) -> str:
    lines = [_clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    normalized_labels = {label.lower().rstrip(":") for label in labels}
    label_prefixes = [_squash(label) for label in labels]
    if labels is MANUFACTURER_LABELS:
        label_prefixes = _MANUFACTURER_PREFIXES
    elif labels is MA_HOLDER_LABELS:
        label_prefixes = _HOLDER_PREFIXES
    excluded_terms = [term.lower() for term in (excluded_terms or [])]
    continuation_terms = [term.lower() for term in (continuation_terms or [])]
    stop_pattern = re.compile(
        rf"^(?:{_label_alternation(MA_HOLDER_LABELS)}|"
        r"for any information|this leaflet was last revised|"
        r"date of revision|package leaflet|contents of the pack|what .* contains|"
        r"\d+\.|annex|b\.|c\.)\b",
        flags=re.IGNORECASE,
    )

    for index, line in enumerate(lines):
        line_lower = line.lower().rstrip(":")
        if any(term in line_lower for term in excluded_terms):
            continue

        prefix = _matches_label(line, label_prefixes)
        if not prefix:
            continue

        # Drop the heading itself, keeping whatever followed it on the line.
        consumed = 0
        seen = 0
        for position, character in enumerate(line):
            if _squash(character):
                seen += 1
            if seen >= len(prefix):
                consumed = position + 1
                break
        remainder = _clean_text(line[consumed:])
        remainder = remainder.lstrip(": ")
        # A heading that carries its value on the same line is already
        # complete. The French register writes "Titulaire de l'autorisation :
        # KENVUE France" and follows it with an unrelated line about
        # prescription conditions, which reading on would have appended.
        if (
            remainder
            and len(_squash(remainder)) >= 4
            and not _is_metadata_label(remainder)
        ):
            return remainder[:max_chars].strip()
        values = [remainder] if remainder else []
        for next_line in lines[index + 1 : index + 1 + max_lines]:
            if stop_pattern.search(next_line):
                break
            if _is_metadata_label(next_line) and not any(
                term in next_line.lower() for term in continuation_terms
            ):
                break
            if next_line.lower().rstrip(":") in normalized_labels:
                continue
            values.append(next_line)

        value = _clean_text("; ".join(item for item in values if item))
        if value:
            return value[:max_chars].strip()
    return ""


def _company_like(value: str) -> bool:
    return bool(
        re.search(
            r"\b(?:ltd|limited|plc|gmbh|kft|s\.?a\.?|b\.?v\.?|inc|llc|pharma|pharmaceuticals?|laboratories|company)\b",
            value,
            flags=re.IGNORECASE,
        )
    )


def _company_name_from_address_line(value: str) -> str:
    value = _clean_text(value)
    if not value:
        return ""
    first_part = _clean_text(value.split(",", 1)[0])
    if first_part and "," in value:
        return first_part
    if first_part and _company_like(first_part):
        return first_part
    return value


def _company_names_from_block(value: str, allow_address_name: bool = False) -> list[str]:
    names = []
    company_pattern = re.compile(
        r"\b(?:[A-Z][A-Za-z0-9&.'’/-]*\s+){1,7}"
        r"(?:Ltd\.?|Limited|PLC|GmbH|Kft|S\.?A\.?|B\.?V\.?|Inc\.?|LLC|Company)\b",
    )
    for match in company_pattern.finditer(value):
        name = _clean_text(match.group(0))
        # A valid company fragment must contain at least one legal suffix and
        # should not begin with common leaflet narrative.
        name = re.sub(
            r"^(?:and|or|the|this|leaflet|medicine|medicinal product)\s+",
            "",
            name,
            flags=re.IGNORECASE,
        )
        identity = name.rstrip(".").casefold()
        if name and all(existing.rstrip(".").casefold() != identity for existing in names):
            names.append(name)
    if names:
        return names
    for part in [item.strip() for item in value.split(";") if item.strip()]:
        name = _company_name_from_address_line(part)
        if _address_line_like(name):
            continue
        if name and name not in names and (_company_like(name) or (allow_address_name and "," in part)):
            names.append(name)
    return names


def _manufacturer_countries(text: str, manufacturer: str, fallback_block: str) -> str:
    countries: list[str] = []
    normalized_text = _clean_text(text)
    for company in [item.strip() for item in manufacturer.split(";") if item.strip()]:
        match = re.search(re.escape(company), normalized_text, flags=re.IGNORECASE)
        if not match:
            continue
        nearby = normalized_text[match.end() : match.end() + 350]
        for country in _extract_countries(nearby).split(";"):
            country = country.strip()
            if country and country not in countries:
                countries.append(country)
    return "; ".join(countries) or _extract_countries(fallback_block)


def _address_line_like(value: str) -> bool:
    return bool(
        re.search(
            r"^(?:\d+|[a-z]?-?\d{3,}|floor\b|flat\b|unit\b|suite\b|building\b|"
            r"street\b|road\b|avenue\b|lane\b|drive\b)",
            _clean_text(value),
            flags=re.IGNORECASE,
        )
    )


def _invalid_manufacturer_value(value: object) -> bool:
    text = _clean_text(value)
    return bool(
        len(text) > 180
        or re.search(
            r"\b(?:this leaflet|contents of the pack|active substance|"
            r"medicine is|medicinal product is|last revised)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _extract_ma_holder(text: str) -> str:
    holder = _section_after_exact_label(
        text,
        MA_HOLDER_LABELS,
        max_lines=6,
        max_chars=400,
        excluded_terms=_MANUFACTURER_TERMS,
        continuation_terms=_HOLDER_TERMS,
    )
    if holder:
        company_names = _company_names_from_block(holder, allow_address_name=True)
        return company_names[0] if company_names else holder

    holder = _value_after_label(text, MA_HOLDER_LABELS)
    if holder and holder.lower() != "and manufacturer":
        return holder

    lines = [_clean_text(line) for line in text.splitlines() if _clean_text(line)]
    for index, line in enumerate(lines):
        lowered = line.lower()
        if not any(term in lowered for term in _HOLDER_TERMS):
            continue
        for candidate in lines[index + 1 : index + 5]:
            if _company_like(candidate):
                return candidate
    return ""


def _extract_manufacturer(text: str) -> str:
    manufacturer_blocks = _extract_manufacturer_blocks(text)
    for manufacturer in manufacturer_blocks:
        company_names = _company_names_from_block(manufacturer)
        if company_names:
            return "; ".join(company_names)
    if manufacturer_blocks:
        return manufacturer_blocks[0]

    block_match = re.search(
        r"(?:^|\n)\s*Manufacturer(?:s responsible for batch release| responsible for batch release)?"
        r"\s*:\s*(.*?)(?:This leaflet was last revised|Marketing Authorisation Holder|"
        r"Package leaflet|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if block_match:
        value = _clean_text(block_match.group(1))
        if value:
            company_names = _company_names_from_block(value)
            return ("; ".join(company_names) if company_names else value)[:500]

    manufacturer = _value_after_label(text, MANUFACTURER_LABELS)
    if manufacturer:
        return manufacturer

    lines = [_clean_text(line) for line in text.splitlines() if _clean_text(line)]
    for index, line in enumerate(lines):
        lowered = line.lower()
        if not any(term in lowered for term in _MANUFACTURER_TERMS):
            continue
        for candidate in lines[index + 1 : index + 6]:
            if _company_like(candidate):
                return candidate
    return ""


def _extract_manufacturer_block(text: str) -> str:
    blocks = _extract_manufacturer_blocks(text)
    return blocks[0] if blocks else ""


def _extract_manufacturer_blocks(text: str) -> list[str]:
    first_block = _section_after_exact_label(
        text,
        MANUFACTURER_LABELS,
        excluded_terms=_HOLDER_TERMS,
        continuation_terms=_MANUFACTURER_TERMS,
    )
    blocks = [first_block] if first_block else []

    pattern = re.compile(
        rf"(?:^|\n)\s*(?:{_label_alternation(MANUFACTURER_LABELS)})\s*:?\s*\n?"
        rf"(.*?)(?=\n\s*(?:{_label_alternation(MA_HOLDER_LABELS)}|"
        r"For any information|This leaflet was last revised|Date of revision|"
        r"Package leaflet|Contents of the pack|What .* contains|\d+\.|ANNEX|B\.|C\.)\b|$)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        value = _clean_text(match.group(1))
        if value and value not in blocks:
            blocks.append(value[:700])
    return blocks


def _extract_country(value: str) -> str:
    text = f" {value or ''} "
    for country in sorted(COUNTRY_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(country)}\b", text, flags=re.IGNORECASE):
            return country
    return ""


def _extract_countries(value: str) -> str:
    countries = []
    text = f" {value or ''} "
    for country in sorted(COUNTRY_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(country)}\b", text, flags=re.IGNORECASE):
            normalized = "Netherlands" if country.lower() == "the netherlands" else country
            if normalized not in countries:
                countries.append(normalized)
    return "; ".join(countries)


def _extract_pack_information(text: str) -> str:
    for marker in [
        "Pack size",
        "Pack sizes",
        "Package size",
        "Package sizes",
        "Contents of the pack",
        "What the pack contains",
    ]:
        index = text.lower().find(marker.lower())
        if index >= 0:
            section = _clean_text(text[index : index + 700])
            sentence_match = re.search(
                r"(?:available|supplied|presented|marketed)?\s*(?:in\s+)?"
                r"(?:packs?|boxes?|cartons?|blisters?|bottles?)\s+(?:of|containing)\s+"
                r"[\d,\s]+(?:and|or|to|-)?[\d,\s]*"
                r"(?:tablets?|capsules?|sachets?|vials?|ampoules?|patch(?:es)?|doses?)",
                section,
                flags=re.IGNORECASE,
            )
            if sentence_match:
                return _clean_text(sentence_match.group(0))[:250]
            pack_size = extract_pack_size(section)
            if pack_size:
                return pack_size
    return ""


def _document_links_from_html(soup: BeautifulSoup) -> dict[str, str]:
    links = {}
    for anchor in soup.select("a[href]"):
        href = urljoin(MHRA_BASE_URL, anchor.get("href", ""))
        text = anchor.get_text(" ", strip=True).lower()
        combined = f"{text} {href.lower()}"
        if not links.get("smpc_url") and (
            "spc" in combined or "summary of product characteristics" in combined
        ):
            links["smpc_url"] = href
        elif not links.get("pil_url") and (
            "pil" in combined or "patient information leaflet" in combined or "package leaflet" in combined
        ):
            links["pil_url"] = href
        elif not links.get("assessment_report_url") and (
            "par" in combined or "public assessment report" in combined
        ):
            links["assessment_report_url"] = href
    return links


def _metadata_from_text(text: str) -> dict[str, str]:
    manufacturer = _extract_manufacturer(text)
    manufacturer_block = _extract_manufacturer_block(text) or manufacturer
    metadata = {
        "company": _extract_ma_holder(text),
        "manufacturer_name": manufacturer,
        "manufacturer_country": _manufacturer_countries(text, manufacturer, manufacturer_block),
        "pack_size": _extract_pack_information(text),
    }
    if manufacturer:
        # Named per row by the caller, which knows the registry the document
        # came from. Stamping "MHRA document" on a Spanish or French row was
        # crediting the wrong regulator for the value.
        metadata["manufacturer_source"] = "document"
    return metadata


def _pdf_text(content: bytes) -> str:
    reader = PdfReader(BytesIO(content), strict=False)
    parts = []
    for page_index in _pdf_page_indices(len(reader.pages)):
        try:
            page = reader.pages[page_index]
            try:
                text = page.extract_text(extraction_mode="layout") or ""
            except (TypeError, ValueError):
                text = page.extract_text() or ""
            parts.append(text)
        except Exception as exc:
            logger.debug("MHRA PDF page extraction failed on page %s: %s", page_index + 1, exc)
    return "\n".join(parts)


@lru_cache(maxsize=512)
def parse_mhra_document_url(document_url: str) -> dict[str, str]:
    if not document_url:
        return {}

    response = None
    try:
        for attempt in range(2):
            try:
                response = requests.get(
                    document_url,
                    timeout=REQUEST_TIMEOUT,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt:
                    raise
    except requests.RequestException as exc:
        logger.warning("MHRA document metadata request failed for %s: %s", document_url, exc)
        return {}
    if response is None:
        return {}

    metadata: dict[str, str] = {}
    content_type = response.headers.get("content-type", "").lower()
    try:
        if "pdf" in content_type or response.content.startswith(b"%PDF"):
            metadata.update(_metadata_from_text(_pdf_text(response.content)))
        else:
            soup = BeautifulSoup(response.text, "html.parser")
            metadata.update(_document_links_from_html(soup))
            metadata.update(_metadata_from_text(soup.get_text("\n", strip=True)))
    except Exception as exc:
        logger.warning("MHRA document metadata parsing failed for %s: %s", document_url, exc)
        return {}

    return {key: value for key, value in metadata.items() if value}


def enrich_mhra_document_metadata(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    document_urls = sorted(
        {
            row.get(field)
            for row in rows
            for field in [
                "smpc_url",
                "pil_url",
                "assessment_report_url",
                "product_url",
                "url",
            ]
            if row.get(field)
        }
    )
    if not document_urls:
        return rows

    metadata_by_url = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(document_urls))) as executor:
        jobs = {executor.submit(parse_mhra_document_url, url): url for url in document_urls}
        for job in as_completed(jobs):
            metadata_by_url[jobs[job]] = job.result()

    for row in rows:
        metadata: dict[str, str] = {}
        for field in [
            "smpc_url",
            "pil_url",
            "assessment_report_url",
            "product_url",
            "url",
        ]:
            url = row.get(field)
            for metadata_field, metadata_value in metadata_by_url.get(url, {}).items():
                is_assessment_document = field == "assessment_report_url" or (
                    field in {"product_url", "url"}
                    and row.get("document_type") == "PAR"
                    and url == row.get("assessment_report_url")
                )
                if is_assessment_document and metadata_field in {
                    "company",
                    "manufacturer_name",
                    "manufacturer_country",
                    "manufacturer_source",
                    "pack_size",
                }:
                    continue
                if metadata_value and not metadata.get(metadata_field):
                    metadata[metadata_field] = metadata_value
        for field in [
            "company",
            "manufacturer_name",
            "manufacturer_country",
            "manufacturer_source",
            "pack_size",
            "smpc_url",
            "pil_url",
            "assessment_report_url",
        ]:
            current_value = _clean_text(row.get(field, ""))
            metadata_value = metadata.get(field)
            should_replace_holder_copy = (
                field == "manufacturer_name"
                and current_value
                and current_value.lower() == _clean_text(row.get("company", "")).lower()
            )
            should_replace_invalid = (
                field == "manufacturer_name"
                and _invalid_manufacturer_value(current_value)
            )
            if metadata_value and (
                not current_value or should_replace_holder_copy or should_replace_invalid
            ):
                if field == "manufacturer_source" and metadata_value == "document":
                    source = _clean_text(row.get("source", ""))
                    metadata_value = f"{source} document" if source else "document"
                row[field] = metadata_value
        if (
            _clean_text(row.get("manufacturer_name", "")).lower()
            == _clean_text(row.get("company", "")).lower()
            and not row.get("manufacturer_source")
        ):
            row["manufacturer_name"] = ""
            row["manufacturer_country"] = ""
            row["manufacturer_source"] = ""
        # Distinguish a completed document parse with no published value from
        # a cached row whose documents have not been inspected yet.
        row["document_enrichment_attempted"] = True

    return rows
