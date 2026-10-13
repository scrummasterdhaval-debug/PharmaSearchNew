"""Health Canada's Drug Product Database, searched from a local copy.

The per-product connector found every drug code for a molecule in one request,
then spent six more requests on each product and stopped at 50: acetaminophen
has 1,348, and all of them would have cost 8,088 calls.

The same API returns a whole dataset when no product is named, so the register
is fetched once, dataset by dataset, and joined on drug_code locally:

    drugproduct       58,260 products   DIN, brand, company, class
    activeingredient  ingredients and strengths
    form, route       dosage forms and routes of administration
    packaging         pack size
    status            current status and first market date
    schedule          prescription, non-prescription, narcotic ...
    therapeuticclass  ATC code and name -- for 48,048 products, which the
                      per-product connector never asked for

It is slow to fetch -- about eight minutes, from health-products.canada.ca -- so
it refreshes in the background, weekly, and searches read the local index.

Product links point at the API record for the product. The DPD web pages the
per-product connector linked, and read Product Monographs from, answer 404 --
checked on 2026-09-13 from a browser as well as from code, Tomcat reporting
/dpd-bdpp/info.do "not available" -- so those links and the monograph lookup
behind them cannot work until Health Canada puts the pages back.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator

from core.logging_config import get_logger
from sources.open_registers import OpenRegister, _match_text, add_register, download_file, search_register


logger = get_logger(__name__)

API_URL = "https://health-products.canada.ca/api/drug"

# Datasets and the query each is served with; packaging takes no language.
DATASETS = {
    "drugproduct": "lang=en&type=json",
    "activeingredient": "lang=en&type=json",
    "form": "lang=en&type=json",
    "route": "lang=en&type=json",
    "packaging": "type=json",
    "status": "lang=en&type=json",
    "schedule": "lang=en&type=json",
    "therapeuticclass": "lang=en&type=json",
}


def fetch_datasets(register: OpenRegister, workdir: Path) -> Path:
    for name, query in DATASETS.items():
        started = time.time()
        download_file(register, f"{API_URL}/{name}/?{query}", workdir / f"{name}.json")
        logger.info("Health Canada %s dataset fetched in %.0fs", name, time.time() - started)
    return workdir


def _by_drug(path: Path) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in json.loads(path.read_text(encoding="utf-8")):
        grouped[record.get("drug_code")].append(record)
    return grouped


def read_dpd_records(register: OpenRegister, workdir: Path) -> Iterator[dict[str, Any]]:
    """One joined record per human drug product."""
    related = {name: _by_drug(workdir / f"{name}.json") for name in DATASETS if name != "drugproduct"}
    for product in json.loads((workdir / "drugproduct.json").read_text(encoding="utf-8")):
        if product.get("class_name") != "Human":
            continue
        code = product.get("drug_code")
        yield {"product": product, **{name: related[name].get(code, []) for name in related}}


def _join(values: Iterable[object]) -> str:
    return ", ".join(dict.fromkeys(str(value).strip() for value in values if str(value or "").strip()))


def _strength(ingredients: list[dict[str, Any]]) -> str:
    parts = []
    for item in ingredients:
        amount = str(item.get("strength") or "").strip()
        unit = str(item.get("strength_unit") or "").strip()
        if amount:
            parts.append(f"{amount} {unit}".strip())
    return "/".join(parts)


def _pack_size(packaging: list[dict[str, Any]]) -> str:
    for item in packaging:
        size = " ".join(
            str(item.get(field) or "").strip()
            for field in ("package_size", "package_size_unit", "package_type")
        ).strip()
        return size or str(item.get("product_information") or "").strip()
    return ""


def build_dpd_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Rows named and numbered as the per-product connector named them.

    Brand name and DIN are unchanged, so a row already saved from the old
    connector is updated in place when it is found again, not duplicated.
    """
    for record in records:
        schedules = [str(item.get("schedule_name") or "").strip() for item in record["schedule"]]
        if schedules and all(item == "HOMEOPATHIC" for item in schedules if item):
            continue
        product = record["product"]
        ingredients = record["activeingredient"]
        active = "; ".join(
            str(item.get("ingredient_name") or "").strip() for item in ingredients if item.get("ingredient_name")
        )
        brand = str(product.get("brand_name") or "").strip()
        if not active or not brand:
            continue
        code = product.get("drug_code")
        status = (record["status"] or [{}])[0]
        therapeutic = (record["therapeuticclass"] or [{}])[0]
        din = str(product.get("drug_identification_number") or "").strip()
        product_url = f"{API_URL}/drugproduct/?lang=en&type=json&id={code}"
        yield _match_text(active), {
            "substance": active,
            "product": brand,
            "company": str(product.get("company_name") or "").strip(),
            "country": "Canada",
            "region": "CA",
            "status": str(status.get("status") or "").strip(),
            "authorisation_scope": _join(item.title() for item in schedules if item),
            "strength": _strength(ingredients),
            "dosage_form": _join(item.get("pharmaceutical_form_name") for item in record["form"]),
            "route": _join(item.get("route_of_administration_name") for item in record["route"]),
            "pack_size": _pack_size(record["packaging"]),
            "atc_code": str(therapeutic.get("tc_atc_number") or "").strip(),
            "therapeutic_category": str(therapeutic.get("tc_atc") or "").strip().title(),
            "registration_number": din or str(code),
            "registration_date": str(status.get("original_market_date") or "").strip(),
            "source": "Health Canada",
            "source_url": product_url,
            "product_url": product_url,
            "url": product_url,
            "document_type": "",
            "last_checked": fetched_at,
        }


HEALTH_CANADA_DPD = add_register(
    OpenRegister(
        source="Health Canada",
        country="Canada",
        region="CA",
        url=f"{API_URL}/drugproduct/?lang=en&type=json",
        slug="health_canada_dpd",
        max_age_seconds=7 * 24 * 3600,
        build_rows=build_dpd_rows,
        fetch=fetch_datasets,
        read_records=read_dpd_records,
    )
)


def run_health_canada_dpd_search(substance: str) -> list[dict[str, Any]]:
    return search_register(HEALTH_CANADA_DPD, substance)
