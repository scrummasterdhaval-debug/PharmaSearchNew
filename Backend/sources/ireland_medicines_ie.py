import os
from functools import lru_cache

import requests

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength


API_BASE_URL = "https://backend-prod.medicines.ie/api/v1"
TOKEN_URL = "https://backend-prod.medicines.ie/oauth/v2/token"
FILE_BASE_URL = "https://backend-prod.medicines.ie/uploads/files"
logger = get_logger(__name__)


@lru_cache(maxsize=1)
def _access_token():
    # OAuth client credentials for the medicines.ie API, read from the
    # environment so they are not kept in the repository. Unset, Ireland
    # searches are skipped.
    client_id = os.environ.get("MEDICINES_IE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("MEDICINES_IE_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        raise ValueError("MEDICINES_IE_CLIENT_ID / MEDICINES_IE_CLIENT_SECRET are not set")
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _headers():
    return {
        "Authorization": f"Bearer {_access_token()}",
        "Origin": "https://www.medicines.ie",
        "Referer": "https://www.medicines.ie/",
        "User-Agent": "Mozilla/5.0",
    }


def _api_get(path, params=None):
    response = requests.get(
        f"{API_BASE_URL}{path}",
        params=params,
        timeout=30,
        headers=_headers(),
    )
    response.raise_for_status()
    return response.json()


@lru_cache(maxsize=2048)
def _company_name(company_id):
    if not company_id:
        return ""
    try:
        return _api_get(f"/companies/{company_id}").get("name", "")
    except requests.RequestException:
        return ""


@lru_cache(maxsize=4096)
def _license(license_id):
    if not license_id:
        return {}
    try:
        return _api_get(f"/licenses/{license_id}")
    except requests.RequestException:
        return {}


@lru_cache(maxsize=4096)
def _ingredient_name(ingredient_id):
    if not ingredient_id:
        return ""
    try:
        return _api_get(f"/ingredients/{ingredient_id}").get("name", "")
    except requests.RequestException:
        return ""


def _file_url(file_data):
    if not file_data:
        return ""
    name = file_data.get("name", "")
    if not name:
        return ""
    return f"{FILE_BASE_URL}/{name}"


def _frontend_url(medicine_id, product, license_data):
    if license_data.get("frontendUrl"):
        return license_data["frontendUrl"]
    slug = "-".join(product.lower().split())
    return f"https://www.medicines.ie/medicines/{slug}-{medicine_id}/spc"


def _extract_record(entity, detail, substance):
    license_ids = detail.get("licenseNumbers") or entity.get("licenseNumbers") or []
    license_data = _license(license_ids[0]) if license_ids else {}
    ingredient_ids = detail.get("ingredients") or entity.get("ingredients") or []
    ingredient_names = [_ingredient_name(item) for item in ingredient_ids]
    ingredient_names = [item for item in ingredient_names if item]
    product = detail.get("name") or entity.get("name") or ""
    active_spc = detail.get("activeSPCData") or {}
    smpc_url = _file_url(active_spc.get("file") or {})
    product_url = _frontend_url(detail.get("id") or entity.get("id"), product, license_data)
    return {
        "substance": ", ".join(ingredient_names) or substance,
        "product": product,
        "company": _company_name(detail.get("company") or entity.get("company")),
        "country": "Ireland",
        "region": "EU",
        "status": detail.get("status", "") or entity.get("status", ""),
        "strength": extract_strength(product),
        "dosage_form": extract_dosage_form(product),
        "pack_size": extract_pack_size(product),
        "registration_number": license_data.get("fullLicenseNumber", ""),
        "registration_date": detail.get("publishedAt", "") or entity.get("publishedAt", ""),
        "source": "Ireland medicines.ie",
        "source_url": "https://www.medicines.ie/",
        "product_url": product_url,
        "url": product_url,
        "smpc_url": smpc_url,
        "last_checked": detail.get("updatedAt", "") or entity.get("updatedAt", ""),
    }


def run_ireland_medicines_search(substance, limit=200):
    try:
        payload = _api_get("/medicines", params={"query": substance})
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("Ireland medicines.ie request failed: %s", exc)
        return []

    results = []
    for entity in payload.get("entities", []):
        medicine_id = entity.get("id")
        if not medicine_id:
            continue
        try:
            detail = _api_get(f"/medicines/{medicine_id}")
        except requests.RequestException:
            detail = entity
        results.append(_extract_record(entity, detail, substance))
        if len(results) >= limit:
            break
    return results
