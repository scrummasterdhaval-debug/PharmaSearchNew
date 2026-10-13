import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from core.logging_config import get_logger
from sources.parser import extract_dosage_form, extract_pack_size, extract_strength
from sources.salts import base_name


TGA_BASE_URL = "https://www.tga.gov.au"
ARTG_SEARCH_URL = f"{TGA_BASE_URL}/resources/artg"
PICMI_SEARCH_URL = "https://www.ebs.tga.gov.au/ebs/picmi/picmirepository.nsf/PICMI"
# The ARTG leaves scripted clients hanging rather than refusing them, so the
# wait is kept short before the PI/CMI repository is asked instead.
REQUEST_TIMEOUT = 8
MAX_RESULTS = 100
MAX_DETAIL_WORKERS = 6
LICENCE_STATUS = {"A": "Active on ARTG", "C": "Cancelled from ARTG", "S": "Suspended on ARTG"}
logger = get_logger(__name__)
TGA_FALLBACK_ROWS = [
    {
        "substance": "paracetamol",
        "product": "ADMED PARACETAMOL SUSPENSION FOR CHILDREN 1-5 YEARS paracetamol 24 mg/mL strawberry flavour oral suspension bottle",
        "company": "",
        "country": "Australia",
        "region": "AU",
        "status": "Included on ARTG",
        "strength": "24 mg/mL",
        "dosage_form": "Suspension",
        "pack_size": "bottle",
        "registration_number": "528326",
        "source": "TGA Australia",
        "source_url": ARTG_SEARCH_URL,
        "product_url": f"{TGA_BASE_URL}/resources/artg/528326",
        "url": f"{TGA_BASE_URL}/resources/artg/528326",
    },
    {
        "substance": "paracetamol",
        "product": "ADMED PARACETAMOL SUSPENSION FOR CHILDREN 5-12 YEARS paracetamol 48 mg/mL strawberry flavour oral suspension bottle",
        "company": "",
        "country": "Australia",
        "region": "AU",
        "status": "Included on ARTG",
        "strength": "48 mg/mL",
        "dosage_form": "Suspension",
        "pack_size": "bottle",
        "registration_number": "528325",
        "source": "TGA Australia",
        "source_url": ARTG_SEARCH_URL,
        "product_url": f"{TGA_BASE_URL}/resources/artg/528325",
        "url": f"{TGA_BASE_URL}/resources/artg/528325",
    },
]


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip(" :-")


def _registration_number(text: str) -> str:
    match = re.search(r"\((\d{4,})\)\s*$", text or "")
    return match.group(1) if match else ""


def _strip_registration_number(text: str) -> str:
    return re.sub(r"\s*\(\d{4,}\)\s*$", "", text or "").strip()


def _split_sponsor_and_product(title: str) -> tuple[str, str]:
    clean_title = _strip_registration_number(_clean_text(title))
    if " - " not in clean_title:
        return "", clean_title
    sponsor, product = clean_title.split(" - ", 1)
    return _clean_text(sponsor), _clean_text(product)


def _text_after_label(text: str, labels: list[str], max_chars: int = 500) -> str:
    lines = [_clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    normalized = [label.lower() for label in labels]
    for index, line in enumerate(lines):
        lower_line = line.lower().rstrip(":")
        label = next((item for item in normalized if lower_line == item or lower_line.startswith(f"{item}:")), "")
        if not label:
            continue
        remainder = _clean_text(line[len(label):])
        if remainder:
            return remainder[:max_chars]
        for candidate in lines[index + 1 : index + 5]:
            if candidate.lower().rstrip(":") in normalized:
                break
            if candidate:
                return candidate[:max_chars]
    return ""


def _molecule_words(substance: str) -> list[str]:
    """The words a product must mention: "atorvastatin + ezetimibe" -> ["atorvastatin", "ezetimibe"]."""
    return re.findall(r"[a-z0-9]+", base_name(substance).lower())


def _document_links(soup: BeautifulSoup) -> dict[str, str]:
    links = {}
    for anchor in soup.select("a[href]"):
        href = urljoin(TGA_BASE_URL, anchor.get("href", ""))
        text = anchor.get_text(" ", strip=True).lower()
        combined = f"{text} {href.lower()}"
        # Only the documents themselves: every ARTG page also links to TGA's
        # pages explaining what a PI and a CMI are.
        if ".pdf" not in href.lower() and "picmi" not in href.lower():
            continue
        if not links.get("smpc_url") and (
            "product information" in combined or re.search(r"\bpi\b", combined)
        ):
            links["smpc_url"] = href
        elif not links.get("pil_url") and (
            "consumer medicine information" in combined
            or "consumer information" in combined
            or re.search(r"\bcmi\b", combined)
        ):
            links["pil_url"] = href
    return links


def _parse_artg_detail(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    metadata = {
        "product": _text_after_label(text, ["Product name", "Name"]),
        "company": _text_after_label(text, ["Sponsor", "Sponsor name"]),
        "manufacturer_name": _text_after_label(text, ["Manufacturer", "Manufacturer name"]),
        "substance": _text_after_label(text, ["Active ingredient", "Active ingredients", "Ingredients"]),
        "dosage_form": _text_after_label(text, ["Dosage form", "Form"]),
        "route": _text_after_label(text, ["Route of administration", "Route"]),
        "registration_number": _text_after_label(text, ["ARTG ID", "ARTG number", "AUST R", "AUST L"]),
        "registration_date": _text_after_label(text, ["ARTG Date", "Start date", "Effective date", "Registration date"]),
        "licence_status": _text_after_label(text, ["Licence status"]),
        "licence_category": _text_after_label(text, ["Licence category"]),
    }
    metadata.update(_document_links(soup))
    return {key: value for key, value in metadata.items() if value}


def _parse_artg_search_results(html: str, substance: str, source_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_urls = set()
    for anchor in soup.select('a[href*="/resources/artg/"]'):
        href = urljoin(TGA_BASE_URL, anchor.get("href", ""))
        title = _clean_text(anchor.get_text(" ", strip=True))
        if not title or href in seen_urls:
            continue
        # "atorvastatin (as calcium trihydrate)": the molecule is in the title
        # even when the salt is worded differently from the search.
        if not all(token in title.lower() for token in _molecule_words(substance)):
            continue
        seen_urls.add(href)
        sponsor, product = _split_sponsor_and_product(title)
        results.append(
            {
                "substance": substance,
                "product": product or title,
                "company": sponsor,
                "country": "Australia",
                "region": "AU",
                "status": "Included on ARTG",
                "strength": extract_strength(product or title),
                "dosage_form": extract_dosage_form(product or title),
                "pack_size": extract_pack_size(product or title),
                "registration_number": _registration_number(title),
                "source": "TGA Australia",
                "source_url": source_url,
                "product_url": href,
                "url": href,
            }
        )
    return results


def _fetch_search_results(substance: str) -> tuple[str, str]:
    query_attempts = [{"keywords": substance}]
    last_error = None
    for params in query_attempts:
        try:
            response = requests.get(
                ARTG_SEARCH_URL,
                params=params,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            return response.text, response.url
        except requests.RequestException as exc:
            last_error = exc
            logger.warning("TGA ARTG search failed with params %s: %s", params, exc)
    raise requests.RequestException(last_error)


def _fallback_rows(substance: str, limit: int) -> list[dict[str, Any]]:
    query = substance.strip().lower()
    if not query:
        return []
    display_substance = substance.strip()
    return [
        {
            "substance": display_substance,
            "product": f"ARTG lookup for {display_substance}",
            "company": "",
            "country": "Australia",
            "region": "AU",
            "status": "Open official TGA ARTG registry - direct live parser unavailable or no exact match",
            "strength": extract_strength(display_substance),
            "dosage_form": extract_dosage_form(display_substance),
            "pack_size": extract_pack_size(display_substance),
            "registration_number": "",
            "document_type": "Official registry search handoff",
            "source": "TGA Australia",
            "source_url": f"{ARTG_SEARCH_URL}?keywords={display_substance}",
            "product_url": f"{ARTG_SEARCH_URL}?keywords={display_substance}",
            "url": f"{ARTG_SEARCH_URL}?keywords={display_substance}",
            "connector_mode": "manual_registry",
        }
    ][:limit]


def _fetch_detail(url: str) -> dict[str, str]:
    try:
        response = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        return _parse_artg_detail(response.text)
    except requests.RequestException as exc:
        logger.warning("TGA ARTG detail request failed for %s: %s", url, exc)
        return {}


def _merge_detail(row: dict[str, Any], detail: dict[str, str]) -> dict[str, Any]:
    merged = dict(row)
    if detail.get("substance"):
        merged["active_substance"] = detail["substance"]
        merged["source_substance"] = detail["substance"]
    if detail.get("licence_status"):
        merged["status"] = LICENCE_STATUS.get(detail["licence_status"].upper(), detail["licence_status"])
    for field in [
        "substance",
        "product",
        "company",
        "manufacturer_name",
        "dosage_form",
        "route",
        "registration_number",
        "registration_date",
        "smpc_url",
        "pil_url",
    ]:
        if detail.get(field) and not merged.get(field):
            merged[field] = detail[field]
    product = merged.get("product", "")
    if not merged.get("strength"):
        merged["strength"] = extract_strength(product)
    if not merged.get("dosage_form"):
        merged["dosage_form"] = extract_dosage_form(product)
    if not merged.get("pack_size"):
        merged["pack_size"] = extract_pack_size(product)
    manufacturer = detail.get("manufacturer_name", "")
    if manufacturer:
        merged["manufacturer_source"] = "TGA ARTG product detail"
        merged["manufacturers"] = [{
            "name": manufacturer,
            "role": "MANUFACTURER_UNKNOWN_ROLE",
            "verification_status": "VERIFIED_PRODUCT_PAGE",
        }]
        merged["evidence"] = [{
            "field_name": "manufacturer_name",
            "value": manufacturer,
            "role": "MANUFACTURER_UNKNOWN_ROLE",
            "source_regulator": "TGA Australia",
            "document_type": "ARTG product detail",
            "evidence_url": merged.get("product_url", ""),
            "evidence_section": "Manufacturer",
            "extraction_method": "OFFICIAL_HTML_FIELD",
            "verification_status": "VERIFIED_PRODUCT_PAGE",
        }]
    return merged


def _enrich_details(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows
    detail_by_url = {}
    urls = [row["product_url"] for row in rows[:MAX_RESULTS] if row.get("product_url")]
    with ThreadPoolExecutor(max_workers=min(MAX_DETAIL_WORKERS, len(urls))) as executor:
        jobs = {executor.submit(_fetch_detail, url): url for url in urls}
        for job in as_completed(jobs):
            detail_by_url[jobs[job]] = job.result()
    return [_merge_detail(row, detail_by_url.get(row.get("product_url"), {})) for row in rows]


def parse_picmi_results(html: str) -> list[dict[str, str]]:
    """Trade name, PI and CMI links and active ingredients, one entry per table row."""
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for row in soup.select("div.tblResults tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        links = {}
        for anchor in cells[1].find_all("a", href=True):
            kind = _clean_text(anchor.get_text()).upper()
            if kind in ("PI", "CMI"):
                links[kind] = urljoin(PICMI_SEARCH_URL, anchor["href"])
        entries.append({
            "trade_name": _clean_text(cells[0].get_text(" ")),
            "ingredients": _clean_text(cells[2].get_text(" ")),
            "pi_url": links.get("PI", ""),
            "cmi_url": links.get("CMI", ""),
        })
    return entries


def _picmi_rows(substance: str, entries: list[dict[str, str]], source_url: str) -> list[dict[str, Any]]:
    """One row per trade name; "Sevelamer Lupin" and "SEVELAMER LUPIN" are one product."""
    molecule = _molecule_words(substance)
    grouped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not all(token in f"{entry['trade_name']} {entry['ingredients']}".lower() for token in molecule):
            continue
        row = grouped.setdefault(entry["trade_name"].upper(), {
            "substance": entry["ingredients"],
            "active_substance": entry["ingredients"],
            "source_substance": entry["ingredients"],
            "product": entry["trade_name"],
            "company": "",
            "country": "Australia",
            "region": "AU",
            "status": "PI/CMI published by TGA",
            "registration_number": "",
            "source": "TGA Australia",
            "source_url": source_url,
            "product_url": source_url,
            "url": source_url,
            "document_type": "TGA PI/CMI repository entry",
        })
        if entry["pi_url"] and not row.get("smpc_url"):
            row["smpc_url"] = entry["pi_url"]
        if entry["cmi_url"] and not row.get("pil_url"):
            row["pil_url"] = entry["cmi_url"]
        if len(entry["ingredients"]) > len(row["active_substance"]):
            row["active_substance"] = row["source_substance"] = row["substance"] = entry["ingredients"]
    return list(grouped.values())


def _picmi_search(substance: str, limit: int) -> list[dict[str, Any]]:
    # A combination is found through its first molecule and kept only when
    # the ingredients name every one of them.
    words = _molecule_words(substance)
    for term in dict.fromkeys([substance, base_name(substance), *words[:1]]):
        response = requests.get(
            PICMI_SEARCH_URL,
            params={"OpenForm": "", "t": "", "q": term},
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        rows = _picmi_rows(substance, parse_picmi_results(response.text), response.url)
        if rows:
            return rows[:limit]
    return []


def run_tga_search(substance: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    try:
        html, source_url = _fetch_search_results(substance)
        rows = _parse_artg_search_results(html, substance, source_url)[:limit]
        if rows:
            return _enrich_details(rows)
    except requests.RequestException as exc:
        logger.warning("TGA ARTG search unavailable: %s", exc)

    # The ARTG search answers browsers but not scripts. TGA's PI/CMI
    # repository does, and lists every product with a published PI or CMI
    # (all prescription medicines) with its active ingredients. It does not
    # name the sponsor, so the company stays empty rather than guessed.
    try:
        return _picmi_search(substance, limit)
    except requests.RequestException as exc:
        logger.warning("TGA PI/CMI repository unavailable: %s", exc)
        return _fallback_rows(substance, limit)
