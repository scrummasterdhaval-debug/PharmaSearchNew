"""Lebanon: the Ministry of Public Health's national drugs database.

    https://www.moph.gov.lb/en/Drugs/index/3/4848/lebanon-national-drugs-database

The search form takes an ingredient and returns every matching registration on
one page -- ATC code, brand name, brand or generic, ingredients with strengths,
form and price. The rest of the record is on each drug's page: registration
number, pack, route, local agent, manufacturing laboratory and its country, and
the responsible party (the marketing authorisation holder).

A drug's page also lists every other registration with exactly the same
ingredients and strengths, with the same columns. So one page is fetched per
ingredient group rather than one per drug, and a drug not covered that way has
its own page read.
"""

from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from typing import Any

import requests

from core.logging_config import get_logger


logger = get_logger(__name__)

BASE_URL = "https://www.moph.gov.lb"
LANDING_URL = f"{BASE_URL}/en/Drugs/index/3/4848/lebanon-national-drugs-database"
SEARCH_URL = f"{BASE_URL}/en/Drugs/index/3/4848"
REQUEST_TIMEOUT = 40
DETAIL_WORKERS = 6
DETAIL_DEADLINE_SECONDS = 25
MAX_RESULTS = 500

ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL = re.compile(r"<t([dh])[^>]*>(.*?)</t\1>", re.S)
TABLE = re.compile(r"<table.*?</table>", re.S)
VIEW_LINK = re.compile(r'href="(/en/Drugs/view/\d+)"')


def _text(fragment: str) -> str:
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _table_rows(table_html: str) -> list[dict[str, str]]:
    """A table as dicts keyed by its header cells."""
    table_html = re.sub(r"<!--.*?-->", "", table_html, flags=re.S)
    rows = ROW.findall(table_html)
    if not rows:
        return []
    headers = [_text(cell) for _kind, cell in CELL.findall(rows[0])]
    records = []
    for row in rows[1:]:
        cells = [cell for _kind, cell in CELL.findall(row)]
        if len(cells) != len(headers):
            continue
        record = {header: _text(cell) for header, cell in zip(headers, cells)}
        link = VIEW_LINK.search(row)
        record["_view"] = link.group(1) if link else ""
        records.append(record)
    return records


def _session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    session.get(LANDING_URL, timeout=REQUEST_TIMEOUT)
    return session


def _search(session: requests.Session, ingredient: str) -> list[dict[str, str]]:
    data = {
        "_method": "POST",
        "data[Drug][ingredient]": ingredient,
        "data[Drug][name]": "",
        "data[Drug][agent]": "",
        "data[Drug][country]": "",
        "data[Drug][form]": "",
        "data[Drug][manufacturer]": "",
    }
    response = session.post(SEARCH_URL, data=data, timeout=REQUEST_TIMEOUT, headers={"Referer": LANDING_URL})
    response.raise_for_status()
    tables = TABLE.findall(response.text)
    return _table_rows(tables[0]) if tables else []


def _details(session: requests.Session, view_path: str) -> list[dict[str, str]]:
    response = session.get(BASE_URL + view_path, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    records = []
    for table in TABLE.findall(response.text):
        for record in _table_rows(table):
            record["_view"] = record["_view"] or view_path
            records.append(record)
    return records


PRICE = re.compile(r"[\d.,]+\s*L\.L")


def _price(record: dict[str, str]) -> str:
    match = PRICE.search(record.get("Price", ""))
    return match.group(0) if match else ""


def _key(record: dict[str, str]) -> tuple[str, str, str, str]:
    """A registration as both tables show it; the price separates packs of one drug."""
    return (
        record.get("Name", "").strip().upper(),
        " ".join(record.get("Ingredients", "").split()).upper(),
        record.get("Form", "").strip().upper(),
        _price(record),
    )


def _strength(ingredients: str) -> str:
    """ "Rosuvastatin (calcium) - 10mg, Ezetimibe - 10mg" -> "10mg/10mg"."""
    strengths = [part.split(" - ", 1)[1].strip() for part in ingredients.split(", ") if " - " in part]
    return "/".join(strengths)


def _molecules(ingredients: str) -> str:
    """The ingredient names without strengths: "Rosuvastatin (calcium); Ezetimibe"."""
    return "; ".join(part.split(" - ", 1)[0].strip() for part in ingredients.split(", ") if part.strip())


def _record(listing: dict[str, str], detail: dict[str, str] | None, substance: str) -> dict[str, Any]:
    detail = detail or {}
    name = listing.get("Name", "").strip()
    ingredients = listing.get("Ingredients", "")
    strength = listing.get("Dosage") or _strength(ingredients)
    view_path = detail.get("_view") or listing.get("_view", "")
    product_url = BASE_URL + view_path if view_path else LANDING_URL
    brand_or_generic = {"B": "Brand", "G": "Generic"}.get(listing.get("B/G", "").strip().upper(), "")
    manufacturer = detail.get("Laboratory", "")
    return {
        # The register's own ingredients: the search engine keeps this as the
        # row's stated substance and matches combinations against it.
        "substance": _molecules(ingredients) or substance,
        "source_substance": _molecules(ingredients),
        "product": f"{name} {strength}".strip(),
        "company": detail.get("Responsible Party Name", ""),
        "ma_holder_country": detail.get("Responsible Party Country", ""),
        "manufacturer_name": manufacturer,
        "manufacturer_country": detail.get("Country", ""),
        "manufacturer_source": "MoPH Lebanon laboratory field" if manufacturer else "",
        "local_agent": detail.get("Agent", ""),
        "country": "Lebanon",
        "region": "ME",
        "status": "Registered",
        "authorisation_scope": brand_or_generic,
        "strength": strength,
        "dosage_form": listing.get("Form", ""),
        "route": detail.get("Route", ""),
        "pack_size": detail.get("Presentation", ""),
        "atc_code": listing.get("ATC", ""),
        "registration_number": detail.get("Registration Nb", ""),
        "price": _price(listing) or _price(detail),
        "source": "MoPH Lebanon",
        "source_url": LANDING_URL,
        "product_url": product_url,
        "url": product_url,
        "document_type": "MoPH Lebanon drug record",
    }


def run_lebanon_moph_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    term = " ".join(str(substance or "").split())
    if not term:
        return []
    try:
        session = _session()
        listings = _search(session, term)[:limit]
    except requests.RequestException as exc:
        logger.warning("MoPH Lebanon search unavailable: %s", exc)
        return []
    if not listings:
        return []

    # Several registrations can share a name, form and price (two ATOZET
    # 10mg/10mg entries); each keeps its own code and registration number, and
    # listings are paired with them in order.
    details: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
    seen_registrations: set[tuple[str, str]] = set()

    def remember(records: list[dict[str, str]]) -> None:
        for record in records:
            identity = (record.get("code", ""), record.get("Registration Nb", ""))
            if identity in seen_registrations:
                continue
            seen_registrations.add(identity)
            details.setdefault(_key(record), []).append(record)

    def fetch_all(paths: list[str]) -> None:
        if not paths:
            return
        executor = ThreadPoolExecutor(max_workers=min(DETAIL_WORKERS, len(paths)))
        futures = {executor.submit(_details, session, path): path for path in paths}
        try:
            for future in as_completed(futures, timeout=DETAIL_DEADLINE_SECONDS):
                try:
                    remember(future.result())
                except requests.RequestException as exc:
                    logger.info("MoPH Lebanon drug page %s unavailable: %s", futures[future], exc)
        except FuturesTimeout:
            logger.info("MoPH Lebanon: drug pages not all read before the deadline")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    # One page per ingredient group lists every registration in that group.
    first_of_group: dict[str, str] = {}
    for listing in listings:
        group = " ".join(listing.get("Ingredients", "").split()).upper()
        if listing.get("_view") and group not in first_of_group:
            first_of_group[group] = listing["_view"]
    fetch_all(list(first_of_group.values()))
    missing = [
        listing["_view"] for listing in listings
        if listing.get("_view")
        and _key(listing) not in details
    ]
    fetch_all(list(dict.fromkeys(missing)))

    taken: dict[tuple[str, str, str, str], int] = {}
    records = []
    for listing in listings:
        key = _key(listing)
        candidates = details.get(key, [])
        index = taken.get(key, 0)
        taken[key] = index + 1
        detail = candidates[index] if index < len(candidates) else None
        records.append(_record(listing, detail, term))
    return records
