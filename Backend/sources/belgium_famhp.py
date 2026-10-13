import base64
import hashlib
import hmac
import json
import random
import time
from typing import Any

import requests

from core.logging_config import get_logger
from sources.parser import extract_pack_size, extract_route, extract_strength


BASE_URL = "https://medicinesdatabase.be"
CONFIG_URL = f"{BASE_URL}/api/config"
PRODUCTS_URL = f"{BASE_URL}/api/products"
USER_AGENT = "Mozilla/5.0"
REQUEST_TIMEOUT = 30
PAGE_SIZE = 20
logger = get_logger(__name__)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _json_b64url(value: dict[str, Any]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode())


def _xsrf_token(secret_key: str) -> str:
    expiry = int(time.time() + 300)
    jti = str(int(time.time() * 1000) * random.randint(1, 9999))
    header = _json_b64url({"alg": "HS256", "typ": "JWT"})
    payload = _json_b64url({"a": USER_AGENT, "exp": expiry, "jti": jti})
    signature = _b64url(
        hmac.new(
            secret_key.encode(),
            f"{header}.{payload}".encode(),
            hashlib.sha256,
        ).digest()
    )
    return f"{signature}.{jti}{expiry}"


def _apply_token(session: requests.Session) -> None:
    """Mint a fresh xsrf token onto the session.

    A token is accepted for only about two calls before the registry starts
    answering 401, so a paged search has to re-mint rather than sign once and
    reuse. The signing key comes from the site's own config endpoint.
    """
    config = session.get(CONFIG_URL, timeout=REQUEST_TIMEOUT).json()
    session.headers.update({"xsrf-token": _xsrf_token(config["key"])})


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en",
        }
    )
    _apply_token(session)
    return session


def _first(values: Any) -> str:
    if isinstance(values, list) and values:
        return str(values[0] or "").strip()
    return str(values or "").strip()


def _document_urls(documents: list[dict[str, Any]]) -> dict[str, str]:
    urls = {"smpc_url": "", "pil_url": ""}
    for document in documents or []:
        doc_type = str(document.get("type") or "").lower()
        url = document.get("url") or ""
        if not url:
            continue
        if doc_type in {"skp", "spc"} and not urls["smpc_url"]:
            urls["smpc_url"] = url
        elif doc_type in {"bijsluiter", "leaflet", "pil"} and not urls["pil_url"]:
            urls["pil_url"] = url
    return urls


def _status(row: dict[str, Any]) -> str:
    availability = row.get("availability") or []
    if isinstance(availability, list) and availability:
        return ", ".join(str(item) for item in availability)
    return ""


def _extract_record(row: dict[str, Any], substance: str) -> dict[str, Any]:
    product = row.get("name", "")
    documents = _document_urls(row.get("documents") or [])
    product_url = f"{BASE_URL}/human/{row.get('id', '')}"
    dosage_form = _first(row.get("pharmaceuticalForm"))
    route = _first(row.get("routeOfAdministration"))
    return {
        "substance": ", ".join(row.get("activeSubstanceShort") or []) or substance,
        "product": product,
        "company": row.get("company", ""),
        "country": "Belgium",
        "region": "EU",
        "status": _status(row),
        "strength": extract_strength(product),
        "dosage_form": dosage_form,
        "route": route or extract_route(product),
        "pack_size": extract_pack_size(product),
        "registration_number": row.get("authorisationNumber", "") or row.get("id", ""),
        "source": "Belgium FAMHP",
        "source_url": f"{PRODUCTS_URL}?activeSubstance={substance}&usage=human",
        "product_url": product_url,
        "url": product_url,
        "smpc_url": documents["smpc_url"],
        "pil_url": documents["pil_url"],
        "last_checked": row.get("editTS", ""),
    }


def run_belgium_famhp_search(substance: str, limit: int = 200) -> list[dict[str, Any]]:
    try:
        session = _session()
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("Belgium FAMHP request failed: %s", exc)
        return []

    rows = []
    start_row = 0
    total_rows = None
    while len(rows) < limit:
        params = {
            "startRow": start_row,
            "RPP": PAGE_SIZE,
            "activeSubstance": substance,
            "usage": "human",
        }
        try:
            response = session.get(PRODUCTS_URL, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 401:
                # The token this session signed has been spent. Sign another
                # and ask again: a 401 here means the page is unsigned, not
                # that the search is over.
                _apply_token(session)
                response = session.get(PRODUCTS_URL, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, KeyError) as exc:
            logger.warning("Belgium FAMHP request failed: %s", exc)
            break

        total_rows = total_rows if total_rows is not None else int(payload.get("rows") or 0)
        page_rows = payload.get("data", [])
        if not page_rows:
            break
        for row in page_rows:
            if row.get("name"):
                rows.append(_extract_record(row, substance))
            if len(rows) >= limit:
                break
        start_row += PAGE_SIZE
        if start_row >= total_rows:
            break
    return rows
