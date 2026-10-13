"""Saudi Arabia: the SFDA drugs list, searched from a local copy.

    https://www.sfda.gov.sa/en/drugs-list

SFDA publishes no downloadable list of human medicines -- its open data covers
veterinary drugs, cosmetics, devices and food -- and the list's filter answers
an automated request with the unfiltered first page. The list itself pages
openly: about 21,000 registrations, 15 to a page, each with scientific name,
trade name, strength and price, and a link to the registration's details.

So the list is read whole, a few pages at a time, and indexed locally like the
other open registers, refreshing weekly in the background. The details page of
each registration a search returns is then read for what the list leaves out:
register number, ATC code, dosage form, route, pack, manufacturer, marketing
company and agent. Values SFDA leaves blank stay blank.
"""

from __future__ import annotations

import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

from core.logging_config import get_logger
from sources.open_registers import OpenRegister, _match_text, add_register, search_register


logger = get_logger(__name__)

BASE_URL = "https://www.sfda.gov.sa"
LIST_URL = f"{BASE_URL}/en/drugs-list"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
REQUEST_TIMEOUT = 40
LIST_WORKERS = 4
PAUSE_SECONDS = 0.2
DETAIL_WORKERS = 6
DETAIL_DEADLINE_SECONDS = 20
DETAIL_LIMIT = 300

ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
DETAIL_LINK = re.compile(r'href="([^"]*details_data[^"]*)"')
PAGE_LINK = re.compile(r"[?&]page=(\d+)")
FIELD = re.compile(r"<tr>\s*<th>(.*?)</th>\s*<td>(.*?)</td>\s*</tr>", re.S)


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def parse_list_page(page_html: str) -> list[dict[str, str]]:
    records = []
    for row in ROW.findall(page_html):
        cells = [_text(cell) for cell in CELL.findall(row)]
        link = DETAIL_LINK.search(row)
        if len(cells) < 5 or not link:
            continue
        records.append({
            "scientific_name": cells[0],
            "trade_name": cells[1],
            "strength": cells[2],
            "dosage_form": cells[3],
            "price": cells[4],
            "details": html.unescape(link.group(1)),
        })
    return records


def fetch_list(register: OpenRegister, workdir: Path) -> Path:
    """Every page of the list, as JSON lines. Pages that fail are retried once."""
    session = _session()
    first = session.get(LIST_URL, timeout=REQUEST_TIMEOUT)
    first.raise_for_status()
    last_page = max((int(number) for number in PAGE_LINK.findall(first.text)), default=0)
    started = time.time()

    def read(page: int) -> list[dict[str, str]]:
        # A page can come back 200 and empty under load; only the last page may
        # be short, so an empty one is tried again rather than lost.
        for attempt in range(3):
            try:
                time.sleep(PAUSE_SECONDS * (attempt + 1))
                response = session.get(LIST_URL, params={"page": page}, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                records = parse_list_page(response.text)
                if records or page == last_page:
                    return records
            except requests.RequestException:
                if attempt == 2:
                    raise
        raise requests.RequestException(f"SFDA list page {page} came back empty")

    target = workdir / "sfda_drugs.jsonl"
    failed = 0
    with target.open("w", encoding="utf-8") as handle, ThreadPoolExecutor(max_workers=LIST_WORKERS) as executor:
        for records in executor.map(lambda page: _safe(read, page), range(last_page + 1)):
            if records is None:
                failed += 1
                continue
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.info(
        "SFDA drugs list: %s pages read in %.0fs, %s failed", last_page + 1, time.time() - started, failed
    )
    if failed > (last_page + 1) // 20:
        raise requests.RequestException(f"SFDA drugs list: {failed} of {last_page + 1} pages failed")
    return workdir


def _safe(read, page):
    try:
        return read(page)
    except requests.RequestException as exc:
        logger.info("SFDA list page %s unavailable: %s", page, exc)
        return None


def read_list(register: OpenRegister, workdir: Path) -> Iterator[dict[str, str]]:
    with (workdir / "sfda_drugs.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def build_sfda_rows(records: Iterable[dict[str, str]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    seen = set()
    for record in records:
        details = record.get("details", "")
        if not record.get("scientific_name") or details in seen:
            continue
        seen.add(details)
        product_url = BASE_URL + details if details.startswith("/") else details
        trade = record.get("trade_name", "")
        yield _match_text(record["scientific_name"]), {
            "substance": record["scientific_name"],
            "source_substance": record["scientific_name"],
            "product": trade,
            "country": "Saudi Arabia",
            "region": "ME",
            "status": "Registered",
            "strength": record.get("strength", ""),
            "dosage_form": record.get("dosage_form", ""),
            "price": f"{record['price']} SAR" if record.get("price") else "",
            "source": "SFDA Saudi Arabia",
            "source_url": LIST_URL,
            "product_url": product_url,
            "url": product_url,
            "document_type": "SFDA drugs list record",
            "last_checked": fetched_at,
        }


SFDA_DRUGS = add_register(
    OpenRegister(
        source="SFDA Saudi Arabia",
        country="Saudi Arabia",
        region="ME",
        url=LIST_URL,
        slug="sfda_drugs_list",
        max_age_seconds=7 * 24 * 3600,
        build_rows=build_sfda_rows,
        fetch=fetch_list,
        read_records=read_list,
    )
)


def parse_details(page_html: str) -> dict[str, str]:
    """The registration's fields as SFDA labels them; "Array" is SFDA's blank."""
    fields = {}
    for label, value in FIELD.findall(page_html):
        text = _text(value)
        fields[_text(label)] = "" if text == "Array" else text
    return fields


def _same_product(detail_name: str, list_name: str) -> bool:
    detail = " ".join(re.findall(r"[a-z0-9]+", detail_name.lower()))
    listed = " ".join(re.findall(r"[a-z0-9]+", list_name.lower()))
    return bool(detail and listed) and (detail.startswith(listed) or listed.startswith(detail))


def _with_details(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows
    session = _session()

    def read(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        page = re.search(r"[?&]page=(\d+)", row["product_url"])
        referer = f"{LIST_URL}?page={page.group(1)}" if page else LIST_URL
        response = session.get(row["product_url"], timeout=REQUEST_TIMEOUT, headers={"Referer": referer})
        response.raise_for_status()
        return row, parse_details(response.text)

    executor = ThreadPoolExecutor(max_workers=DETAIL_WORKERS)
    futures = [executor.submit(read, row) for row in rows[:DETAIL_LIMIT]]
    try:
        for future in as_completed(futures, timeout=DETAIL_DEADLINE_SECONDS):
            try:
                row, fields = future.result()
            except requests.RequestException:
                continue
            # SFDA's details page has at times answered every id with one
            # product (ZETRON 250 MG CAPSULE, azithromycin), browsers included.
            # A page is only used when it describes the row's own product.
            if not _same_product(fields.get("Trade Name", ""), row.get("product", "")):
                logger.info("SFDA details for %s showed %s; not used", row.get("product"), fields.get("Trade Name"))
                continue
            size = " ".join(part for part in (fields.get("Size", ""), fields.get("Size Unit", "")) if part)
            pack = fields.get("Package Size", "")
            updates = {
                "registration_number": fields.get("Register Number", ""),
                "atc_code": fields.get("ATC Code 1", ""),
                "status": fields.get("Authorization Status", "") or fields.get("Marketing Status", ""),
                "dosage_form": fields.get("Dosage Form", "") or row.get("dosage_form", ""),
                "route": fields.get("Route of Administration", ""),
                "pack_size": " ".join(part for part in (pack, size, fields.get("Package Type", "")) if part),
                "manufacturer_name": fields.get("Manufacturer Name", ""),
                "manufacturer_country": fields.get("Manufacturer Country", ""),
                "company": fields.get("Marketing Company", ""),
                "ma_holder_country": fields.get("Country of marketing company", ""),
                "local_agent": fields.get("First agent", ""),
            }
            row.update({key: value for key, value in updates.items() if value})
            if row.get("manufacturer_name"):
                row["manufacturer_source"] = "SFDA registration details"
    except FuturesTimeout:
        logger.info("SFDA: registration details not all read before the deadline")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return rows


def run_sfda_saudi_search(substance: str) -> list[dict[str, Any]]:
    return _with_details(search_register(SFDA_DRUGS, substance))
