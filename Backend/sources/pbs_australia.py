"""Australia's Pharmaceutical Benefits Scheme, from its monthly "PBS API CSV files".

The Department of Health publishes the whole PBS Schedule each month as a zip
of CSV tables on pbs.gov.au. Every listed brand names its sponsor, which the
TGA connector cannot supply: the ARTG blocks scripts, so TGA rows come from
the PI/CMI list, which has no sponsor.

This is its own source, "PBS Australia". Its sponsor is not copied onto TGA
rows, and it covers only PBS-subsidised medicines, not every product on the
ARTG. The sponsor is the company responsible for the listing, so it fills
the company column and never the manufacturer column.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

from sources.open_registers import (
    DOWNLOAD_TIMEOUT,
    OpenRegister,
    _match_text,
    add_register,
    download_file,
    search_register,
)

PBS_PUBLICATIONS = "https://www.pbs.gov.au/browse/publications"
PBS_ITEM_URL = "https://www.pbs.gov.au/medicine/item/{}"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _clean(value: object) -> str:
    text = " ".join(str(value or "").split())
    return "" if text.lower() == "null" else text


def fetch_pbs(register: OpenRegister, workdir: Path) -> Path:
    """The zip's name carries the schedule date, so take the newest one linked."""
    response = requests.get(PBS_PUBLICATIONS, headers=HEADERS, timeout=DOWNLOAD_TIMEOUT)
    response.raise_for_status()
    links = re.findall(r'href="([^"]*PBS-API-CSV-files\.zip[^"]*)"', response.text)
    if not links:
        raise RuntimeError("pbs.gov.au publications page links no PBS API CSV zip")
    url = sorted(links)[-1]
    if url.startswith("/"):
        url = "https://www.pbs.gov.au" + url
    return download_file(register, url, workdir / "pbs_api_csv_files.zip")


def _table(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    member = next(n for n in archive.namelist() if n.endswith(f"/{name}.csv") or n == f"{name}.csv")
    with archive.open(member) as handle:
        yield from csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig", errors="replace"))


def read_pbs(register: OpenRegister, path: Path) -> Iterator[dict[str, str]]:
    """One record per PBS code and brand, with its sponsor and ATC code joined in.

    items.csv repeats a brand once per program and restriction; those repeats
    say nothing more about the product, so the first is kept.
    """
    with zipfile.ZipFile(path) as archive:
        sponsors = {row["organisation_id"]: _clean(row["name"]) for row in _table(archive, "organisations")}
        atc: dict[str, str] = {}
        for row in _table(archive, "item-atc-relationships"):
            atc.setdefault(row["pbs_code"], _clean(row["atc_code"]))
        seen: set[tuple[str, str]] = set()
        for item in _table(archive, "items"):
            key = (item["pbs_code"], item["brand_name"])
            if key in seen:
                continue
            seen.add(key)
            yield {
                **item,
                "sponsor_name": sponsors.get(item.get("organisation_id", ""), ""),
                "atc_code": atc.get(item["pbs_code"], ""),
            }


def build_pbs_rows(records: Iterable[dict[str, str]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        drug = _clean(record.get("drug_name"))
        brand = _clean(record.get("brand_name"))
        if not drug:
            continue
        form = _clean(record.get("li_form"))
        pbs_code = _clean(record.get("pbs_code"))
        yield _match_text(drug), {
            "substance": drug,
            "active_substance": drug,
            "source_substance": drug,
            "product": f"{brand} {form}".strip() if brand else form,
            "company": _clean(record.get("sponsor_name")),
            "country": "Australia",
            "region": "AU",
            "status": "PBS listed",
            "dosage_form": form,
            "route": _clean(record.get("moa_preferred_term")),
            "pack_size": _clean(record.get("pack_size")),
            "atc_code": _clean(record.get("atc_code")),
            "registration_number": pbs_code,
            "registration_date": _clean(record.get("first_listed_date")),
            "source": "PBS Australia",
            "source_url": PBS_PUBLICATIONS,
            "product_url": PBS_ITEM_URL.format(pbs_code) if pbs_code else "",
            "document_type": "PBS Schedule listing",
            "last_checked": fetched_at,
        }


PBS_AUSTRALIA = add_register(OpenRegister(
    source="PBS Australia",
    country="Australia",
    region="AU",
    url=PBS_PUBLICATIONS,
    slug="pbs_australia",
    max_age_seconds=7 * 24 * 3600,  # republished on the 1st of each month
    build_rows=build_pbs_rows,
    fetch=fetch_pbs,
    read_records=read_pbs,
))


def run_pbs_australia_search(substance: str) -> list[dict[str, Any]]:
    return search_register(PBS_AUSTRALIA, substance)
