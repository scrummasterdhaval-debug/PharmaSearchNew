"""Registers regulators in Central and South Asia publish whole.

    Kazakhstan  NDDA's State Register of Medicinal Products, through the
                register site's own public JSON service: trade name, INN,
                form, producer and its country, registration number and
                dates, ATC, prescription status, and whether the product was
                registered nationally or under EAEU rules.

Indexed locally like the other registers (open_registers). The register is in
Russian, so rows are put into English and matched the way Russia's are
(grls_russia: transliterated, then folded to one spelling).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from sources.europe_registers import _folded_search
from sources.grls_russia import english_company, english_country, english_form, latin, match_text
from sources.national_registers import _clean
from sources.open_registers import OpenRegister, add_register


# ------------------------------------------------------------- Kazakhstan

KAZAKHSTAN_API = "https://register.ndda.kz/register/api/v1/PharmaRegister/list/paged"
KAZAKHSTAN_PAGE = "https://register.ndda.kz/"
KAZAKHSTAN_PAGE_SIZE = 200  # the service's own ceiling
KAZAKHSTAN_KIND = {"Eaes": "EAEU registration", "National": "National registration"}


def fetch_kazakhstan(register: OpenRegister, workdir: Path) -> Path:
    """Every page of the register, as JSON lines."""
    import requests

    from sources.open_registers import DOWNLOAD_TIMEOUT

    session = requests.Session()
    session.headers["User-Agent"] = "PharmaSearch/1.0 (regulatory register download)"
    destination = workdir / "ndda_register.jsonl"
    page, last_page = 1, 1
    with destination.open("w", encoding="utf-8") as handle:
        while page <= last_page:
            for attempt in range(3):
                try:
                    response = session.post(
                        KAZAKHSTAN_API,
                        json={"currentPage": page, "perPage": KAZAKHSTAN_PAGE_SIZE},
                        timeout=DOWNLOAD_TIMEOUT,
                    )
                    response.raise_for_status()
                    body = response.json()
                    break
                except (requests.RequestException, ValueError):
                    if attempt == 2:
                        raise
                    time.sleep(5 * (attempt + 1))
            last_page = int(body.get("lastPage") or page)
            for record in body.get("data") or []:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            page += 1
    return destination


def read_kazakhstan(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _numbered(value: str) -> list[str]:
    """ "1. Labena d.o.o., 2. JSC ..." -> each producer; a single one as it is."""
    import re

    parts = [part.strip(" ,") for part in re.split(r"(?:^|,\s*)\d+\.\s*", value) if part.strip(" ,")]
    return parts or ([value] if value else [])


def _producer_countries(value: str) -> str:
    return "; ".join(dict.fromkeys(english_country(part.title()) for part in _numbered(value)))


def build_kazakhstan_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    seen: set[str] = set()
    for record in records:
        key = _clean(record.get("sourceId")) or str(record.get("id"))
        if key in seen:
            continue
        seen.add(key)
        inn = _clean(record.get("mnn"))
        trade = _clean(record.get("drugTradeName"))
        if not (inn or trade):
            continue
        active = latin(inn)
        maker = "; ".join(english_company(name) for name in _numbered(_clean(record.get("producerName"))))
        tokens = match_text(inn or trade)
        yield tokens, {
            "substance": active,
            "active_substance": active,
            "source_substance": active,
            "local_substance": inn,
            "product": latin(trade),
            "local_product_name": trade,
            # The register names the producer, not the holder.
            "company": "",
            "country": "Kazakhstan",
            "region": "AS",
            "status": "Registered",
            "authorisation_scope": KAZAKHSTAN_KIND.get(_clean(record.get("sourceType")), _clean(record.get("sourceType"))),
            "dosage_form": english_form(_clean(record.get("lekFormName"))),
            "atc_code": _clean(record.get("atcCode")),
            "classification": "Prescription" if record.get("recipeSign") else "Without prescription",
            "registration_number": latin(_clean(record.get("regNumber"))),
            "registration_date": _clean(record.get("regDate"))[:10],
            "expiry_date": _clean(record.get("expirationDate"))[:10] or "Unlimited",
            "manufacturer_name": maker,
            "manufacturer_country": _producer_countries(_clean(record.get("countryName"))),
            "manufacturer_source": "NDDA State Register of Medicinal Products (producer)" if maker else "",
            "inn_fold_tokens": tokens.strip(),
            "source": "NDDA Kazakhstan",
            "source_url": KAZAKHSTAN_PAGE,
            "product_url": "",
            "document_type": "NDDA State Register of Medicinal Products record",
            "last_checked": fetched_at,
        }


NDDA_KAZAKHSTAN = add_register(OpenRegister(
    source="NDDA Kazakhstan",
    country="Kazakhstan",
    region="AS",
    url=KAZAKHSTAN_PAGE,
    slug="ndda_kazakhstan",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_kazakhstan_rows,
    fetch=fetch_kazakhstan,
    read_records=read_kazakhstan,
))


def run_ndda_kazakhstan_search(substance: str) -> list[dict[str, Any]]:
    return _folded_search(NDDA_KAZAKHSTAN, substance, "NDDA Kazakhstan register")
