"""Drugs@FDA: every US application and who holds it, from a local copy.

The NDC directory (fda_ndc.py) lists what is on the market now, under the
labeler's name. A dossier buyer also needs what was approved and by whom,
including products since discontinued: Liptruzet and Roszet, the two US
atorvastatin and rosuvastatin combinations with ezetimibe, are both
discontinued and absent from the NDC directory.

Drugs@FDA has it: every NDA, ANDA and BLA with its holder, each product's
strength, form, marketing status and therapeutic-equivalence code, and the
approval history. openFDA publishes it whole (~9 MB zipped, weekly):

    https://download.open.fda.gov/drug/drugsfda/drug-drugsfda-0001-of-0001.json.zip

fda.gov's own Orange Book download turns away scripted clients; this is the
same agency's published copy of the same approvals.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Iterator

from sources.fda_ndc import _iso, stream_results
from sources.open_registers import OpenRegister, _match_text, add_register, search_register


DOWNLOAD_URL = "https://download.open.fda.gov/drug/drugsfda/drug-drugsfda-0001-of-0001.json.zip"
OVERVIEW_URL = "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={}"

STATUS = {
    "Prescription": "Marketed (prescription)",
    "Over-the-counter": "Marketed (OTC)",
    "Discontinued": "Discontinued",
    "None (Tentative Approval)": "Tentative approval",
}
APPLICATION_TYPES = {"NDA": "NDA", "ANDA": "ANDA (generic)", "BLA": "BLA"}


def read_records(register: OpenRegister, path: Path) -> Iterator[dict[str, Any]]:
    yield from stream_results(path)


def _approval_date(record: dict[str, Any]) -> str:
    """The original application's approval, the date the licence was granted."""
    dates = [
        submission.get("submission_status_date", "")
        for submission in record.get("submissions") or []
        if submission.get("submission_type") == "ORIG" and submission.get("submission_status") in ("AP", "TA")
    ]
    return _iso(min(dates)) if dates else ""


def build_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        number = str(record.get("application_number") or "")
        kind = next((prefix for prefix in ("ANDA", "NDA", "BLA") if number.startswith(prefix)), "")
        digits = number[len(kind):]
        approved = _approval_date(record)
        for product in record.get("products") or []:
            ingredients = product.get("active_ingredients") or []
            names = "; ".join(str(item.get("name") or "").strip() for item in ingredients if item.get("name"))
            if not names:
                continue
            # "EQ 10MG BASE/10MG **Federal Register determination that ...**":
            # the note is about the discontinuation, not the strength.
            strength = "/".join(
                re.sub(r"\*\*.*?\*\*", "", str(item.get("strength") or "")).strip()
                for item in ingredients
                if item.get("strength")
            )
            brand = str(product.get("brand_name") or "").strip()
            status = str(product.get("marketing_status") or "").strip()
            yield _match_text(names), {
                "substance": names,
                "active_substance": names,
                "source_substance": names,
                "product": " ".join(part for part in (brand, strength) if part),
                "company": str(record.get("sponsor_name") or "").strip(),
                "country": "United States",
                "region": "US",
                "status": STATUS.get(status, status),
                "authorisation_scope": APPLICATION_TYPES.get(kind, kind),
                "strength": strength,
                "dosage_form": str(product.get("dosage_form") or "").title(),
                "route": str(product.get("route") or "").title(),
                "registration_number": f"{number} product {product.get('product_number', '')}".strip(),
                "application_number": number,
                "registration_date": approved,
                "te_code": str(product.get("te_code") or ""),
                "reference_drug": str(product.get("reference_drug") or ""),
                "source": "Drugs@FDA",
                "source_url": DOWNLOAD_URL,
                "product_url": OVERVIEW_URL.format(digits) if digits else "",
                "document_type": "Drugs@FDA application",
                "last_checked": fetched_at,
            }


DRUGS_AT_FDA = add_register(
    OpenRegister(
        source="Drugs@FDA",
        country="United States",
        region="US",
        url=DOWNLOAD_URL,
        slug="fda_drugsfda",
        max_age_seconds=7 * 24 * 3600,
        build_rows=build_rows,
        read_records=read_records,
    )
)


def run_drugs_at_fda_search(substance: str) -> list[dict[str, Any]]:
    return search_register(DRUGS_AT_FDA, substance)
