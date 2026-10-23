"""HMA MRI Product Index: medicines authorised through the mutual-recognition
and decentralised procedures (MRP/DCP), with every member state concerned.

The portal (mri.cts-mrp.eu/portal) is an Angular app over an OData API at
mri-production.cts-mrp.eu. The API answers only the portal's own browser
session (scripted clients are disconnected), after a two-step handshake: GET
/Authentication returns a token, POSTing it back with the time returns a JWT,
and every query sends that JWT in a ``YouDontOwnMeV1`` header. So the search
loads the portal in headless Chromium and runs the same calls from inside it.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from core.logging_config import get_logger
from sources.ema import EU_COUNTRIES
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength
from sources.salts import base_name


PORTAL_URL = "https://mri.cts-mrp.eu/portal/"
API_URL = "https://mri-production.cts-mrp.eu/v1/odata"
MRI_PRODUCT_SEARCH_URL = f"{PORTAL_URL}product-search"
EXPAND = "activeSubstances,atcCodes,cms,doseForms,rms,typeLevelCategory,withdrawalReasons"
PAGE_TIMEOUT_MS = 30_000
FETCH_TIMEOUT_MS = 25_000
MAX_RESULTS = 200
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
)
logger = get_logger(__name__)

# Runs inside the portal page: the handshake, then one product query.
_FETCH_SCRIPT = """
async ([api, filter, expand, top, timeoutMs]) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const token = await (await fetch(api + '/Authentication', {signal: controller.signal})).json();
    const jwt = await (await fetch(api + '/Authentication', {
      method: 'POST', signal: controller.signal, headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({authenticationToken: token.authenticationToken, authenticationTime: new Date().toISOString()}),
    })).json();
    const query = new URLSearchParams({'$filter': filter, '$expand': expand, '$count': 'true', '$top': String(top), '$orderby': 'productKey'});
    const response = await fetch(api + '/ProductSearch?' + query, {signal: controller.signal, headers: {YouDontOwnMeV1: jwt.jwt}});
    return {status: response.status, body: await response.json()};
  } finally {
    clearTimeout(timer);
  }
}
"""


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _source_url(substance: str) -> str:
    return f"{MRI_PRODUCT_SEARCH_URL}?searchTerm={quote(substance.strip())}"


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
            "status": "Open official EU MRI Product Index - the index did not answer",
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


def _odata_text(value: str) -> str:
    return value.replace("'", "''")


def search_filter(substance: str) -> str:
    """Human medicines whose active substances include the molecule.

    The index files salts under the molecule ("Atorvastatin calcium"), so the
    query asks for the first molecule word; a combination keeps only products
    naming every molecule (checked in ``rows_from_products``).
    """
    words = re.findall(r"[a-z0-9]+", base_name(substance).lower())
    first = words[0] if words else substance.strip().lower()
    return f"(domainKey eq 'h') and activeSubstances/any(a: contains(tolower(a/innName), '{_odata_text(first)}'))"


def _amount(value: Any) -> str:
    if value is None or value == "":
        return ""
    return f"{float(value):g}"


def _strength(actives: list[dict[str, Any]]) -> str:
    parts = []
    for active in actives:
        amount = _amount(active.get("amount"))
        unit = _clean_text(active.get("unitDescription"))
        if amount:
            parts.append(f"{amount} {unit}".strip())
    return "/".join(parts)


def _active_names(actives: list[dict[str, Any]]) -> str:
    names = []
    for active in actives:
        name = _clean_text(active.get("innName"))
        salt = _clean_text(active.get("saltName"))
        if name:
            names.append(f"{name} {salt}".strip() if salt and salt.lower() not in name.lower() else name)
    return " + ".join(names)


def _country(entry: dict[str, Any] | None) -> str:
    if not entry:
        return ""
    name = _clean_text(entry.get("countryName"))
    for eu_country in EU_COUNTRIES:
        if eu_country.lower() == name.lower():
            return eu_country
    return name


def _date(value: Any) -> str:
    return str(value or "")[:10]


def _status(product: dict[str, Any]) -> str:
    if product.get("withdrawalReasons"):
        return "Withdrawn (MRP/DCP)"
    outcome = _clean_text(product.get("outcome"))
    if outcome.lower() == "positive":
        return "Authorised via MRP/DCP"
    return f"MRP/DCP outcome: {outcome}" if outcome else "Listed in MRI Product Index"


def rows_from_products(products: list[dict[str, Any]], substance: str) -> list[dict[str, Any]]:
    """One row per product: the reference member state as its country, the
    concerned member states alongside."""
    molecule = re.findall(r"[a-z0-9]+", base_name(substance).lower())
    rows = []
    for product in products:
        actives = product.get("activeSubstances") or []
        active_names = _active_names(actives)
        if not all(word in active_names.lower() for word in molecule):
            continue
        rms = product.get("rms") or {}
        concerned = [_country(entry) for entry in product.get("cms") or []]
        holder_lines = [line.strip() for line in str(product.get("maHolder") or "").splitlines() if line.strip()]
        category = product.get("typeLevelCategory") or {}
        name = _clean_text(product.get("name")) or _clean_text(rms.get("productName"))
        procedure = _clean_text(product.get("productKey"))
        rows.append(
            {
                "substance": active_names or substance,
                "active_substance": active_names,
                "source_substance": active_names,
                "product": name,
                "company": holder_lines[0] if holder_lines else "",
                "company_address": ", ".join(holder_lines[1:]),
                "country": _country(rms) or "European Union",
                "region": "EU",
                "status": _status(product),
                "strength": _strength(actives) or extract_strength(name),
                "dosage_form": "; ".join(
                    _clean_text(form.get("doseFormName")) for form in product.get("doseForms") or [] if form.get("doseFormName")
                ) or extract_dosage_form(name),
                "pack_size": extract_pack_size(name),
                "registration_number": procedure,
                "registration_date": _date(product.get("dateOfOutcome")),
                "atc_code": ", ".join(
                    _clean_text(code.get("code")) for code in product.get("atcCodes") or [] if code.get("code")
                ),
                "classification": _clean_text(category.get("typeLevelCategory5")),
                "procedure_type": " / ".join(
                    _clean_text(category.get(key))
                    for key in ("typeLevelCategory2", "typeLevelCategory3")
                    if category.get(key)
                ),
                "reference_member_state": _country(rms),
                "concerned_member_states": ", ".join(country for country in concerned if country),
                "source": "EU MRI Product Index",
                "source_url": _source_url(substance),
                "product_url": _source_url(procedure or substance),
                "url": _source_url(procedure or substance),
                "document_type": "MRI Product Index",
            }
        )
    return rows


def _query_index(substance: str, limit: int) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=BROWSER_USER_AGENT)
            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            return page.evaluate(_FETCH_SCRIPT, [API_URL, search_filter(substance), EXPAND, limit, FETCH_TIMEOUT_MS])
        finally:
            browser.close()


def run_eu_mri_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    clean_substance = substance.strip()
    if not clean_substance:
        return []
    try:
        answer = _query_index(clean_substance, limit)
    except Exception as exc:
        logger.warning("EU MRI Product Index unavailable for %s: %s", clean_substance, exc)
        return _fallback_row(clean_substance)
    if answer.get("status") != 200 or not isinstance(answer.get("body"), dict):
        logger.warning("EU MRI Product Index answered %s for %s", answer.get("status"), clean_substance)
        return _fallback_row(clean_substance)
    return rows_from_products(answer["body"].get("value") or [], clean_substance)[:limit]
