import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import requests

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength


BASE_URL = "https://health-products.canada.ca/api/drug"
DPD_PRODUCT_URL = "https://health-products.canada.ca/dpd-bdpp/info"
# The DPD product page links the Product Monograph, which is Canada's SmPC. It
# is a PDF on a separate host and is the only document Health Canada publishes
# per product, so it is worth the one extra request per drug code.
PRODUCT_MONOGRAPH_PATTERN = re.compile(
    r"href=\"(https://pdf\.hres\.ca/[^\"]+\.PDF)\"", flags=re.IGNORECASE
)
REQUEST_TIMEOUT = 15
DETAIL_WORKERS = 12
logger = get_logger(__name__)


def _get_field(row, *names):
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return ""


def _fetch(dataset, **params):
    """Call one Drug Product Database dataset with server-side filtering."""
    url = f"{BASE_URL}/{dataset}/"
    response = requests.get(
        url,
        params={"lang": "en", "type": "json", **params},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        return [payload]
    return payload or []


def _fetch_quietly(dataset, drug_code):
    try:
        return _fetch(dataset, id=drug_code)
    except (requests.RequestException, ValueError) as exc:
        logger.info("Health Canada %s lookup failed for %s: %s", dataset, drug_code, exc)
        return []


@lru_cache(maxsize=128)
def _ingredient_rows(substance):
    return _fetch("activeingredient", ingredientname=substance)


def _ingredient_strength(rows):
    strengths = []
    for row in rows:
        value = str(_get_field(row, "strength")).strip()
        unit = str(_get_field(row, "strength_unit", "strengthUnit")).strip()
        text = f"{value} {unit}".strip()
        if text and text not in strengths:
            strengths.append(text)
    return "; ".join(strengths[:3])


def _pack_size(packaging_rows):
    sizes = []
    for row in packaging_rows:
        size = str(_get_field(row, "package_size", "packageSize")).strip()
        unit = str(_get_field(row, "package_size_unit", "packageSizeUnit")).strip()
        package_type = str(_get_field(row, "package_type", "packageType")).strip()
        text = " ".join(part for part in (size, unit, package_type) if part).strip()
        if not text:
            text = str(_get_field(row, "product_information", "productInformation")).strip()
        if text and text not in sizes:
            sizes.append(text)
    return "; ".join(sizes[:3])


def _status_details(status_rows):
    """Latest status plus the original market date used as the registration date."""
    status = ""
    market_date = ""
    for row in status_rows:
        status = str(_get_field(row, "status", "status_name", "statusName")).strip() or status
        market_date = (
            str(_get_field(row, "original_market_date", "originalMarketDate")).strip()
            or market_date
        )
    return status, market_date



def _product_monograph_url(product_url: str) -> str:
    """The Product Monograph PDF the DPD page links, if it has one.

    Not every listing has a monograph -- older and discontinued products often
    do not -- so a miss is normal and must not fail the row.
    """
    try:
        response = requests.get(
            product_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.info("Health Canada monograph lookup failed for %s: %s", product_url, exc)
        return ""
    match = PRODUCT_MONOGRAPH_PATTERN.search(response.text)
    return match.group(1) if match else ""


def _drug_record(substance, drug_code, ingredient_rows):
    products = _fetch_quietly("drugproduct", drug_code)
    if not products:
        return None
    product_row = products[0]
    product = _get_field(product_row, "brand_name", "brandName")
    if not product:
        return None

    forms = _fetch_quietly("form", drug_code)
    packaging = _fetch_quietly("packaging", drug_code)
    statuses = _fetch_quietly("status", drug_code)
    routes = _fetch_quietly("route", drug_code)

    status, market_date = _status_details(statuses)
    dosage_form = ""
    if forms:
        dosage_form = str(
            _get_field(forms[0], "pharmaceutical_form_name", "pharmaceuticalFormName")
        ).strip()
    route = ""
    if routes:
        route = str(
            _get_field(routes[0], "route_of_administration_name", "routeOfAdministrationName")
        ).strip()

    din = str(_get_field(product_row, "drug_identification_number", "drugIdentificationNumber")).strip()
    company = str(_get_field(product_row, "company_name", "companyName")).strip()
    product_url = f"{DPD_PRODUCT_URL}.do?lang=en&code={drug_code}"
    monograph_url = _product_monograph_url(product_url)
    return {
        "substance": substance,
        "product": product,
        "company": company,
        "country": "Canada",
        "region": "CA",
        "status": status,
        "strength": _ingredient_strength(ingredient_rows) or extract_strength(product),
        "dosage_form": dosage_form or extract_dosage_form(product),
        "pack_size": _pack_size(packaging) or extract_pack_size(product),
        "route": route,
        "registration_number": din or str(drug_code),
        "registration_date": market_date,
        "source": "Health Canada",
        "source_url": f"{BASE_URL}/activeingredient/?lang=en&type=json&ingredientname={substance}",
        "product_url": product_url,
        "url": product_url,
        # Canada's Product Monograph is the counterpart of an SmPC; without it
        # a Canadian row has no document of its own and borrows another
        # country's label.
        "smpc_url": monograph_url,
        "document_type": "Health Canada product monograph" if monograph_url else "",
    }


def run_health_canada_search(substance, limit=100):
    query = substance.strip()
    if not query:
        return []
    # A search that could not be asked is not a search that found nothing.
    # Swallowing the timeout here returned an empty list, which the harvest
    # could not tell from a molecule Canada does not register: it recorded
    # the molecule as harvested and never asked again. Seven molecules were
    # stamped done with nothing that way, and emtricitabine alone had 28
    # products waiting. Worse, the harvest counts failures to decide the
    # registry is down, and an error reported as an empty result is invisible
    # to that count -- Canada could fail every molecule and the run would end
    # reporting success.
    #
    # So this raises, and its two callers each do the right thing: the harvest
    # records a failure, leaves the molecule for the next run and stops the
    # run if failures pile up, while a live search logs it and carries on with
    # the other registries. Per-product lookups keep returning empty, because
    # a listing with no monograph really is a listing with no monograph.
    ingredients = _ingredient_rows(query)

    rows_by_code = {}
    for row in ingredients:
        drug_code = str(_get_field(row, "drug_code", "drugCode")).strip()
        if drug_code:
            rows_by_code.setdefault(drug_code, []).append(row)
    if not rows_by_code:
        return []

    # The ingredient lookup already lists every drug code; only the first
    # ``limit`` are looked up, because each costs six more requests. The count
    # travels with the rows so a capped search can say it was capped.
    available_total = len(rows_by_code)
    codes = list(rows_by_code)[:limit]
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as executor:
        records = executor.map(
            lambda code: _drug_record(query, code, rows_by_code[code]), codes
        )
        return [
            {**record, "available_total": available_total}
            for record in records
            if record
        ]
