"""Fills blank columns from what other registries published about the molecule.

A registry answers only for its own market, so a row is missing fields that a
different regulator states plainly about the same molecule. CDSCO never gives
an ATC code; EMA always does. Completion closes that gap by grouping every
harvested row by molecule and lending values across the group.

What may be lent depends on what the field describes:

* ATC code and therapeutic category are properties of the *molecule*. Metformin
  is A10BA02 in every country, so lending it is stating a fact, not inventing
  one, and it is written into the row's own column.
* SmPC, PIL and assessment reports are properties of a *specific marketing
  authorisation*. A Spanish product's SmPC is not the Indian product's label,
  and writing it into smpc_url would present another country's document as this
  product's own. Those are attached as clearly separate reference_* fields, with
  the regulator that issued them recorded alongside.

Every lent value records where it came from, so nothing inherited is ever
mistaken for something the local regulator published.
"""

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from core.logging_config import get_logger
from repository import EVIDENCE_INSERT_SQL, get_connection, initialize_database
from services.evidence_assertions import (
    COMPLETABLE_FIELDS,
    COMPLETION_METHOD,
    COMPLETION_SECTION,
    UNVERIFIED,
    register_url,
)
from services.field_availability import DOCUMENT_CAPABLE_SOURCES
from services.harvest_vocabulary import row_molecule_key
from services.therapeutic_category import short_therapeutic_category


logger = get_logger(__name__)

# Molecule-level facts, safe to write into the row's own column. The ATC code
# is the only one that can be lent verbatim: it is a code, so it means the same
# thing whichever registry states it.
#
# Therapeutic category is deliberately not lent. Registries write it in their
# own language and register -- Romania stores the ATC class in Romanian, the
# FDA stores label prose -- so a majority vote across a molecule spreads
# "Hipocolesterolemiante Si Hipotrigliceridemiante" or "Calpol products are
# indicated for..." over every other country's rows. It is derived from the
# agreed ATC code instead, which yields the same fact in one language.
MOLECULE_FIELDS = ("atc_code",)

# Authorisation-level documents, attached only as a molecule reference.
DOCUMENT_FIELDS = ("smpc_url", "pil_url", "assessment_report_url")

# Of those, the ones worth lending to another regulator's product.
#
# An assessment report is not lent. The SmPC and the PIL describe the medicine
# -- its composition, indications, dosing and warnings -- which is largely the
# same medicine wherever it is sold. An assessment report is the regulator's
# evaluation of one specific licence application, headed with the procedure and
# licence numbers that identify it: "Procedure No: UK/H/4817/001/DC, UK Licence
# No: PL 17509/0061". It documents a decision about a UK authorisation and says
# nothing about a US product, so an empty column is the honest answer.
LENDABLE_DOCUMENT_FIELDS = ("smpc_url", "pil_url")


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


STRENGTH_CHAIN_PATTERN = re.compile(
    r"\d[\d.,]*\s*(?:mg|g|mcg|microgram)s?(?:\s*/\s*\d[\d.,]*\s*(?:mg|g|mcg|microgram)s?)+(?![a-z/])",
    re.IGNORECASE,
)


def names_two_strengths(product: object) -> bool:
    """A product named with a pair of strengths is a combination.

    Spain and several other registries return the searched molecule as the
    substance, so a combination found by one of its molecules looks single, and
    would be lent that molecule's ATC code. "10 mg/160 mg" is a pair; a
    concentration such as "250 mg/5 ml" is not, and neither is a list of one
    molecule's strengths, "10 mg/20 mg/40 mg".
    """
    return any(
        chain.count("/") == 1 for chain in STRENGTH_CHAIN_PATTERN.findall(_clean(product))
    )


def _molecule_key(row: dict[str, Any]) -> str:
    return row_molecule_key(row)


def _agreed_value(rows: list[dict[str, Any]], field: str) -> tuple[str, str]:
    """The value most registries state for a molecule-level field.

    A majority is used rather than the first value found: a single row with a
    mis-parsed ATC code should not be lent to the whole molecule.
    """
    votes: Counter[str] = Counter()
    sources: dict[str, str] = {}
    for row in rows:
        value = _clean(row.get(field))
        if not value:
            continue
        votes[value] += 1
        sources.setdefault(value, _clean(row.get("source")))
    if not votes:
        return "", ""
    value, _count = votes.most_common(1)[0]
    return value, sources.get(value, "")


def _dosage_form_key(value: object) -> str:
    """A dosage form reduced to what distinguishes it from another form."""
    text = _clean(value).lower()
    for form in (
        "injection", "infusion", "spray", "cream", "ointment", "gel", "drops",
        "inhaler", "inhalation", "capsule", "tablet", "solution", "suspension",
        "syrup", "patch", "suppository", "powder", "lotion", "paste",
    ):
        if form in text:
            return form
    return text


# Esters and salts that make a genuinely different medicine of the same
# molecule: triamcinolone acetonide is Kenalog, hexacetonide is Aristospan, and
# they are not interchangeable.
SALT_QUALIFIERS = (
    "hexacetonide", "acetonide", "diacetate", "dipropionate", "propionate",
    "valerate", "furoate", "decanoate", "enanthate", "palmitate", "pamoate",
    "succinate", "fumarate", "maleate", "besylate", "mesylate", "tartrate",
)


def _salt_key(row: dict[str, Any]) -> str:
    """The salt or ester a row names, wherever it happens to state it.

    Registries put it in different places: the MHRA writes it into the product
    name, openFDA states it in substance_name while the product is a brand.
    """
    text = " ".join(
        _clean(row.get(field)).lower()
        for field in ("source_substance", "substance", "product")
    )
    for qualifier in SALT_QUALIFIERS:
        if qualifier in text:
            return qualifier
    return ""


def _document_donor(
    rows: list[dict[str, Any]], target: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """The row whose documents best represent this product.

    A molecule is not a product. Triamcinolone is a nasal spray in one country
    and an injectable suspension in another, and lending the injection's
    assessment report to the spray describes a different medicine. So a donor
    in the same dosage form outranks everything else.

    After that, regulators publishing full document sets are preferred, and
    among them the row carrying the most documents wins, so a reference points
    at a complete label rather than a stray PIL.
    """
    candidates = [
        row
        for row in rows
        if any(_clean(row.get(field)) for field in DOCUMENT_FIELDS)
    ]
    if not candidates:
        return None
    wanted_form = _dosage_form_key(target.get("dosage_form")) if target else ""
    wanted_salt = _salt_key(target) if target else ""
    return max(
        candidates,
        key=lambda row: (
            # The salt outranks the form: an acetonide injection and a
            # hexacetonide injection are different medicines, not two
            # presentations of one.
            bool(wanted_salt) and _salt_key(row) == wanted_salt,
            bool(wanted_form) and _dosage_form_key(row.get("dosage_form")) == wanted_form,
            _clean(row.get("source")) in DOCUMENT_CAPABLE_SOURCES,
            sum(1 for field in DOCUMENT_FIELDS if _clean(row.get(field))),
        ),
    )


def _completions_for_group(rows: list[dict[str, Any]]) -> list[tuple[int, dict[str, str]]]:
    """The column updates each row in a molecule group needs."""
    agreed = {field: _agreed_value(rows, field) for field in MOLECULE_FIELDS}
    updates: list[tuple[int, dict[str, str]]] = []

    for row in rows:
        # Chosen per row, since the best donor depends on the dosage form of
        # the product being completed.
        donor = _document_donor(rows, row)
        changes: dict[str, str] = {}
        lenders = []

        for field in MOLECULE_FIELDS:
            value, lender = agreed[field]
            if value and not _clean(row.get(field)) and not names_two_strengths(row.get("product")):
                changes[field] = value
                if lender and lender not in lenders:
                    lenders.append(lender)

        # Derived from whichever code the row will end up carrying, so a row
        # that just inherited an ATC code gets the category that goes with it.
        atc_code = changes.get("atc_code") or _clean(row.get("atc_code"))
        category = short_therapeutic_category("", row.get("substance"), atc_code)
        if category and category != _clean(row.get("therapeutic_category")):
            changes["therapeutic_category"] = category

        if changes:
            changes["completion_source"] = ", ".join(lenders) or "derived from ATC code"

        if donor is not None and donor.get("id") != row.get("id"):
            reference_changes = {}
            for field in LENDABLE_DOCUMENT_FIELDS:
                value = _clean(donor.get(field))
                # Only offer a reference where the row has no document of its own.
                if value and not _clean(row.get(field)):
                    reference_changes[f"reference_{field}"] = value
            if reference_changes:
                changes.update(reference_changes)
                changes["reference_source"] = _clean(donor.get("source"))
                changes["reference_product"] = _clean(donor.get("product"))

        if changes:
            updates.append((int(row["id"]), changes))

    return updates


def attach_reference_documents(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lend documents to result rows without writing to the database.

    A live search builds its rows from the connectors, so they have never been
    through completion and show no documents at all -- even where the database
    already holds a document for that very molecule. Displaying them means
    lending in memory, against the stored rows, at the moment of rendering.
    """
    wanted = {
        row_molecule_key(row)
        for row in rows
        if not any(_clean(row.get(field)) for field in LENDABLE_DOCUMENT_FIELDS)
        and not any(_clean(row.get(f"reference_{field}")) for field in LENDABLE_DOCUMENT_FIELDS)
    }
    wanted.discard("")
    if not wanted:
        return rows

    placeholders = ", ".join("?" for _ in wanted)
    with get_connection() as connection:
        candidates = [
            dict(candidate)
            for candidate in connection.execute(
                f"""
                SELECT substance_key, source, product, dosage_form, source_substance,
                       smpc_url, pil_url, assessment_report_url
                FROM product_details
                WHERE substance_key IN ({placeholders})
                  AND (smpc_url <> '' OR pil_url <> '' OR assessment_report_url <> '')
                """,
                tuple(wanted),
            ).fetchall()
        ]

    by_key: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_key.setdefault(str(candidate.get("substance_key") or ""), []).append(candidate)

    for row in rows:
        key = row_molecule_key(row)
        # Per row, so a spray is not handed the injection's assessment report.
        donor = _document_donor(by_key.get(key, []), row)
        if not donor:
            continue
        for field in LENDABLE_DOCUMENT_FIELDS:
            value = _clean(donor.get(field))
            if value and not _clean(row.get(field)) and not _clean(row.get(f"reference_{field}")):
                row[f"reference_{field}"] = value
                row["reference_source"] = _clean(donor.get("source"))
                row["reference_product"] = _clean(donor.get("product"))
    return rows


def _load_rows() -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute("SELECT * FROM product_details").fetchall()
    return [dict(row) for row in rows]


def _record_completion_evidence(
    connection: Any, row_id: int, changes: dict[str, str], row: dict[str, Any], now: str
) -> None:
    """State that a lent value rests on the lenders, not on this register.

    The row may already carry an assertion naming its own regulator, from when
    the value was saved or backfilled. Where completion replaces the value that
    assertion no longer describes the column, so it is dropped rather than left
    to contradict the new one in the export.
    """
    url = register_url(row)
    if not url:
        return
    lenders = changes.get("completion_source", "")
    for field in COMPLETABLE_FIELDS:
        if field not in changes:
            continue
        connection.execute(
            "DELETE FROM evidence WHERE product_detail_id = ? AND field_name = ?",
            (row_id, field),
        )
        connection.execute(
            EVIDENCE_INSERT_SQL,
            (
                row_id, None, field, changes[field], "",
                lenders or str(row.get("source") or ""),
                str(row.get("document_type") or ""), url, "",
                COMPLETION_SECTION, COMPLETION_METHOD, now, UNVERIFIED, "",
            ),
        )


def _apply(
    updates: list[tuple[int, dict[str, str]]],
    rows_by_id: dict[int, dict[str, Any]] | None = None,
) -> int:
    applied = 0
    now = datetime.now(timezone.utc).isoformat()
    rows_by_id = rows_by_id or {}
    with get_connection() as connection:
        for row_id, changes in updates:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            connection.execute(
                f"UPDATE product_details SET {assignments} WHERE id = ?",
                (*changes.values(), row_id),
            )
            row = rows_by_id.get(row_id)
            if row is not None:
                _record_completion_evidence(connection, row_id, changes, row, now)
            applied += 1
    return applied


def complete_fields(dry_run: bool = False) -> dict[str, Any]:
    """Lend molecule-level values across every harvested row.

    Returns what was filled, per field, so the gain is measured rather than
    assumed.
    """
    initialize_database()
    rows = _load_rows()

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = _molecule_key(row)
        if key:
            groups.setdefault(key, []).append(row)

    updates: list[tuple[int, dict[str, str]]] = []
    for group in groups.values():
        # A molecule seen once has nobody to borrow from.
        if len(group) > 1:
            updates.extend(_completions_for_group(group))

    filled: Counter[str] = Counter()
    for _row_id, changes in updates:
        for column in changes:
            if not column.endswith("_source") and column != "reference_product":
                filled[column] += 1

    applied = 0 if dry_run else _apply(
        updates, {int(row["id"]): row for row in rows if row.get("id")}
    )
    logger.info(
        "Field completion %s %s rows across %s molecules",
        "would update" if dry_run else "updated",
        len(updates),
        len(groups),
    )
    return {
        "molecules": len(groups),
        "rows_examined": len(rows),
        "rows_updated": len(updates),
        "rows_written": applied,
        "filled": dict(filled.most_common()),
        "dry_run": dry_run,
    }


def clear_lent_assessment_reports() -> int:
    """Drop assessment reports lent by an earlier pass.

    They were lent before it was clear that an assessment report documents a
    decision about one licence rather than describing the medicine. A row's own
    assessment report is untouched.
    """
    initialize_database()
    with get_connection() as connection:
        cursor = connection.execute(
            "UPDATE product_details SET reference_assessment_report_url = ''"
            " WHERE reference_assessment_report_url <> ''"
        )
        return cursor.rowcount


def repair_lent_categories() -> dict[str, Any]:
    """Re-derive therapeutic category on every row completion has touched.

    An earlier version of this pass lent the category verbatim, which spread one
    registry's wording -- in its own language -- across every other country's
    rows for that molecule. Only rows completion wrote are repaired: a category
    a regulator published for its own product is that regulator's text and stays
    exactly as it published it.
    """
    initialize_database()
    with get_connection() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT id, substance, atc_code, therapeutic_category FROM product_details"
                " WHERE completion_source <> ''"
            ).fetchall()
        ]

    repaired = 0
    cleared = 0
    with get_connection() as connection:
        for row in rows:
            category = short_therapeutic_category("", row.get("substance"), row.get("atc_code"))
            if category == _clean(row.get("therapeutic_category")):
                continue
            connection.execute(
                "UPDATE product_details SET therapeutic_category = ? WHERE id = ?",
                (category, row["id"]),
            )
            repaired += 1
            if not category:
                cleared += 1

    logger.info("Repaired the therapeutic category on %s completed rows", repaired)
    return {"rows_examined": len(rows), "rows_repaired": repaired, "rows_cleared": cleared}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Fill blank fields from other registries")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be filled")
    parser.add_argument(
        "--repair-categories",
        action="store_true",
        help="Re-derive the therapeutic category on rows completion has written",
    )
    args = parser.parse_args()
    if args.repair_categories:
        print(json.dumps(repair_lent_categories(), indent=1))
    else:
        print(json.dumps(complete_fields(dry_run=args.dry_run), indent=1))
