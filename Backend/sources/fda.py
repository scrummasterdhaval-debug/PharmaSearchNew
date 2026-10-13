from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import re
from urllib.parse import quote

import requests

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_strength


OPENFDA_LABEL_URL = "https://api.fda.gov/drug/label.json"
# openFDA serves the label as JSON; DailyMed serves the same SPL as the
# readable Prescribing Information, keyed by the set id the API already returns.
DAILYMED_LABEL_URL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm"
OPENFDA_NDC_URL = "https://api.fda.gov/drug/ndc.json"
# NDC records carry pack size, marketing start date and the labeler, so it is
# worth waiting for them rather than dropping the enrichment after one second.
NDC_ENRICHMENT_TIMEOUT = 6
logger = get_logger(__name__)


def _first(values):
    if isinstance(values, list) and values:
        return values[0]
    if isinstance(values, str):
        return values
    return ""


def _join_values(values):
    if isinstance(values, list):
        return " ".join(str(value) for value in values if str(value).strip())
    return str(values or "")


def _label_text(item, *fields):
    parts = []
    for field in fields:
        value = _join_values(item.get(field))
        if value:
            parts.append(value)
    return " ".join(parts)


def _fda_strength(item, product):
    # Only the active ingredient section states the strength. The description and
    # SPL data elements also carry excipient and total-weight figures, which the
    # strength pattern happily matches (e.g. "1231.46 g" for an atorvastatin tablet).
    return (
        extract_strength(_label_text(item, "active_ingredient"))
        or extract_strength(product)
        or extract_strength(_label_text(item, "spl_product_data_elements"))
    )


def _fda_dosage_form(openfda, item, product):
    dosage_form = _first(openfda.get("dosage_form"))
    if dosage_form:
        return dosage_form.title()
    text = _label_text(
        item,
        "spl_product_data_elements",
        "dosage_and_administration",
        "description",
    )
    return extract_dosage_form(text) or extract_dosage_form(product)


def _fda_therapeutic_category(item):
    purpose = _first(item.get("purpose"))
    if purpose:
        return " ".join(purpose.split())
    pharmacologic_class = _first(item.get("openfda", {}).get("pharm_class_epc"))
    return pharmacologic_class


def _normalize(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


@lru_cache(maxsize=128)
def _fetch_ndc_records(substance):
    query = f'active_ingredients.name:"{substance.upper()}"'
    try:
        response = requests.get(
            OPENFDA_NDC_URL,
            params={"search": query, "limit": 1000},
            timeout=5,
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return response.json().get("results", [])
    except (requests.RequestException, ValueError) as exc:
        logger.warning("FDA NDC request failed: %s", exc)
        return []


def _fda_date(value):
    """openFDA dates arrive as YYYYMMDD."""
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return ""
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"


def _pack_size_from_ndc(record):
    packages = record.get("packaging") or []
    descriptions = [
        package.get("description", "")
        for package in packages
        if package.get("description")
    ]
    return "; ".join(descriptions[:3])


def _strength_from_ndc(record):
    ingredients = record.get("active_ingredients") or []
    strengths = []
    for ingredient in ingredients:
        # openFDA writes unit-dose strengths as "500 mg/1"; the denominator is noise.
        strength = re.sub(r"/1\b", "", str(ingredient.get("strength") or "")).strip()
        if strength:
            strengths.append(strength)
    return "; ".join(strengths)


def _best_ndc_record(product, company, ndc_records):
    product_key = _normalize(product)
    company_key = _normalize(company)
    best_record = None
    best_score = 0
    for record in ndc_records:
        names = [
            record.get("brand_name", ""),
            record.get("brand_name_base", ""),
            record.get("generic_name", ""),
        ]
        name_keys = [_normalize(name) for name in names if name]
        if not name_keys:
            continue
        score = 0
        if product_key in name_keys:
            score += 100
        elif any(product_key and (product_key in key or key in product_key) for key in name_keys):
            score += 70
        labeler_key = _normalize(record.get("labeler_name", ""))
        if company_key and labeler_key and (company_key in labeler_key or labeler_key in company_key):
            score += 25
        if score > best_score:
            best_record = record
            best_score = score
    return best_record


def run_fda_search(substance, limit=100):
    encoded = quote(f'openfda.substance_name:"{substance}"')
    url = f"{OPENFDA_LABEL_URL}?search={encoded}&limit={limit}"
    ndc_executor = ThreadPoolExecutor(max_workers=1)
    ndc_future = ndc_executor.submit(_fetch_ndc_records, substance)
    try:
        response = requests.get(url, timeout=8)
        if response.status_code == 404:
            ndc_executor.shutdown(wait=False, cancel_futures=True)
            return []
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        ndc_executor.shutdown(wait=False, cancel_futures=True)
        logger.warning("FDA request failed: %s", exc)
        return []
    except ValueError as exc:
        ndc_executor.shutdown(wait=False, cancel_futures=True)
        logger.warning("FDA returned invalid JSON: %s", exc)
        return []

    try:
        ndc_records = ndc_future.result(timeout=NDC_ENRICHMENT_TIMEOUT)
    except FutureTimeoutError:
        logger.info("FDA NDC packaging enrichment deferred for %s", substance)
        ndc_records = []
    finally:
        ndc_executor.shutdown(wait=False, cancel_futures=True)
    # openFDA says how many labels match, in the same response. This search
    # asks for the first ``limit`` of them, so the total is what tells a reader
    # that acetaminophen's 100 rows are 100 of 3,276 rather than all there are.
    available_total = int(((data.get("meta") or {}).get("results") or {}).get("total") or 0)
    results = []
    for item in data.get("results", []):
        openfda = item.get("openfda", {})
        product = _first(openfda.get("brand_name")) or _first(openfda.get("generic_name"))
        application_number = _first(openfda.get("application_number"))
        route = _first(openfda.get("route"))
        if not product:
            continue
        company = _first(openfda.get("manufacturer_name"))
        ndc_record = _best_ndc_record(product, company, ndc_records) or {}
        labeler = str(ndc_record.get("labeler_name", "")).strip()
        holder = company or labeler
        set_id = str(item.get("set_id") or _first(openfda.get("spl_set_id")) or "").strip()
        dailymed_url = f"{DAILYMED_LABEL_URL}?setid={set_id}" if set_id else ""
        product_query_url = url
        if application_number:
            product_query = quote(f'openfda.application_number:"{application_number}"')
            product_query_url = f"{OPENFDA_LABEL_URL}?search={product_query}&limit=10"
        results.append(
            {
                "substance": substance,
                # The label names the exact salt -- "TRIAMCINOLONE ACETONIDE"
                # where the search term was just "triamcinolone". A brand like
                # KENALOG-40 states it nowhere else, and without it a document
                # can only be matched to the molecule, not to the salt.
                "source_substance": _first(openfda.get("substance_name")) or substance,
                "product": product,
                "company": holder,
                # openFDA's ``manufacturer_name`` and the NDC ``labeler_name``
                # identify the SPL/NDC labeler. Neither establishes a physical
                # finished-product manufacturing site.
                "commercial_company": labeler,
                "labeler_name": holder,
                "applicant_sponsor": holder,
                "registration_date": _fda_date(ndc_record.get("marketing_start_date")),
                "country": "United States",
                "region": "US",
                "status": "Label available",
                "strength": _strength_from_ndc(ndc_record) or _fda_strength(item, product),
                "dosage_form": _fda_dosage_form(openfda, item, product)
                or str(ndc_record.get("dosage_form", "")).title(),
                "pack_size": _pack_size_from_ndc(ndc_record),
                "expiry_date": ndc_record.get("listing_expiration_date", ""),
                "route": route,
                "therapeutic_category": _fda_therapeutic_category(item),
                "registration_number": application_number,
                "source": "FDA",
                "source_url": url,
                "product_url": dailymed_url or product_query_url,
                "url": dailymed_url or product_query_url,
                # The FDA's Prescribing Information is the US counterpart of an
                # SmPC, and DailyMed is where it is published for people rather
                # than for the API. Without it a US row has no document of its
                # own and ends up borrowing another country's label.
                "smpc_url": dailymed_url,
                "document_type": "FDA prescribing information" if dailymed_url else "",
                "available_total": available_total,
            }
        )
    return results
