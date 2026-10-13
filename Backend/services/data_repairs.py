"""One-off repairs for stored rows: grouping defects, and what a vendor should not see.

Combinations were keyed on their first molecule, so values were lent between a
molecule and the combinations containing it: plain metformin rows could take
Janumet's A10BD07, and a combination with no code of its own took metformin's
A10BA02. Rekeying fixes the groups; the codes and documents lent under the old
groups have to be withdrawn and lent again.

An earlier MHRA connector filed a document under the searched molecule whether
or not the document was for it: an amlodipine assessment report stored as
atorvastatin, a pioglitazone SmPC stored as metformin. Each doubtful row is
checked against the MHRA index by its licence number, and kept only if today's
connector would return it for that molecule.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import requests

from core.logging_config import get_logger
from repository import PRODUCT_DETAIL_CHILD_TABLES, get_connection, initialize_database
from services.harvest_vocabulary import row_molecule_key
from sources.mhra import (
    mhra_search_api_key,
    MHRA_SEARCH_URL,
    _active_substances,
    _normalized_tokens,
    _record_matches_substance,
    _substance_from_title,
)
from services.therapeutic_category import short_therapeutic_category
from sources.parser import clean_product_name


logger = get_logger(__name__)

MHRA_DOCUMENT_FIELDS = ("smpc_url", "pil_url", "assessment_report_url")
LICENCES_PER_REQUEST = 40
REFERENCE_FIELDS = (
    "reference_smpc_url", "reference_pil_url", "reference_source", "reference_product",
)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _licence_key(registration_number: object) -> str:
    """The form the MHRA index files a licence under: "PL 15764/0016" -> PL157640016."""
    first = _clean(registration_number).split(",")[0].split(";")[0]
    return "".join(ch for ch in first.upper() if ch.isalnum())


def fetch_licence_documents(licences: list[str]) -> list[dict[str, Any]]:
    """Every document the MHRA index holds for these licence numbers."""
    quoted = ",".join(licences)
    documents: list[dict[str, Any]] = []
    skip = 0
    api_key = mhra_search_api_key()
    if not api_key:
        raise RuntimeError("MHRA_SEARCH_API_KEY is not set")
    while True:
        response = requests.get(
            MHRA_SEARCH_URL,
            params={
                "api-key": api_key,
                "api-version": "2017-11-11",
                "$filter": f"pl_number/any(p: search.in(p, '{quoted}', ','))",
                "$select": "metadata_storage_path,substance_name,title,product_name,doc_type,pl_number",
                "$top": "1000",
                "$skip": str(skip),
            },
            timeout=60,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        page = response.json().get("value", [])
        documents.extend(page)
        if len(page) < 1000:
            return documents
        skip += len(page)


FILE_NAME_PRODUCT = re.compile(r"\.pdf\b|^(?:leaflet|spc-doc|par-doc|doc_pl)", re.IGNORECASE)


def _is_doubtful(row: dict[str, Any]) -> bool:
    """The stored product name is a file name, or does not name the molecule it is filed under."""
    if FILE_NAME_PRODUCT.search(_clean(row.get("product"))):
        return True
    wanted = set(_normalized_tokens(row.get("substance")))
    stated = set(_normalized_tokens(f"{row.get('product')} {row.get('source_substance')}"))
    return bool(wanted) and not wanted <= stated


def _delete_rows(conn: Any, ids: Iterable[int]) -> int:
    ids = list(ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ", ".join("?" for _ in chunk)
        for table in PRODUCT_DETAIL_CHILD_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE product_detail_id IN ({marks})", chunk)
        conn.execute(f"DELETE FROM product_details WHERE id IN ({marks})", chunk)
    return len(ids)


def verify_mhra_rows(
    dry_run: bool = False,
    fetch: Callable[[list[str]], list[dict[str, Any]]] = fetch_licence_documents,
) -> dict[str, Any]:
    """Keep a doubtful MHRA row only if its document is for its molecule."""
    initialize_database()
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT id, substance, source_substance, product, country,
                          registration_number, document_type, product_url, smpc_url,
                          pil_url, assessment_report_url
                   FROM product_details WHERE source = 'MHRA'"""
            ).fetchall()
        ]
    doubtful = [row for row in rows if _is_doubtful(row)]
    by_licence: dict[str, list[dict[str, Any]]] = {}
    no_licence = 0
    for row in doubtful:
        licence = _licence_key(row.get("registration_number"))
        if licence:
            by_licence.setdefault(licence, []).append(row)
        else:
            no_licence += 1

    documents: dict[str, dict[str, Any]] = {}
    licence_documents: dict[str, list[dict[str, Any]]] = {}
    unreachable: set[str] = set()
    licences = sorted(by_licence)
    for start in range(0, len(licences), LICENCES_PER_REQUEST):
        batch = licences[start : start + LICENCES_PER_REQUEST]
        try:
            for document in fetch(batch):
                url = _clean(document.get("metadata_storage_path"))
                if url:
                    documents[url] = document
                for number in document.get("pl_number") or []:
                    licence_documents.setdefault(_licence_key(number), []).append(document)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("MHRA licence lookup failed for %s licences: %s", len(batch), exc)
            unreachable.update(batch)

    names_in_use = {
        (_clean(row.get("product")).lower(), _clean(row.get("country")).lower()): row
        for row in rows
    }
    removals: list[int] = []
    updates: list[tuple[int, dict[str, str]]] = []
    reasons = {"another_molecule": 0, "licence_not_in_register": 0, "merged_into_same_product": 0}
    confirmed = 0
    relinked = 0

    for licence, licence_rows in by_licence.items():
        if licence in unreachable:
            continue
        for row in licence_rows:
            document = next(
                (
                    documents[url]
                    for url in (
                        _clean(row.get(field))
                        for field in ("product_url", *MHRA_DOCUMENT_FIELDS)
                    )
                    if url and url in documents
                ),
                None,
            )
            changes: dict[str, str] = {}
            if document is None:
                # MHRA replaces a PDF when a label is revised, so the stored
                # link can be gone while the licence is not. The licence is the
                # product: its current documents decide, and the row is pointed
                # at the current one of the same kind.
                current = licence_documents.get(licence, [])
                if not current:
                    removals.append(row["id"])
                    reasons["licence_not_in_register"] += 1
                    continue
                kind = _clean(row.get("document_type")).upper()
                document = max(
                    current,
                    key=lambda item: (
                        _record_matches_substance(item, row.get("substance")),
                        _clean(item.get("doc_type")).upper() == kind,
                    ),
                )
                if _record_matches_substance(document, row.get("substance")):
                    url = _clean(document.get("metadata_storage_path"))
                    field = {"SPC": "smpc_url", "PIL": "pil_url", "PAR": "assessment_report_url"}.get(
                        _clean(document.get("doc_type")).upper()
                    )
                    changes["product_url"] = url
                    if field:
                        changes[field] = url
                    relinked += 1
            if not _record_matches_substance(document, row.get("substance")):
                removals.append(row["id"])
                reasons["another_molecule"] += 1
                continue

            confirmed += 1
            actives = _active_substances(document) or [
                _substance_from_title(document.get("title"), row.get("substance"))
            ]
            stated = ", ".join(actives)
            # A search keeps the typed term as the substance and the registry's
            # own wording beside it; a harvest row stores the registry's wording.
            if _clean(row.get("source_substance")):
                if stated != _clean(row.get("source_substance")):
                    changes["source_substance"] = stated
            elif stated != _clean(row.get("substance")):
                changes["substance"] = stated

            product = clean_product_name(document.get("product_name") or "")
            title = _clean(document.get("title"))
            if not product and FILE_NAME_PRODUCT.search(_clean(row.get("product"))):
                # No product name anywhere, only a file name: name it by what
                # it contains and its licence, which is how MHRA lists it.
                product = (
                    clean_product_name(title) if title and not FILE_NAME_PRODUCT.search(title)
                    else f"{stated.title()} - {_clean(row.get('registration_number'))}".strip(" -")
                )
            if product and product != _clean(row.get("product")):
                key = (product.lower(), _clean(row.get("country")).lower())
                survivor = names_in_use.get(key)
                if survivor is not None and survivor["id"] != row["id"]:
                    # The save path keeps one row per product name, so a second
                    # document for the same product joins the row that has it.
                    merged = {
                        field: _clean(row.get(field))
                        for field in MHRA_DOCUMENT_FIELDS
                        if _clean(row.get(field)) and not _clean(survivor.get(field))
                    }
                    if merged:
                        survivor.update(merged)
                        updates.append((survivor["id"], merged))
                    removals.append(row["id"])
                    reasons["merged_into_same_product"] += 1
                    continue
                changes["product"] = product
                names_in_use[key] = {**row, **changes}
            if changes:
                row.update(changes)
                updates.append((row["id"], changes))

    if not dry_run:
        removed = set(removals)
        with get_connection() as conn:
            for row_id, changes in updates:
                if row_id in removed:
                    continue
                if "substance" in changes or "source_substance" in changes:
                    current = dict(
                        conn.execute(
                            "SELECT substance, source_substance FROM product_details WHERE id = ?",
                            (row_id,),
                        ).fetchone()
                    )
                    current.update(changes)
                    changes = {**changes, "substance_key": row_molecule_key(current)}
                assignments = ", ".join(f"{column} = ?" for column in changes)
                conn.execute(
                    f"UPDATE product_details SET {assignments} WHERE id = ?",
                    (*changes.values(), row_id),
                )
            _delete_rows(conn, removals)

    result = {
        "mhra_rows": len(rows),
        "doubtful": len(doubtful),
        "confirmed": confirmed,
        "relinked_to_current_document": relinked,
        "removed": len(removals),
        "removed_because": reasons,
        "rows_updated": len({row_id for row_id, _ in updates} - set(removals)),
        "left_unchecked_no_licence": no_licence,
        "left_unchecked_lookup_failed": sum(len(by_licence[l]) for l in unreachable),
        "dry_run": dry_run,
    }
    logger.info("MHRA row verification: %s", result)
    return result


def rekey_substances(dry_run: bool = False) -> dict[str, Any]:
    """Recompute every row's molecule key under the combination-aware rule."""
    initialize_database()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, substance, source_substance, substance_key FROM product_details"
        ).fetchall()
        changed = [
            (key, row["id"])
            for row in rows
            if (key := row_molecule_key(dict(row))) != (row["substance_key"] or "")
        ]
        if not dry_run:
            conn.executemany("UPDATE product_details SET substance_key = ? WHERE id = ?", changed)
    return {"rows": len(rows), "rekeyed": len(changed), "dry_run": dry_run}


# WHO ATC level-4 groups that hold only combinations. Beyond these, a level-5
# code numbered in the 50s or 70s is "<molecule>, combinations": N02BE51.
COMBINATION_ATC_GROUPS = (
    "A10BD", "B01AC3", "C03EA", "C07BB", "C07CB", "C07FB", "C08GA", "C09BA",
    "C09BB", "C09BX", "C09DA", "C09DB", "C09DX", "C10BA", "C10BX", "G03AA",
    "G03AB", "G03FA", "G03FB", "J01CR", "J01EE", "J05AR", "N02AJ", "R03AK",
    "R03AL",
)


def is_combination_atc(code: str) -> bool:
    code = _clean(code).upper()
    if code.startswith(COMBINATION_ATC_GROUPS):
        return True
    return len(code) == 7 and code[5] in "57"


def _majority(codes: Iterable[str]) -> str:
    votes = Counter(code for code in codes if code)
    return votes.most_common(1)[0][0] if votes else ""


def _looks_like_combination_product(product: object) -> bool:
    text = f" {_clean(product).lower()} "
    return any(mark in text for mark in ("/", "+", " and ", " & ", " co-", " plus ", "combination"))


def correct_group_atc_codes(dry_run: bool = False) -> dict[str, Any]:
    """Undo the ATC codes that crossed between a molecule and its combinations.

    Which codes were lent cannot be read back reliably: the evidence backfill
    marked a row's own code as lent whenever anything else on the row had been.
    So only what is provably wrong is changed, and never a code the register
    itself published:

    * a combination carrying the agreed code of one of its own molecules --
      Janumet with metformin's A10BA02 -- takes the combination's agreed code,
      or none;
    * a single-molecule product carrying the agreed code of a combination that
      contains the molecule -- metformin with A10BD07 -- takes the molecule's.

    A single molecule whose code merely differs from the majority is left
    alone: triamcinolone is D07AB09 topical and H02AB08 systemic, and both are
    right.
    """
    from services.field_completion import _record_completion_evidence, names_two_strengths

    initialize_database()
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT id, source, substance, source_substance, product, document_type,
                          product_url, source_url, atc_code, therapeutic_category
                   FROM product_details"""
            ).fetchall()
        ]
        published = {
            row[0]
            for row in conn.execute(
                """SELECT DISTINCT product_detail_id FROM evidence
                   WHERE field_name = 'atc_code' AND extraction_method = 'REGISTER_RECORD_BACKFILL'"""
            ).fetchall()
        }

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row["key"] = row_molecule_key(row)
        if row["key"]:
            groups.setdefault(row["key"], []).append(row)

    def codes(group: list[dict[str, Any]]) -> list[str]:
        return [_clean(row.get("atc_code")).upper() for row in group if _clean(row.get("atc_code"))]

    single: dict[str, str] = {}
    for key, group in groups.items():
        if "+" not in key:
            group_codes = codes(group)
            single[key] = _majority(c for c in group_codes if not is_combination_atc(c)) or _majority(group_codes)

    combination: dict[str, str] = {}
    for key, group in groups.items():
        if "+" in key:
            parts = {single.get(part, "") for part in key.split("+")} - {""}
            candidates = [c for c in codes(group) if c not in parts]
            combination[key] = _majority(c for c in candidates if is_combination_atc(c)) or _majority(candidates)

    combination_codes_of: dict[str, set[str]] = {}
    for key, code in combination.items():
        if code:
            for part in key.split("+"):
                combination_codes_of.setdefault(part, set()).add(code)

    changes: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        code = _clean(row.get("atc_code")).upper()
        key = row["key"]
        if not code or not key or row["id"] in published:
            continue
        if "+" in key:
            parts = {single.get(part, "") for part in key.split("+")} - {""}
            if code in parts and code != combination.get(key, ""):
                changes.append((row, combination.get(key, "")))
        elif code == single.get(key) and names_two_strengths(row.get("product")):
            # Found by one molecule of a combination the registry did not
            # state, and lent that molecule's code: "FENZIL 10 MG/160 MG".
            changes.append((row, ""))
        elif (
            code in combination_codes_of.get(key, set())
            and single.get(key)
            and not is_combination_atc(single[key])
            and not _looks_like_combination_product(row.get("product"))
        ):
            changes.append((row, single[key]))

    if not dry_run and changes:
        now = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            for row, code in changes:
                assignments = {"atc_code": code}
                category = short_therapeutic_category("", row.get("substance"), code) if code else ""
                if category:
                    assignments["therapeutic_category"] = category
                conn.execute(
                    "UPDATE product_details SET "
                    + ", ".join(f"{column} = ?" for column in assignments)
                    + " WHERE id = ?",
                    (*assignments.values(), row["id"]),
                )
                if code:
                    lender = (
                        "agreed code for this combination" if "+" in row["key"]
                        else "agreed code for this molecule"
                    )
                    _record_completion_evidence(
                        conn, row["id"], {**assignments, "completion_source": lender}, row, now
                    )
                else:
                    conn.execute(
                        "DELETE FROM evidence WHERE product_detail_id = ? AND field_name = 'atc_code'",
                        (row["id"],),
                    )

    combos = [(row, code) for row, code in changes if "+" in row["key"]]
    unstated = [(row, code) for row, code in changes if "+" not in row["key"] and not code]
    return {
        "combination_rows_corrected": len(combos),
        "combination_rows_left_without_code": sum(1 for _, code in combos if not code),
        "unstated_combination_rows_cleared": len(unstated),
        "single_molecule_rows_corrected": len(changes) - len(combos) - len(unstated),
        "dry_run": dry_run,
    }


def clear_misattached_references(dry_run: bool = False) -> dict[str, Any]:
    """Drop reference documents lent from a product in another molecule group.

    While they shared a group, a combination could be lent the plain molecule's
    SmPC, and the reverse. The donor is found by the document's URL; a
    reference whose donor is in the row's own group stays.
    """
    initialize_database()
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT id, substance, source_substance, smpc_url, pil_url,
                          reference_smpc_url, reference_pil_url
                   FROM product_details"""
            ).fetchall()
        ]
        owner_keys: dict[str, set[str]] = {}
        for row in rows:
            row["key"] = row_molecule_key(row)
            for field in ("smpc_url", "pil_url"):
                url = _clean(row.get(field))
                if url:
                    owner_keys.setdefault(url, set()).add(row["key"])
        stale = []
        for row in rows:
            for field in ("reference_smpc_url", "reference_pil_url"):
                url = _clean(row.get(field))
                if url and url in owner_keys and row["key"] not in owner_keys[url]:
                    stale.append(row["id"])
                    break
        if not dry_run:
            for start in range(0, len(stale), 500):
                chunk = stale[start : start + 500]
                marks = ", ".join("?" for _ in chunk)
                conn.execute(
                    "UPDATE product_details SET "
                    + ", ".join(f"{field} = ''" for field in REFERENCE_FIELDS)
                    + f" WHERE id IN ({marks})",
                    chunk,
                )
    return {"references_cleared": len(stale), "dry_run": dry_run}


def _fold(value: object) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def remove_non_pharmaceutical_rows(dry_run: bool = False) -> dict[str, Any]:
    """Remove rows a pharmaceutical buyer should never be shown.

    * rows with no regulator at all: what is left of the sample data, every one
      carrying the same invented ATC code;
    * homeopathic registrations, already left out of Italy, Brazil and the FDA
      and Health Canada registers, but stored by the connectors before them;
    * Indonesian cosmetic, food and supplement notifications, which BPOM keeps
      in the same register as medicines.
    """
    from sources.regional_live import bpom_is_non_medicine

    initialize_database()
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT id, source, substance, product, strength, dosage_form,
                          document_type, registration_number
                   FROM product_details"""
            ).fetchall()
        ]
        reasons: dict[str, list[int]] = {"no_regulator": [], "homeopathic": [], "indonesian_non_medicine": []}
        for row in rows:
            if not _clean(row.get("source")):
                reasons["no_regulator"].append(row["id"])
                continue
            text = _fold(" ".join(
                str(row.get(field) or "")
                for field in ("substance", "product", "strength", "dosage_form", "document_type")
            ))
            if "homeopath" in text or "[hp_" in text:
                reasons["homeopathic"].append(row["id"])
            elif row.get("source") == "BPOM Indonesia" and bpom_is_non_medicine(row.get("registration_number")):
                reasons["indonesian_non_medicine"].append(row["id"])
        if not dry_run:
            _delete_rows(conn, [row_id for ids in reasons.values() for row_id in ids])
    return {**{reason: len(ids) for reason, ids in reasons.items()}, "dry_run": dry_run}


DUPLICATE_IDENTITY = (
    "source", "product", "country", "registration_number", "strength", "dosage_form",
    "company", "pack_size", "status",
)


def merge_identical_rows(dry_run: bool = False) -> dict[str, Any]:
    """Merge rows a vendor cannot tell apart.

    Rows are identical when every value shown in a result row matches. The row
    with the most filled columns stays, takes any value only another copy had,
    and the copies are removed. Rows differing in pack, company or status are
    different registrations and are left alone.
    """
    initialize_database()
    with get_connection() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM product_details").fetchall()]
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in rows:
            if not _clean(row.get("product")):
                continue
            key = tuple(_clean(row.get(field)).lower() for field in DUPLICATE_IDENTITY)
            groups.setdefault(key, []).append(row)

        removals: list[int] = []
        fills = 0
        for group in groups.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda row: (-sum(1 for value in row.values() if _clean(value)), row["id"]))
            keeper, copies = group[0], group[1:]
            filled = {}
            for column in keeper:
                if column == "id" or _clean(keeper.get(column)):
                    continue
                value = next((copy[column] for copy in copies if _clean(copy.get(column))), None)
                if value is not None:
                    filled[column] = value
            if filled and not dry_run:
                conn.execute(
                    "UPDATE product_details SET "
                    + ", ".join(f"{column} = ?" for column in filled)
                    + " WHERE id = ?",
                    (*filled.values(), keeper["id"]),
                )
            fills += bool(filled)
            removals.extend(copy["id"] for copy in copies)
        if not dry_run:
            _delete_rows(conn, removals)
    return {"copies_removed": len(removals), "keepers_filled_from_copies": fills, "dry_run": dry_run}


OLD_DPD_PAGE = re.compile(
    r"https://health-products\.canada\.ca/dpd-bdpp/info\.do\?lang=en&code=(\d+)"
)
DPD_RECORD = "https://health-products.canada.ca/api/drug/drugproduct/?lang=en&type=json&id={code}"


def relink_health_canada_products(dry_run: bool = False) -> dict[str, Any]:
    """Point Canadian rows at the product record that still answers.

    Health Canada's DPD product pages now return 404, from a browser as from
    code; the API record for the same drug code does not. New rows already link
    there.
    """
    initialize_database()
    columns = ("product_url", "source_url", "evidence_url")
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id, product_url, source_url, evidence_url FROM product_details"
                " WHERE source = 'Health Canada'"
            ).fetchall()
        ]
        updated = 0
        for row in rows:
            changes = {
                column: OLD_DPD_PAGE.sub(lambda m: DPD_RECORD.format(code=m.group(1)), row[column])
                for column in columns
                if row.get(column) and OLD_DPD_PAGE.search(row[column])
            }
            if not changes:
                continue
            updated += 1
            if not dry_run:
                conn.execute(
                    "UPDATE product_details SET "
                    + ", ".join(f"{column} = ?" for column in changes)
                    + " WHERE id = ?",
                    (*changes.values(), row["id"]),
                )
        assertions = 0
        for evidence_id, url in conn.execute(
            "SELECT id, evidence_url FROM evidence WHERE evidence_url LIKE ?",
            ("https://health-products.canada.ca/dpd-bdpp/info.do%",),
        ).fetchall():
            assertions += 1
            if not dry_run:
                conn.execute(
                    "UPDATE evidence SET evidence_url = ? WHERE id = ?",
                    (OLD_DPD_PAGE.sub(lambda m: DPD_RECORD.format(code=m.group(1)), url), evidence_id),
                )
    return {"rows_relinked": updated, "assertions_relinked": assertions, "dry_run": dry_run}


def _compact(value: str) -> str:
    return re.sub(r"[\s;,]+", " ", value).strip()


MARKUP_COLUMNS = (
    "substance", "source_substance", "product", "company", "strength", "pack_size",
    "dosage_form", "manufacturer_name", "registration_number",
)


def clean_stored_markup(dry_run: bool = False) -> dict[str, Any]:
    """Remove HTML markup, placeholder values and invalid ATC codes from the store.

    Only what is not the registry's wording goes: "&amp;", "<br>", "N/A", "-",
    "Not yet assigned". Status, dosage-form language and dates keep the
    registry's own text and are presented at display time.
    """
    from services.vendor_display import clean_atc_code, clean_text

    initialize_database()
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT id, atc_code, {', '.join(MARKUP_COLUMNS)} FROM product_details"
            ).fetchall()
        ]
        per_column: Counter[str] = Counter()
        for row in rows:
            changes = {}
            for column in MARKUP_COLUMNS:
                value = row.get(column)
                if not isinstance(value, str) or not value:
                    continue
                cleaned = clean_text(value)
                # Spacing and separator punctuation alone are the registry's
                # own and not worth a write: "a;b" stays "a;b".
                if _compact(cleaned) != _compact(value):
                    changes[column] = cleaned
            if _clean(row.get("atc_code")) and not clean_atc_code(row["atc_code"]):
                changes["atc_code"] = ""
            if not changes:
                continue
            per_column.update(changes.keys())
            if not dry_run:
                conn.execute(
                    "UPDATE product_details SET "
                    + ", ".join(f"{column} = ?" for column in changes)
                    + " WHERE id = ?",
                    (*changes.values(), row["id"]),
                )
    return {"values_cleaned": dict(per_column), "dry_run": dry_run}


MHRA_DOCUMENT_HOST = "mhraproducts4853.blob.core.windows.net"
DOCUMENT_TYPE_FIELDS = {"SPC": "smpc_url", "PIL": "pil_url", "PAR": "assessment_report_url"}


def relink_dead_mhra_documents(
    dry_run: bool = False,
    fetch: Callable[[list[str]], list[dict[str, Any]]] = fetch_licence_documents,
    check: Callable[[Iterable[str]], set[str]] | None = None,
) -> dict[str, Any]:
    """Replace links to MHRA PDFs that no longer exist.

    MHRA deletes the old PDF when it revises a label, so a stored link opens
    an error page. Each dead link is replaced by the licence's current document
    of the same kind, checked to open; where the licence has none, the link is
    removed rather than left broken. Reference documents lent to other rows are
    removed when dead.
    """
    from sources.document_links import dead_links

    check = check or dead_links
    initialize_database()
    link_columns = ("product_url", *MHRA_DOCUMENT_FIELDS, "evidence_url")
    reference_columns = ("reference_smpc_url", "reference_pil_url")
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                f"""SELECT id, source, substance, registration_number, document_type,
                           {', '.join(link_columns + reference_columns)}
                    FROM product_details"""
            ).fetchall()
        ]
    urls = {
        row[column]
        for row in rows
        for column in link_columns + reference_columns
        if row.get(column) and MHRA_DOCUMENT_HOST in row[column]
    }
    dead = check(urls)

    affected = [
        row for row in rows
        if row.get("source") == "MHRA" and any(row.get(column) in dead for column in link_columns)
    ]
    licences = sorted({_licence_key(row.get("registration_number")) for row in affected} - {""})
    current: dict[str, list[dict[str, Any]]] = {}
    unreachable: set[str] = set()
    for start in range(0, len(licences), LICENCES_PER_REQUEST):
        batch = licences[start : start + LICENCES_PER_REQUEST]
        try:
            for document in fetch(batch):
                for number in document.get("pl_number") or []:
                    current.setdefault(_licence_key(number), []).append(document)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("MHRA licence lookup failed for %s licences: %s", len(batch), exc)
            unreachable.update(batch)
    candidate_urls = {
        _clean(document.get("metadata_storage_path"))
        for documents in current.values()
        for document in documents
    }
    dead |= check(candidate_urls - dead)

    replaced = removed = 0
    updates: list[tuple[int, dict[str, str]]] = []
    evidence_changes: list[tuple[str, str, int]] = []
    for row in affected:
        licence = _licence_key(row.get("registration_number"))
        if licence in unreachable:
            continue
        live = [
            document for document in current.get(licence, [])
            if _clean(document.get("metadata_storage_path")) not in dead
        ]
        live.sort(key=lambda document: not _record_matches_substance(document, row.get("substance")))
        by_type: dict[str, str] = {}
        for document in live:
            by_type.setdefault(_clean(document.get("doc_type")).upper(), _clean(document["metadata_storage_path"]))

        changes: dict[str, str] = {}
        mapping: dict[str, str] = {}
        for field in link_columns:
            old = row.get(field)
            if old not in dead:
                continue
            if field in MHRA_DOCUMENT_FIELDS:
                kind = next(k for k, f in DOCUMENT_TYPE_FIELDS.items() if f == field)
            else:
                # product_url and evidence_url point at whichever document the
                # row was built from.
                kind = next(
                    (k for k, f in DOCUMENT_TYPE_FIELDS.items() if row.get(f) == old),
                    _clean(row.get("document_type")).upper(),
                )
            new = by_type.get(kind, "")
            mapping.setdefault(old, new)
            changes[field] = mapping[old]
        if not changes:
            continue
        replaced += sum(1 for value in changes.values() if value)
        removed += sum(1 for value in changes.values() if not value)
        updates.append((row["id"], changes))
        evidence_changes.extend((new, old, row["id"]) for old, new in mapping.items())

    reference_rows = [
        row["id"] for row in rows
        if any(row.get(column) in dead for column in reference_columns)
    ]
    if not dry_run:
        with get_connection() as conn:
            for row_id, changes in updates:
                conn.execute(
                    "UPDATE product_details SET "
                    + ", ".join(f"{column} = ?" for column in changes)
                    + " WHERE id = ?",
                    (*changes.values(), row_id),
                )
            conn.executemany(
                "UPDATE evidence SET evidence_url = ? WHERE evidence_url = ? AND product_detail_id = ?",
                evidence_changes,
            )
            for start in range(0, len(reference_rows), 500):
                chunk = reference_rows[start : start + 500]
                marks = ", ".join("?" for _ in chunk)
                conn.execute(
                    "UPDATE product_details SET "
                    + ", ".join(f"{field} = ''" for field in REFERENCE_FIELDS)
                    + f" WHERE id IN ({marks})",
                    chunk,
                )
    return {
        "links_checked": len(urls),
        "dead_links": len(dead & urls),
        "rows_affected": len(updates),
        "links_replaced_with_current_document": replaced,
        "links_removed_no_current_document": removed,
        "rows_whose_dead_reference_was_removed": len(reference_rows),
        "left_unchecked_lookup_failed": sum(
            1 for row in affected if _licence_key(row.get("registration_number")) in unreachable
        ),
        "dry_run": dry_run,
    }


# Regulators whose own record carries an ATC code, and those whose own record
# carries a therapeutic category. A value on any other regulator's row came from
# somewhere else.
ATC_PUBLISHERS = frozenset({
    "EMA", "EU MRI Product Index", "Health Canada", "AIFA Italy", "BPOM Indonesia",
    "ANMDMR Romania", "Spain CIMA", "MoPH Lebanon", "SFDA Saudi Arabia",
    "EOF Greece", "HSA Singapore", "Ukraine DRLZ", "HPRA Ireland",
    "CBG Netherlands", "URPL Poland", "SUKL Czech Republic", "DKMA Denmark",
    "DMP Norway", "SUKL Slovakia", "ZVA Latvia", "TITCK Turkey",
    "ALIMS Serbia", "Malta Medicines Authority", "BDA Bulgaria", "INVIMA Colombia",
    "NDDA Kazakhstan",
})
CATEGORY_PUBLISHERS = frozenset({
    "EMA", "FDA", "Health Canada", "AIFA Italy", "ANMDMR Romania", "CDSCO India", "ANVISA Brazil",
})
DERIVED_ONLY = "derived from ATC code"
BORROWED_METHODS = ("CROSS_SOURCE_COMPLETION_BACKFILL",)


def revoke_borrowed_values(dry_run: bool = False) -> dict[str, Any]:
    """Keep only what each regulator published for its own product.

    Field completion lent a molecule's ATC code, therapeutic category and
    reference documents across regulators. The values were usually right for
    the molecule but were not that regulator's record, and nothing on screen
    said so. This withdraws them:

    * an ATC code stays only on a row from a regulator that publishes ATC codes,
      and only if completion did not lend it (a completion_source naming
      lenders, or an evidence assertion naming a lender, marks it lent);
    * a therapeutic category stays where the regulator publishes one and
      completion never rewrote it; otherwise it is the class of the row's own
      ATC code, or nothing;
    * every reference document is removed;
    * MHRA registration dates are removed: MHRA's index has no authorisation
      date, and the stored value is when the PDF was indexed.
    """
    from services.therapeutic_category import therapeutic_category_from_atc

    initialize_database()
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT id, source, atc_code, therapeutic_category, completion_source,
                          registration_date, reference_smpc_url, reference_pil_url,
                          reference_assessment_report_url, reference_source, reference_product
                   FROM product_details"""
            ).fetchall()
        ]
        lent_by_evidence = {
            row[0]
            for row in conn.execute(
                f"""SELECT DISTINCT product_detail_id FROM evidence
                    WHERE field_name = 'atc_code'
                      AND extraction_method IN ({', '.join('?' for _ in BORROWED_METHODS)})
                      AND COALESCE(source_regulator, '') NOT IN ('', ?)""",
                (*BORROWED_METHODS, DERIVED_ONLY),
            ).fetchall()
        }
        own_source_evidence = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT product_detail_id, source_regulator FROM evidence WHERE field_name = 'atc_code'"
            ).fetchall()
        }

        counts: Counter[str] = Counter()
        reference_fields = (
            "reference_smpc_url", "reference_pil_url", "reference_assessment_report_url",
            "reference_source", "reference_product",
        )
        cleared_fields: list[tuple[int, str]] = []
        for row in rows:
            source = _clean(row.get("source"))
            completion = _clean(row.get("completion_source"))
            changes: dict[str, str] = {}

            atc = _clean(row.get("atc_code"))
            lent = (completion not in ("", DERIVED_ONLY)) or (
                row["id"] in lent_by_evidence and (row["id"], source) not in own_source_evidence
            )
            own_atc = atc if atc and source in ATC_PUBLISHERS and not lent else ""
            if atc and not own_atc:
                changes["atc_code"] = ""
                counts["atc_codes_removed"] += 1
                cleared_fields.append((row["id"], "atc_code"))

            category = _clean(row.get("therapeutic_category"))
            if category:
                if source in CATEGORY_PUBLISHERS and not completion:
                    kept_category = category
                else:
                    kept_category = therapeutic_category_from_atc(own_atc)
                if kept_category != category:
                    changes["therapeutic_category"] = kept_category
                    counts["categories_removed" if not kept_category else "categories_rederived_from_own_atc"] += 1
                    cleared_fields.append((row["id"], "therapeutic_category"))

            if any(_clean(row.get(field)) for field in reference_fields):
                changes.update({field: "" for field in reference_fields})
                counts["rows_with_reference_documents_removed"] += 1

            if source == "MHRA" and _clean(row.get("registration_date")):
                changes["registration_date"] = ""
                counts["mhra_document_dates_removed"] += 1
                cleared_fields.append((row["id"], "registration_date"))

            if completion:
                changes["completion_source"] = ""
            if changes and not dry_run:
                conn.execute(
                    "UPDATE product_details SET "
                    + ", ".join(f"{column} = ?" for column in changes)
                    + " WHERE id = ?",
                    (*changes.values(), row["id"]),
                )
        if not dry_run:
            conn.executemany(
                "DELETE FROM evidence WHERE product_detail_id = ? AND field_name = ?",
                cleared_fields,
            )
    return {"rows": len(rows), **dict(counts), "dry_run": dry_run}


def restore_health_canada_from_register(
    dry_run: bool = False, register_rows: Iterable[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Give Canadian rows the ATC code Health Canada itself publishes, by DIN.

    Rows saved by the old per-product connector never carried an ATC code, so
    theirs was lent from other regulators and has been withdrawn. The Drug
    Product Database register downloaded for search holds each DIN's own code
    and ATC description; those are Health Canada's record, so they are filled
    in where the row has none, with an assertion naming the register.
    """
    import json
    import sqlite3

    from repository import EVIDENCE_INSERT_SQL
    from services.evidence_assertions import VERIFIED_RECORD

    if register_rows is None:
        from sources.health_canada_dpd import HEALTH_CANADA_DPD
        from sources.open_registers import current_index

        index = current_index(HEALTH_CANADA_DPD)
        if index is None:
            return {"skipped": "Health Canada register not downloaded yet", "dry_run": dry_run}
        connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
        try:
            register_rows = [json.loads(payload) for (payload,) in connection.execute("SELECT payload FROM rows")]
        finally:
            connection.close()

    by_din: dict[str, dict[str, Any]] = {}
    for record in register_rows:
        din = _clean(record.get("registration_number"))
        if din and _clean(record.get("atc_code")):
            by_din.setdefault(din, record)

    initialize_database()
    now = datetime.now(timezone.utc).isoformat()
    restored: Counter[str] = Counter()
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT id, registration_number, atc_code, therapeutic_category, product_url, document_type
                   FROM product_details WHERE source = 'Health Canada'"""
            ).fetchall()
        ]
        for row in rows:
            record = by_din.get(_clean(row.get("registration_number")))
            if not record:
                continue
            changes = {
                field: _clean(record.get(field))
                for field in ("atc_code", "therapeutic_category")
                if not _clean(row.get(field)) and _clean(record.get(field))
            }
            if not changes:
                continue
            restored.update(changes.keys())
            if dry_run:
                continue
            conn.execute(
                "UPDATE product_details SET "
                + ", ".join(f"{column} = ?" for column in changes)
                + " WHERE id = ?",
                (*changes.values(), row["id"]),
            )
            url = _clean(row.get("product_url")) or _clean(record.get("product_url"))
            for field, value in changes.items():
                conn.execute(
                    EVIDENCE_INSERT_SQL,
                    (
                        row["id"], None, field, value, "", "Health Canada", "Drug Product Database",
                        url, "", "Drug Product Database register", "OFFICIAL_REGISTER_FIELD",
                        now, VERIFIED_RECORD, "",
                    ),
                )
    return {
        "canadian_rows": len(rows),
        "atc_codes_restored_from_register": restored["atc_code"],
        "categories_restored_from_register": restored["therapeutic_category"],
        "dry_run": dry_run,
    }


def fold_romanian_pack_rows(dry_run: bool = False) -> dict[str, Any]:
    """Merge Romania's one-row-per-pack registrations into one row per product.

    The connector now folds packs as it reads them; this does the same for the
    rows stored before. The row for the first pack stays -- its product link is
    the one a later search matches on -- and takes every pack's description and
    number; the other pack rows are removed. No pack is lost.
    """
    from sources.romania_anmdmr import fold_pack_registrations

    initialize_database()
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM product_details WHERE source = 'ANMDMR Romania' ORDER BY id"
            ).fetchall()
        ]
        folded = fold_pack_registrations(rows)
        keepers = {row["id"]: row for row in folded}
        removals = [row["id"] for row in rows if row["id"] not in keepers]
        by_id = {row["id"]: row for row in rows}
        updated = 0
        for row_id, keeper in keepers.items():
            original = by_id[row_id]
            changes = {
                field: keeper[field]
                for field in ("registration_number", "pack_size", "smpc_url", "pil_url")
                if (keeper.get(field) or "") != (original.get(field) or "")
            }
            if not changes:
                continue
            updated += 1
            if dry_run:
                continue
            conn.execute(
                "UPDATE product_details SET "
                + ", ".join(f"{column} = ?" for column in changes)
                + " WHERE id = ?",
                (*changes.values(), row_id),
            )
            for field, value in changes.items():
                conn.execute(
                    "UPDATE evidence SET value = ? WHERE product_detail_id = ? AND field_name = ?",
                    (value, row_id, field),
                )
        if not dry_run:
            _delete_rows(conn, removals)
    return {
        "romanian_rows": len(rows),
        "products": len(folded),
        "pack_rows_merged_away": len(removals),
        "product_rows_updated": updated,
        "dry_run": dry_run,
    }


def repair_combinations_and_mhra(dry_run: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {"non_pharmaceutical": remove_non_pharmaceutical_rows(dry_run=dry_run)}
    report["markup"] = clean_stored_markup(dry_run=dry_run)
    report["mhra"] = verify_mhra_rows(dry_run=dry_run)
    report["duplicates"] = merge_identical_rows(dry_run=dry_run)
    report["health_canada_links"] = relink_health_canada_products(dry_run=dry_run)
    report["mhra_dead_links"] = relink_dead_mhra_documents(dry_run=dry_run)
    report["rekey"] = rekey_substances(dry_run=dry_run)
    # Correct ATC codes lent between molecules and combinations (805+ affected rows).
    report["atc_codes"] = correct_group_atc_codes(dry_run=dry_run)
    # Nothing is lent again afterwards: a row shows what its regulator published.
    report["borrowed_values"] = revoke_borrowed_values(dry_run=dry_run)
    report["health_canada_register"] = restore_health_canada_from_register(dry_run=dry_run)
    report["romanian_packs"] = fold_romanian_pack_rows(dry_run=dry_run)
    return report


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Clean the stored data: non-medicines, markup, MHRA rows, duplicates, links, combinations"
    )
    parser.add_argument("--dry-run", action="store_true", help="count without writing")
    parser.add_argument(
        "--mhra-links-only", action="store_true",
        help="only replace links to MHRA documents that no longer exist",
    )
    parser.add_argument(
        "--revoke-borrowed", action="store_true",
        help="only remove values lent from other regulators and MHRA document dates",
    )
    parser.add_argument(
        "--fold-romanian-packs", action="store_true",
        help="only merge Romania's one-row-per-pack registrations into one row per product",
    )
    args = parser.parse_args()
    parser_revoke = args.revoke_borrowed
    if args.mhra_links_only:
        print(json.dumps(relink_dead_mhra_documents(dry_run=args.dry_run), indent=1))
    elif args.fold_romanian_packs:
        print(json.dumps(fold_romanian_pack_rows(dry_run=args.dry_run), indent=1))
    elif parser_revoke:
        print(json.dumps({
            "borrowed_values": revoke_borrowed_values(dry_run=args.dry_run),
            "health_canada_register": restore_health_canada_from_register(dry_run=args.dry_run),
        }, indent=1))
    else:
        print(json.dumps(repair_combinations_and_mhra(dry_run=args.dry_run), indent=1))
