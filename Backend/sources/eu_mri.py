from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from core.logging_config import get_logger
from sources.ema import EU_COUNTRIES
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength


MRI_PRODUCT_SEARCH_URL = "https://mri-production.cts-mrp.eu/product-search"
MRI_LEGACY_SEARCH_URL = "https://mri.cts-mrp.eu/Human/Product/FullTextSearch"
REQUEST_TIMEOUT = 20
MAX_RESULTS = 50
logger = get_logger(__name__)


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _source_url(substance: str) -> str:
    return f"{MRI_PRODUCT_SEARCH_URL}?search={quote(substance.strip())}"


def _fallback_row(substance: str) -> list[dict[str, Any]]:
    clean_substance = substance.strip()
    if not clean_substance:
        return []
    return [
        {
            "substance": clean_substance,
            "product": f"{clean_substance} official EU MRI Product Index search",
            "company": "",
            "country": "European Union",
            "region": "EU",
            "status": "Open official EU MRI Product Index - service unavailable or JavaScript search requires browser parsing",
            "strength": extract_strength(clean_substance),
            "dosage_form": extract_dosage_form(clean_substance),
            "pack_size": extract_pack_size(clean_substance),
            "registration_number": "",
            "document_type": "Official registry search handoff",
            "source": "EU MRI Product Index",
            "source_url": _source_url(clean_substance),
            "product_url": _source_url(clean_substance),
            "url": _source_url(clean_substance),
            "connector_mode": "manual_registry",
        }
    ]


def _header_key(value: str) -> str:
    return (
        value.lower()
        .replace("/", " ")
        .replace("-", " ")
        .replace("(", " ")
        .replace(")", " ")
        .replace(".", " ")
        .strip()
    )


def _value(cells: dict[str, str], *names: str) -> str:
    for name in names:
        key = _header_key(name)
        if cells.get(key):
            return cells[key]
    for header, value in cells.items():
        for name in names:
            if _header_key(name) in header and value:
                return value
    return ""


def _country_from_cells(cells: dict[str, str]) -> str:
    country = _value(
        cells,
        "authorisation country",
        "country",
        "cms",
        "rms",
        "member state",
    )
    if country in EU_COUNTRIES:
        return country
    for eu_country in EU_COUNTRIES:
        if eu_country.lower() in country.lower():
            return eu_country
    return country or "European Union"


def _parse_table_rows(html: str, source_url: str, substance: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, Any]] = []
    for table in soup.select("table"):
        headers = [_header_key(th.get_text(" ", strip=True)) for th in table.select("thead th")]
        if not headers:
            first_row = table.find("tr")
            if first_row:
                headers = [_header_key(cell.get_text(" ", strip=True)) for cell in first_row.find_all(["th", "td"])]
        if not headers:
            continue
        for row in table.select("tbody tr") or table.select("tr")[1:]:
            values = [_clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all("td")]
            if len(values) < 2:
                continue
            cells = {
                headers[index]: values[index]
                for index in range(min(len(headers), len(values)))
                if headers[index]
            }
            product = _value(cells, "full name", "product", "product name", "description", "name")
            active = _value(cells, "active substance", "substance")
            if substance.lower() not in f"{product} {active}".lower():
                continue
            country = _country_from_cells(cells)
            link = row.find("a", href=True)
            product_url = requests.compat.urljoin(source_url, link["href"]) if link else source_url
            results.append(
                {
                    "substance": active or substance,
                    "product": product,
                    "company": _value(cells, "mah owner", "mah", "marketing authorisation holder", "holder"),
                    "country": country,
                    "region": "EU",
                    "status": _value(cells, "authorisation status", "status") or "Authorised via MRI/MRP/DCP",
                    "strength": extract_strength(product),
                    "dosage_form": _value(cells, "pharmaceutical form", "authorised dose form")
                    or extract_dosage_form(product),
                    "pack_size": _value(cells, "pack size") or extract_pack_size(product),
                    "registration_number": _value(cells, "ma nr", "ma number", "mrp dcp cp nr", "procedure number"),
                    "atc_code": _value(cells, "atc code"),
                    "source": "EU MRI Product Index",
                    "source_url": source_url,
                    "product_url": product_url,
                    "url": product_url,
                    "document_type": "MRI Product Index",
                }
            )
            if len(results) >= MAX_RESULTS:
                return results
    return results


def run_eu_mri_search(substance: str) -> list[dict[str, Any]]:
    clean_substance = substance.strip()
    if not clean_substance:
        return []
    urls = [
        _source_url(clean_substance),
        f"{MRI_LEGACY_SEARCH_URL}?search={quote(clean_substance)}",
    ]
    for url in urls:
        try:
            response = requests.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if response.status_code >= 500:
                logger.warning("EU MRI Product Index returned status %s for %s", response.status_code, url)
                continue
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("EU MRI Product Index request failed for %s: %s", url, exc)
            continue
        rows = _parse_table_rows(response.text, response.url, clean_substance)
        if rows:
            return rows
    return _fallback_row(clean_substance)
