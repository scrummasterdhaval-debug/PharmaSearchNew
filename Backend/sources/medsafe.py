"""Medsafe New Zealand: the product register and each product's detail page.

The search page answers a plain GET (``?qry=Product&ingr=...``) with every
match on one page. The detail page names the sponsor, the composition and,
unlike most registers, the site behind each manufacturing step.
"""
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from core.logging_config import get_logger
from sources.salts import base_name


MEDSAFE_BASE_URL = "https://www.medsafe.govt.nz"
PRODUCT_SEARCH_URL = f"{MEDSAFE_BASE_URL}/DbSearch/"
REQUEST_TIMEOUT = 30
MAX_RESULTS = 200
DETAIL_WORKERS = 6
DETAIL_DEADLINE_SECONDS = 60
HEADERS = {"User-Agent": "Mozilla/5.0"}
FINISHED_PRODUCT_STEP = "manufacture of final dose form"
API_STEP = "manufacture of active ingredient"
logger = get_logger(__name__)


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _lookup_url(term: str) -> str:
    return f"{PRODUCT_SEARCH_URL}?qry=Product&ingr={quote(term.strip())}"


def _fallback_rows(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """A pointer to the register, for when Medsafe does not answer at all."""
    display_substance = substance.strip()
    if not display_substance:
        return []
    return [
        {
            "substance": display_substance,
            "product": f"{display_substance} official New Zealand registry search",
            "company": "",
            "country": "New Zealand",
            "region": "NZ",
            "status": "Open official Medsafe registry - register did not answer",
            "registration_number": "",
            "document_type": "Official registry search handoff",
            "source": "Medsafe New Zealand",
            "source_url": _lookup_url(display_substance),
            "product_url": _lookup_url(display_substance),
            "url": _lookup_url(display_substance),
            "connector_mode": "manual_registry",
        }
    ][:limit]


LISTING_NAME = re.compile(r"^(?P<name>.+?),\s*(?P<rest>.+?)(?:\s*\((?P<classification>[^)]*)\))?$")


def parse_search_results(html: str) -> list[dict[str, str]]:
    """One entry per product row of ``#productGrid``."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table#productGrid") or soup.select_one("table.quickgrid")
    if table is None:
        return []
    results = []
    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        link = cells[0].find("a", href=True) if cells else None
        if len(cells) < 6 or link is None:
            continue
        listing = _clean_text(link.get_text(" "))
        match = LISTING_NAME.match(listing)
        results.append({
            "listing": listing,
            "trade_name": _clean_text(match.group("name")) if match else listing,
            "classification": _clean_text(match.group("classification")) if match and match.group("classification") else "",
            "ingredients": _clean_text(cells[1].get_text(" ")),
            "sponsor": _clean_text(cells[2].get_text(" ")),
            "status": _clean_text(cells[3].get_text(" ")),
            "approval_date": _clean_text(cells[4].get_text(" ")),
            "notification_date": _clean_text(cells[5].get_text(" ")),
            "url": urljoin(PRODUCT_SEARCH_URL, link["href"]),
        })
    return results


def _lines(cell: Any) -> list[str]:
    """A cell's text split where the page breaks lines (name, street, town, COUNTRY)."""
    return [line for line in (_clean_text(part) for part in cell.get_text("\n").split("\n")) if line]


def _site(cell: Any) -> dict[str, str]:
    lines = _lines(cell)
    if not lines:
        return {}
    # Overseas addresses end with the country in capitals; New Zealand ones do not.
    country = lines[-1].title() if len(lines) > 1 and lines[-1].isupper() and not re.search(r"\d", lines[-1]) else ""
    return {"name": lines[0], "country": country, "address": ", ".join(lines[1:])}


def _table_after(soup: BeautifulSoup, heading: str) -> Any:
    for title in soup.find_all("h4"):
        if _clean_text(title.get_text()).lower() == heading:
            return title.find_next("table")
    return None


def parse_product_detail(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    detail: dict[str, Any] = {}
    file_ref = soup.find(string=re.compile(r"File ref:"))
    if file_ref:
        detail["file_ref"] = _clean_text(str(file_ref).split("File ref:", 1)[1])

    # The header table alternates a row of headings with a row of values.
    header = file_ref.find_parent("table").find_next("table") if file_ref else None
    if header is not None:
        rows = header.find_all("tr")
        for heading_row, value_row in zip(rows, rows[1:]):
            headings = [_clean_text(th.get_text(" ")) for th in heading_row.find_all("th")]
            values = value_row.find_all("td")
            for name, cell in zip(headings, values):
                detail[name] = cell

    for key in ("Trade Name", "Dose Form", "Strength", "Identifier", "Application date", "Classification"):
        if key in detail:
            detail[key] = _clean_text(detail[key].get_text(" "))
    if "Sponsor" in detail:
        detail["Sponsor"] = (_lines(detail["Sponsor"]) or [""])[0]
    if "Regulatory status" in detail:
        status_lines = _lines(detail["Regulatory status"])
        detail["Regulatory status"] = status_lines[0] if status_lines else ""
        for line in status_lines[1:]:
            label, _, value = line.partition(":")
            detail[_clean_text(label)] = _clean_text(value)

    actives: list[str] = []
    api_sites: list[dict[str, str]] = []
    composition = _table_after(soup, "composition")
    if composition is not None:
        in_actives = False
        for row in composition.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            kind = _clean_text(cells[1].get_text(" "))
            if kind in ("Active", "Excipient"):
                in_actives = kind == "Active"
                continue
            if in_actives and kind and kind not in actives:
                actives.append(kind)
                site = _site(cells[2])
                if site and site not in api_sites:
                    api_sites.append(site)
    detail["actives"] = actives

    steps: dict[str, list[dict[str, str]]] = {}
    production = _table_after(soup, "production")
    if production is not None:
        step = ""
        for row in production.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            step = _clean_text(cells[0].get_text(" ")) or step
            site = _site(cells[1])
            if site:
                steps.setdefault(step.lower(), []).append(site)
    detail["steps"] = steps
    detail["api_sites"] = steps.get(API_STEP) or api_sites

    packs = []
    packaging = _table_after(soup, "packaging")
    if packaging is not None:
        for row in packaging.select("tbody tr"):
            cells = [_clean_text(cell.get_text(" ")) for cell in row.find_all("td")]
            if len(cells) >= 2 and cells[1]:
                packs.append(f"{cells[0]}, {cells[1]}" if cells[0] else cells[1])
    detail["packs"] = packs
    return detail


def _strength_from(actives: list[str]) -> str:
    strengths = [match.group(0) for match in (re.search(r"[\d.,]+\s*(?:mg|g|mcg|µg|microgram\w*|IU|%|mL)\b.*$", a, re.I) for a in actives) if match]
    return "/".join(strengths)


def _molecules(actives: list[str]) -> str:
    return "; ".join(
        _clean_text(re.sub(r"\s*[\d.,]+\s*(?:mg|g|mcg|µg|microgram\w*|IU|%|mL)\b.*$", "", active, flags=re.I))
        for active in actives
    )


def build_row(listing: dict[str, str], detail: dict[str, Any]) -> dict[str, Any]:
    actives = detail.get("actives") or []
    molecules = _molecules(actives) or listing.get("ingredients", "")
    makers = detail.get("steps", {}).get(FINISHED_PRODUCT_STEP, [])
    api_sites = detail.get("api_sites") or []
    trade_name = detail.get("Trade Name") or listing.get("trade_name", "")
    strength = detail.get("Strength") or _strength_from(actives)
    status = detail.get("Regulatory status") or listing.get("status", "")
    manufacturer = "; ".join(site["name"] for site in makers)
    row: dict[str, Any] = {
        "substance": molecules,
        "active_substance": molecules,
        "source_substance": molecules,
        "product": " ".join(part for part in (trade_name, strength) if part),
        "company": detail.get("Sponsor") or listing.get("sponsor", ""),
        "country": "New Zealand",
        "region": "NZ",
        "status": status,
        "classification": detail.get("Classification") or listing.get("classification", ""),
        "strength": strength,
        "dosage_form": detail.get("Dose Form", ""),
        "pack_size": "; ".join(detail.get("packs") or []),
        "registration_number": detail.get("file_ref", ""),
        "registration_date": detail.get("Approval date") or listing.get("approval_date", ""),
        "status_date": detail.get("Notification date") or listing.get("notification_date", ""),
        "manufacturer_name": manufacturer,
        "manufacturer_country": "; ".join(dict.fromkeys(site["country"] for site in makers if site["country"])),
        "manufacturer_source": "Medsafe product detail: Manufacture of Final Dose Form" if manufacturer else "",
        "api_manufacturer": "; ".join(dict.fromkeys(site["name"] for site in api_sites)),
        "api_manufacturer_country": "; ".join(dict.fromkeys(site["country"] for site in api_sites if site["country"])),
        "source": "Medsafe New Zealand",
        "source_url": _lookup_url(listing.get("query", "") or molecules),
        "product_url": listing["url"],
        "url": listing["url"],
        "document_type": "Medsafe product detail",
    }
    if makers:
        row["manufacturers"] = [
            {
                "name": site["name"],
                "country": site["country"],
                "address": site["address"],
                "role": "FINISHED_PRODUCT_MANUFACTURER",
                "verification_status": "VERIFIED_PRODUCT_PAGE",
            }
            for site in makers
        ]
        row["evidence"] = [
            {
                "field_name": "manufacturer_name",
                "value": manufacturer,
                "role": "FINISHED_PRODUCT_MANUFACTURER",
                "source_regulator": "Medsafe New Zealand",
                "document_type": "Medsafe product detail",
                "evidence_url": listing["url"],
                "evidence_section": "Production: Manufacture of Final Dose Form",
                "extraction_method": "OFFICIAL_HTML_FIELD",
                "verification_status": "VERIFIED_PRODUCT_PAGE",
            }
        ]
    return row


def _search(session: requests.Session, term: str) -> list[dict[str, str]]:
    response = session.get(
        PRODUCT_SEARCH_URL, params={"qry": "Product", "ingr": term}, headers=HEADERS, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    listings = parse_search_results(response.text)
    for listing in listings:
        listing["query"] = term
    return listings


def _detail(session: requests.Session, url: str) -> dict[str, Any]:
    response = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return parse_product_detail(response.text)


def run_medsafe_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    term = _clean_text(substance)
    if not term:
        return []
    session = requests.Session()
    try:
        listings = _search(session, term)
        # Medsafe files most salts under the bare molecule ("Sevelamer"); the
        # composition on each detail page then says which salt it is.
        molecule = base_name(term)
        if not listings and molecule.lower() != term.lower():
            listings = _search(session, molecule)
    except requests.RequestException as exc:
        logger.warning("Medsafe product search unavailable: %s", exc)
        return _fallback_rows(term, limit)
    listings = listings[:limit]
    if not listings:
        return []

    details: dict[str, dict[str, Any]] = {}
    executor = ThreadPoolExecutor(max_workers=min(DETAIL_WORKERS, len(listings)))
    futures = {executor.submit(_detail, session, listing["url"]): listing["url"] for listing in listings}
    try:
        for future in as_completed(futures, timeout=DETAIL_DEADLINE_SECONDS):
            try:
                details[futures[future]] = future.result()
            except requests.RequestException as exc:
                logger.info("Medsafe product page %s unavailable: %s", futures[future], exc)
    except FuturesTimeout:
        logger.info("Medsafe: product pages not all read before the deadline")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return [build_row(listing, details.get(listing["url"], {})) for listing in listings]
