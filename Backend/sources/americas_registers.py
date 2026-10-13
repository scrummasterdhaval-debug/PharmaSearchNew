"""Registers regulators in the Americas publish whole.

    Colombia  INVIMA's current marketing authorisations (registros sanitarios
              vigentes) with their CUM codes, published on datos.gov.co
              (CC BY-SA 4.0): holder, manufacturers and importers by role,
              active ingredients with strengths, ATC, form, route, packs.

Indexed locally like the other registers (open_registers). The dataset is
one row per pack, per active ingredient and per role; the connector puts a
product back together from its INVIMA file number (expediente).
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, Iterator

from sources.national_registers import _clean, _indexed, _unique, national_match_text
from sources.open_registers import OpenRegister, add_register, search_register


# --------------------------------------------------------------- Colombia

COLOMBIA_DATASET = "https://www.datos.gov.co/resource/i7cb-raxc.csv"
COLOMBIA_PAGE = "https://www.datos.gov.co/Salud-y-Protecci-n-Social/C-DIGO-NICO-DE-MEDICAMENTOS-VIGENTES/i7cb-raxc"
COLOMBIA_PAGE_SIZE = 50000


def fetch_colombia(register: OpenRegister, workdir: Path) -> Path:
    """Page through the dataset's API: the bulk export stops short on a large file."""
    import requests

    from sources.open_registers import DOWNLOAD_TIMEOUT

    destination = workdir / "invima_cum.csv"
    offset = 0
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = None
        while True:
            response = requests.get(
                COLOMBIA_DATASET,
                params={"$limit": COLOMBIA_PAGE_SIZE, "$offset": offset, "$order": ":id"},
                timeout=DOWNLOAD_TIMEOUT,
                headers={"User-Agent": "PharmaSearch/1.0 (regulatory register download)"},
            )
            response.raise_for_status()
            rows = list(csv.DictReader(response.text.splitlines()))
            if not rows:
                break
            if writer is None:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
            writer.writerows(rows)
            if len(rows) < COLOMBIA_PAGE_SIZE:
                break
            offset += COLOMBIA_PAGE_SIZE
    return destination


def colombian_date(value: object) -> str:
    """datos.gov.co writes dates month first: "08/23/2012"."""
    from datetime import datetime

    text = _clean(value)[:10]
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return text


def read_colombia(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    """One record per INVIMA file: its ingredients, roles and packs gathered together."""
    products: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            number = _clean(row.get("expediente"))
            if not number:
                continue
            product = products.get(number)
            if product is None:
                product = products[number] = {
                    **row, "ingredients": {}, "manufacturers": [], "importers": [], "packs": [], "active_packs": 0,
                }
            ingredient = _clean(row.get("principioactivo"))
            if ingredient:
                amount = " ".join(
                    part for part in (_clean(row.get("cantidad")), _clean(row.get("unidadmedida"))) if part
                )
                reference = _clean(row.get("unidadreferencia"))
                product["ingredients"].setdefault(ingredient, f"{amount}/{reference}" if amount and reference else amount)
            role = _clean(row.get("tiporol")).upper()
            name = _clean(row.get("nombrerol"))
            if name and role == "FABRICANTE":
                product["manufacturers"].append(name)
            elif name and role == "IMPORTADOR":
                product["importers"].append(name)
            pack = _clean(row.get("descripcioncomercial"))
            if pack and pack not in product["packs"]:
                product["packs"].append(pack)
                if _clean(row.get("estadocum")) == "Activo":
                    product["active_packs"] += 1
    yield from products.values()


def build_colombia_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        ingredients = record["ingredients"]
        active = "; ".join(name.capitalize() for name in ingredients)
        product = _clean(record.get("producto"))
        if not (active or product):
            continue
        strengths = [amount for amount in ingredients.values() if amount]
        makers = _unique(record["manufacturers"])
        status = _clean(record.get("estadoregistro")) or "Vigente"
        yield _indexed(national_match_text(" ".join(ingredients) or product), {
            "substance": active,
            "active_substance": active,
            "source_substance": "; ".join(ingredients),
            "product": product,
            "company": _clean(record.get("titular")),
            "country": "Colombia",
            "region": "LA",
            "status": "Current" if status == "Vigente" else status,
            "authorisation_scope": _clean(record.get("modalidad")).capitalize(),
            "strength": " / ".join(strengths),
            "dosage_form": _clean(record.get("formafarmaceutica")).capitalize(),
            "route": _clean(record.get("viaadministracion")).capitalize(),
            "pack_size": _unique(record["packs"]),
            "atc_code": _clean(record.get("atc")),
            "registration_number": _clean(record.get("registrosanitario")),
            "registration_date": colombian_date(record.get("fechaexpedicion")),
            "expiry_date": colombian_date(record.get("fechavencimiento")),
            "manufacturer_name": makers,
            "manufacturer_source": "INVIMA register (role: manufacturer)" if makers else "",
            "source": "INVIMA Colombia",
            "source_url": COLOMBIA_PAGE,
            "product_url": "",
            "document_type": "INVIMA current marketing authorisation (CUM)"
            + (f"; importer: {_unique(record['importers'])}" if record["importers"] else ""),
            "last_checked": fetched_at,
        })


INVIMA_COLOMBIA = add_register(OpenRegister(
    source="INVIMA Colombia",
    country="Colombia",
    region="LA",
    url=COLOMBIA_DATASET,
    slug="invima_colombia",
    max_age_seconds=7 * 24 * 3600,
    build_rows=build_colombia_rows,
    fetch=fetch_colombia,
    read_records=read_colombia,
))


def run_invima_colombia_search(substance: str) -> list[dict[str, Any]]:
    return search_register(INVIMA_COLOMBIA, substance)
