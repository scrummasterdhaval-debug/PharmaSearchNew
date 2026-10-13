"""CDSCO India approvals (SUGAM).

The SUGAM "Approved Drugs/Vaccines/r-DNA/Blood Product" page is backed by an
undocumented JSON endpoint that filters server side, so one request per search
returns the matching approvals directly. It is the only CDSCO surface that
carries the manufacturer alongside the molecule -- the FDC approval PDFs this
connector used to scrape name the drug but never the company, and the separate
new-drugs table has the same gap.

Each record also distinguishes a bulk drug (API) approval from a finished
formulation, which is the difference between a company making a substance and
merely packaging it, and lists the actual production sites separately from the
applicant's registered office.

The endpoint answers in ~21 seconds whatever is asked of it -- the cost is the
server-side scan, not the payload -- which does not fit the live search budget.
Since one unfiltered request returns the whole corpus, we fetch that once, keep
it on disk, and filter it in process.
"""

import json
import time
from pathlib import Path
from typing import Any

import requests

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength


BASE_URL = "https://cdscoonline.gov.in/CDSCO"
SEARCH_URL = f"{BASE_URL}/loadDrugApprovals"
PUBLIC_URL = f"{BASE_URL}/cdscoDrugs"
REQUEST_TIMEOUT = 60
MAX_RESULTS = 150

# The drug-type filter the page sends for "All"; the narrower codes drop either
# bulk drugs or finished formulations, and we want both.
DRUG_TYPE_ALL = "13"

CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "cdsco_india_approvals.json"
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

logger = get_logger(__name__)

_MEMORY_CACHE: dict[str, Any] = {}

SUPPLY_TYPE_STATUS = {
    "Bulk Drug": "Approved (bulk drug / API)",
    "Finished Formulation": "Approved (finished formulation)",
    "Both BD & FF": "Approved (bulk drug and finished formulation)",
}


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _manufacturing_sites(value: object) -> list[str]:
    """``manuf_addr`` holds one or more production sites joined by <br>, and is
    "NA" on the older records that predate site-level reporting."""
    text = str(value or "")
    for separator in ("<br/>", "<br />", "<BR>", "<br>"):
        text = text.replace(separator, "\n")
    sites = (_clean(site) for site in text.split("\n"))
    return [site for site in sites if site and site.upper() != "NA"]


def _matches(record: dict[str, Any], substance: str) -> bool:
    """The endpoint also matches on indication text, which pulls in products
    that merely mention the substance, so confirm it names the molecule."""
    needle = substance.strip().lower()
    if not needle:
        return False
    haystack = " ".join(
        _clean(record.get(field)).lower()
        for field in ("str_drug_name", "str_composition", "str_pct_name")
    )
    return needle in haystack


def _record(raw: dict[str, Any], substance: str) -> dict[str, Any] | None:
    product = _clean(raw.get("str_drug_name"))
    if not product:
        return None

    company = _clean(raw.get("str_man_unit_name"))
    sites = _manufacturing_sites(raw.get("manuf_addr"))
    composition = _clean(raw.get("str_composition"))
    supply_type = _clean(raw.get("str_applied_for"))
    form_id = _clean(raw.get("num_form_id"))
    dosage_form = _clean(raw.get("str_dosage"))
    indication = _clean(raw.get("str_indication"))
    if supply_type == "Bulk Drug":
        manufacturer_role = "API_MANUFACTURER"
        manufacturer_scope = "API"
    elif supply_type == "Finished Formulation":
        manufacturer_role = "FINISHED_PRODUCT_MANUFACTURER"
        manufacturer_scope = "FINISHED_PRODUCT"
    else:
        manufacturer_role = "MANUFACTURER"
        manufacturer_scope = "API_AND_FINISHED_PRODUCT"

    manufacturers = [
        {
            "name": company,
            "address": site,
            "country": "India",
            "role": manufacturer_role,
            "scope": manufacturer_scope,
            "verification_status": "VERIFIED_REGULATOR_RECORD",
        }
        for site in sites
    ]
    evidence = [
        {
            "field_name": "manufacturer_address",
            "value": site,
            "role": manufacturer_role,
            "source_regulator": "CDSCO India",
            "document_type": "CDSCO SUGAM approval record",
            "evidence_url": PUBLIC_URL,
            "evidence_section": "Manufacturing site",
            "extraction_method": "OFFICIAL_API_FIELD",
            "verification_status": "VERIFIED_REGULATOR_RECORD",
        }
        for site in sites
    ]

    return {
        "substance": substance,
        "product": product,
        "company": company,
        # The applicant is the licence holder; the sites are where it is made,
        # and they are frequently different companies on loan licence.
        # Do not turn the applicant into a manufacturer when CDSCO did not
        # publish a manufacturing site. The normalized list preserves every
        # site instead of retaining only the first address.
        "manufacturer_name": company if sites else "",
        "manufacturer_country": "India" if sites else "",
        "manufacturer_source": "CDSCO SUGAM manufacturing-site record" if sites else "",
        "manufacturers": manufacturers,
        "evidence": evidence,
        "applicant_sponsor": company,
        "country": "India",
        "region": "AS",
        "status": SUPPLY_TYPE_STATUS.get(supply_type, "Approved by CDSCO"),
        "strength": extract_strength(composition) or extract_strength(product),
        "dosage_form": dosage_form if dosage_form.upper() not in {"", "NA"} else extract_dosage_form(product),
        "pack_size": extract_pack_size(product),
        "therapeutic_category": indication if indication.upper() not in {"", "NA"} else "",
        "registration_number": form_id,
        "registration_date": _clean(raw.get("dt_closure_dt")),
        "document_type": "CDSCO approval record",
        "source": "CDSCO India",
        "source_url": PUBLIC_URL,
        "product_url": PUBLIC_URL,
        "url": PUBLIC_URL,
    }


def _fetch_corpus() -> list[dict[str, Any]]:
    response = requests.get(
        SEARCH_URL,
        params={"searchText": "", "year": "", "month": "", "drugTypeValue": DRUG_TYPE_ALL},
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get("aaData") or []


def _read_cache() -> list[dict[str, Any]] | None:
    if _MEMORY_CACHE.get("records") is not None:
        return _MEMORY_CACHE["records"]
    if not CACHE_PATH.exists():
        return None
    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("CDSCO India cache unreadable: %s", exc)
        return None
    if time.time() - cached.get("fetched_at", 0) > CACHE_TTL_SECONDS:
        return None
    _MEMORY_CACHE["records"] = cached.get("records") or []
    return _MEMORY_CACHE["records"]


def _write_cache(records: list[dict[str, Any]]) -> None:
    _MEMORY_CACHE["records"] = records
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"fetched_at": time.time(), "records": records}),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not persist CDSCO India cache: %s", exc)


def refresh_cache() -> int:
    """Re-download the corpus regardless of cache age. Returns the record count."""
    records = _fetch_corpus()
    _write_cache(records)
    logger.info("Refreshed CDSCO India cache with %s records", len(records))
    return len(records)


def _corpus() -> list[dict[str, Any]]:
    cached = _read_cache()
    if cached is not None:
        return cached
    try:
        records = _fetch_corpus()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("CDSCO India unavailable: %s", exc)
        return _MEMORY_CACHE.get("records") or []
    _write_cache(records)
    return records


def run_cdsco_india_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    clean_substance = substance.strip()
    if not clean_substance:
        return []

    payload = {"aaData": _corpus()}

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in payload.get("aaData") or []:
        if not _matches(raw, clean_substance):
            continue
        record = _record(raw, clean_substance)
        if not record:
            continue
        key = (
            record["product"].lower(),
            record["company"].lower(),
            record["registration_number"],
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(record)
        if len(rows) >= limit:
            break
    return rows
