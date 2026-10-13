"""The FDA's National Drug Code directory, searched from a local copy.

The label connector asked openFDA for the first 100 labels of a molecule and
stopped there -- 100 of acetaminophen's 3,276, 100 of ibuprofen's 1,184. It
could not simply ask for more: every label carries the whole prescribing
text, so metformin's 396 alone come to 54 MB.

The NDC directory lists every finished product the FDA has on file, with what
a regulatory search needs and none of the prose. openFDA publishes all of it as
one download (~28 MB zipped, 247 MB of JSON, refreshed weekly):

    https://download.open.fda.gov/drug/ndc/drug-ndc-0001-of-0001.json.zip

It is streamed out of the ZIP one record at a time -- all 137,841 in about six
seconds and a few megabytes of memory -- and indexed by open_registers.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote

from sources.open_registers import OpenRegister, _match_text, add_register, search_register


DOWNLOAD_URL = "https://download.open.fda.gov/drug/ndc/drug-ndc-0001-of-0001.json.zip"
DAILYMED_LABEL_URL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm"
OPENFDA_NDC_URL = "https://api.fda.gov/drug/ndc.json"

# Finished products a person is given. Bulk ingredients, material "for further
# processing" and animal products are listed too, but they are not medicines on
# the US market.
HUMAN_PRODUCT_TYPES = {
    "HUMAN PRESCRIPTION DRUG",
    "HUMAN OTC DRUG",
    "VACCINE",
    "PLASMA DERIVATIVE",
    "CELLULAR THERAPY",
    "STANDARDIZED ALLERGENIC",
    "NON-STANDARDIZED ALLERGENIC",
}

# The route to market, which for a generic search is the question that matters:
# an ANDA is an approved generic, an NDA the reference product.
MARKETING_CATEGORY = {
    "ANDA": "ANDA (generic)",
    "NDA": "NDA",
    "NDA AUTHORIZED GENERIC": "NDA authorized generic",
    "BLA": "BLA",
    "OTC MONOGRAPH DRUG": "OTC monograph",
    "OTC MONOGRAPH FINAL": "OTC monograph",
    "OTC MONOGRAPH NOT FINAL": "OTC monograph",
    "UNAPPROVED DRUG OTHER": "Unapproved drug",
    "UNAPPROVED MEDICAL GAS": "Unapproved medical gas",
    "EMERGENCY USE AUTHORIZATION": "Emergency use authorization",
}


def stream_results(zip_path: Path, chunk_size: int = 1 << 20) -> Iterator[dict[str, Any]]:
    """Yield each object of the file's top-level "results" array in turn.

    The file is one JSON document a quarter of a gigabyte long. Loading it
    whole costs several gigabytes of memory, and no streaming JSON parser is
    installed, so the array is decoded an element at a time from a sliding
    buffer instead.
    """
    decoder = json.JSONDecoder()
    with zipfile.ZipFile(zip_path) as archive, archive.open(archive.infolist()[0]) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8")
        buffer = ""
        while True:
            marker = buffer.find('"results"')
            if marker != -1:
                start = buffer.find("[", marker)
                if start != -1:
                    buffer = buffer[start + 1:]
                    break
            more = text.read(chunk_size)
            if not more:
                return
            # Keep a tail, so a marker split across two reads is still found.
            buffer = (buffer[-16:] if marker == -1 else buffer) + more

        position = 0
        exhausted = False
        while True:
            while True:
                while position < len(buffer) and buffer[position] in " \r\n\t,":
                    position += 1
                if position < len(buffer) and buffer[position] == "]":
                    return
                try:
                    record, end = decoder.raw_decode(buffer, position)
                    break
                except json.JSONDecodeError:
                    if exhausted:
                        return
                    more = text.read(chunk_size)
                    exhausted = not more
                    buffer = buffer[position:] + more
                    position = 0
            yield record
            position = end
            if position > chunk_size * 4:
                buffer = buffer[position:]
                position = 0


def read_ndc_records(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    yield from stream_results(path)


def _iso(value: object) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _strength(ingredients: list[dict[str, Any]]) -> str:
    """"500 mg/1" is FDA for 500 mg per unit; the "/1" says nothing to a reader."""
    parts = [re.sub(r"/1$", "", str(item.get("strength") or "").strip()) for item in ingredients]
    return "/".join(part for part in parts if part)


def _therapeutic_category(pharm_classes: list[str]) -> str:
    """The established pharmacologic classes, e.g. "Biguanide [EPC]" -> Biguanide."""
    established = [
        re.sub(r"\s*\[EPC\]$", "", item).strip()
        for item in pharm_classes or []
        if item.endswith("[EPC]")
    ]
    return "; ".join(dict.fromkeys(established))


def _status(end_date: str) -> str:
    if not end_date:
        return "Marketed"
    try:
        ends = date.fromisoformat(end_date)
    except ValueError:
        return "Marketed"
    return "Marketing ended" if ends < date.today() else f"Marketed (ends {end_date})"


def _pack_sizes(packaging: list[dict[str, Any]]) -> str:
    """The packs as the FDA describes them, without the NDC codes they repeat."""
    descriptions = [
        re.sub(r"\s*\(\d{4,5}-\d{3,4}-\d{1,2}\)", "", str(item.get("description") or "")).strip()
        for item in packaging or []
    ]
    descriptions = [item for item in dict.fromkeys(descriptions) if item]
    return "; ".join(descriptions[:6]) + ("; …" if len(descriptions) > 6 else "")


def build_fda_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """One row per product listing, labelled by its NDC.

    A repackager relabelling someone else's approved product lists it under
    its own NDC and the same application number. Keyed on name and application
    alone the two would merge on screen and overwrite each other when saved,
    so the NDC is part of the product name.
    """
    for record in records:
        if not record.get("finished"):
            continue
        if str(record.get("product_type") or "") not in HUMAN_PRODUCT_TYPES:
            continue
        category = str(record.get("marketing_category") or "")
        if category == "UNAPPROVED HOMEOPATHIC":
            continue
        ingredients = record.get("active_ingredients") or []
        active = "; ".join(str(item.get("name") or "").strip() for item in ingredients if item.get("name"))
        if not active:
            continue
        product_ndc = str(record.get("product_ndc") or "").strip()
        brand = str(record.get("brand_name") or record.get("generic_name") or "").strip()
        strength = _strength(ingredients)
        name = " ".join(part for part in (brand, strength) if part)
        product = f"{name} (NDC {product_ndc})" if product_ndc else name
        openfda = record.get("openfda") or {}
        set_ids = openfda.get("spl_set_id") or []
        dailymed = f"{DAILYMED_LABEL_URL}?setid={set_ids[0]}" if set_ids else ""
        application = str(record.get("application_number") or "").strip()
        end_date = _iso(record.get("marketing_end_date"))
        yield _match_text(active), {
            "substance": active,
            "product": product,
            "company": str(record.get("labeler_name") or "").strip(),
            "labeler_name": str(record.get("labeler_name") or "").strip(),
            "country": "United States",
            "region": "US",
            "status": _status(end_date),
            "authorisation_scope": MARKETING_CATEGORY.get(category, category.title()),
            "strength": strength,
            "dosage_form": str(record.get("dosage_form") or "").title(),
            "route": ", ".join(item.title() for item in record.get("route") or []),
            "pack_size": _pack_sizes(record.get("packaging") or []),
            "therapeutic_category": _therapeutic_category(record.get("pharm_class") or []),
            "registration_number": application or product_ndc,
            "registration_date": _iso(record.get("marketing_start_date")),
            "expiry_date": _iso(record.get("listing_expiration_date")),
            "source": "FDA",
            "source_url": DOWNLOAD_URL,
            "product_url": dailymed or f'{OPENFDA_NDC_URL}?search=product_ndc:"{quote(product_ndc)}"',
            # DailyMed's Prescribing Information is the US counterpart of an
            # SmPC; the set id in the directory links it without another request.
            "smpc_url": dailymed,
            "document_type": "FDA prescribing information" if dailymed else "",
            "last_checked": fetched_at,
        }


FDA_NDC = add_register(
    OpenRegister(
        source="FDA",
        country="United States",
        region="US",
        url=DOWNLOAD_URL,
        slug="fda_ndc",
        max_age_seconds=7 * 24 * 3600,
        build_rows=build_fda_rows,
        read_records=read_ndc_records,
        # Acetaminophen alone has 3,498 listings; the bound is a safeguard, and
        # a search that reaches it says so on the page.
        max_results=5000,
    )
)


def run_fda_ndc_search(substance: str) -> list[dict[str, Any]]:
    return search_register(FDA_NDC, substance)
