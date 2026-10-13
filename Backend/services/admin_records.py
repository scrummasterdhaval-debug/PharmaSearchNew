"""Hand corrections to a stored registration, recorded rather than silent.

A connector's value is a claim with a source behind it. When an operator
overrides one, that override is itself a claim, so it is written as a manual
evidence assertion alongside the value and kept in an edit log that says who
changed what, from what, and when. Nothing here overwrites a field without
leaving that trail.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.logging_config import get_logger
from repository import get_connection, initialize_database


logger = get_logger(__name__)

MANUAL_METHOD = "MANUAL_ADMIN_ENTRY"
MANUAL_STATUS = "VERIFIED_MANUAL_ENTRY"
MANUAL_SECTION = "Manual admin entry"

# Only these may be edited. Identity and derived columns (id, substance_key,
# last_checked) are deliberately absent: they are computed, not asserted.
EDITABLE_FIELDS: list[tuple[str, str]] = [
    ("product", "Product name"),
    ("substance", "Active substance"),
    ("company", "MA holder"),
    ("applicant_sponsor", "Applicant / sponsor"),
    ("country", "Country"),
    ("region", "Region"),
    ("authorisation_scope", "Authorisation scope"),
    ("status", "Registration status"),
    ("registration_number", "Registration number"),
    ("registration_date", "Registration date"),
    ("expiry_date", "Expiry date"),
    ("strength", "Strength"),
    ("dosage_form", "Dosage form"),
    ("pack_size", "Pack size"),
    ("route", "Route"),
    ("atc_code", "ATC code"),
    ("manufacturer_name", "Manufacturer name"),
    ("manufacturer_country", "Manufacturer country"),
    ("manufacturer_address", "Manufacturer address / site"),
    ("manufacturer_role", "Manufacturer role"),
    ("batch_release_manufacturer", "Batch release manufacturer"),
    ("smpc_url", "SmPC URL"),
    ("pil_url", "PIL URL"),
    ("assessment_report_url", "Assessment report URL"),
    ("source_url", "Source URL"),
    ("verification_status", "Verification status"),
    ("missing_reason", "Missing reason"),
]

_EDITABLE = {name for name, _ in EDITABLE_FIELDS}


def initialize_admin_tables() -> None:
    initialize_database()
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin_edits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_detail_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                old_value TEXT NOT NULL DEFAULT '',
                new_value TEXT NOT NULL DEFAULT '',
                editor TEXT NOT NULL,
                edited_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_admin_edits_product
            ON admin_edits(product_detail_id);

            CREATE INDEX IF NOT EXISTS idx_admin_edits_time
            ON admin_edits(edited_at);
            """
        )


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def find_records(
    query: str = "",
    source: str = "",
    country: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Matching registrations plus the total, so the page can show its position."""
    initialize_admin_tables()
    clauses: list[str] = []
    args: list[Any] = []
    if query:
        clauses.append(
            "(substance LIKE ? OR product LIKE ? OR company LIKE ? OR registration_number LIKE ?)"
        )
        args.extend([f"%{query}%"] * 4)
    if source:
        clauses.append("source = ?")
        args.append(source)
    if country:
        clauses.append("country = ?")
        args.append(country)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM product_details {where}", args
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT id, substance, product, company, country, source, status,
                   registration_number, manufacturer_name, verification_status, last_checked
            FROM product_details {where}
            ORDER BY substance, product, country
            LIMIT ? OFFSET ?
            """,
            [*args, limit, offset],
        ).fetchall()
    return [dict(row) for row in rows], int(total)


def filter_options() -> dict[str, list[str]]:
    initialize_admin_tables()
    with get_connection() as conn:
        sources = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT source FROM product_details WHERE COALESCE(source,'')<>'' ORDER BY source"
            )
        ]
        countries = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT country FROM product_details WHERE COALESCE(country,'')<>'' ORDER BY country"
            )
        ]
    return {"sources": sources, "countries": countries}


def get_record(product_detail_id: int) -> dict[str, Any] | None:
    initialize_admin_tables()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM product_details WHERE id = ?", (product_detail_id,)
        ).fetchone()
        if not row:
            return None
        record = dict(row)
        record["manufacturers"] = [
            dict(item) for item in conn.execute(
                """
                SELECT o.display_name AS name, s.site_name, s.address,
                       COALESCE(s.country, o.country, '') AS country,
                       r.role, r.scope, r.verification_status
                FROM registration_organization_roles r
                LEFT JOIN organizations o ON o.id = r.organization_id
                LEFT JOIN manufacturing_sites s ON s.id = r.site_id
                WHERE r.product_detail_id = ? ORDER BY r.id
                """,
                (product_detail_id,),
            )
        ]
        record["documents"] = [
            dict(item) for item in conn.execute(
                """SELECT document_type, title, url, source_regulator, verification_status
                   FROM regulatory_documents WHERE product_detail_id = ? ORDER BY id""",
                (product_detail_id,),
            )
        ]
        record["evidence"] = [
            dict(item) for item in conn.execute(
                """SELECT field_name, value, source_regulator, evidence_url, evidence_section,
                          extraction_method, verification_status, extracted_at
                   FROM evidence WHERE product_detail_id = ? ORDER BY extracted_at DESC, id DESC""",
                (product_detail_id,),
            )
        ]
        record["edits"] = [
            dict(item) for item in conn.execute(
                """SELECT field_name, old_value, new_value, editor, edited_at
                   FROM admin_edits WHERE product_detail_id = ? ORDER BY edited_at DESC, id DESC
                   LIMIT 50""",
                (product_detail_id,),
            )
        ]
    return record


def update_record(
    product_detail_id: int, submitted: dict[str, str], editor: str
) -> dict[str, Any]:
    """Apply the changed fields, and record each one as a manual assertion."""
    initialize_admin_tables()
    now = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        current = conn.execute(
            "SELECT * FROM product_details WHERE id = ?", (product_detail_id,)
        ).fetchone()
        if not current:
            return {"found": False, "changed": []}
        current = dict(current)

        changes: list[tuple[str, str, str]] = []
        for field, value in submitted.items():
            if field not in _EDITABLE:
                continue
            new_value = _text(value)
            old_value = _text(current.get(field))
            if new_value != old_value:
                changes.append((field, old_value, new_value))

        if not changes:
            return {"found": True, "changed": []}

        assignments = ", ".join(f"{field}=?" for field, _, _ in changes)
        conn.execute(
            f"UPDATE product_details SET {assignments} WHERE id = ?",
            [new for _, _, new in changes] + [product_detail_id],
        )

        for field, old_value, new_value in changes:
            conn.execute(
                """
                INSERT INTO admin_edits(
                    product_detail_id, field_name, old_value, new_value, editor, edited_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (product_detail_id, field, old_value, new_value, editor, now),
            )
            # The override is a claim in its own right, so it joins the
            # evidence for this row rather than quietly replacing a
            # regulator's value with an unattributed one.
            conn.execute(
                """
                INSERT INTO evidence(
                    product_detail_id, document_id, field_name, value, role,
                    source_regulator, document_type, evidence_url, evidence_page,
                    evidence_section, extraction_method, extracted_at,
                    verification_status, missing_reason
                ) VALUES (?, NULL, ?, ?, '', ?, '', '', '', ?, ?, ?, ?, '')
                ON CONFLICT(
                    product_detail_id, field_name, role, value,
                    evidence_url, evidence_page, evidence_section
                ) DO UPDATE SET
                    extracted_at=excluded.extracted_at,
                    extraction_method=excluded.extraction_method,
                    verification_status=excluded.verification_status
                """,
                (
                    product_detail_id, field, new_value, current.get("source") or "",
                    f"{MANUAL_SECTION} by {editor}", MANUAL_METHOD, now, MANUAL_STATUS,
                ),
            )

    logger.info(
        "Admin %s edited record %s: %s",
        editor, product_detail_id, ", ".join(field for field, _, _ in changes),
    )
    return {"found": True, "changed": [field for field, _, _ in changes]}


def recent_edits(limit: int = 30) -> list[dict[str, Any]]:
    initialize_admin_tables()
    with get_connection() as conn:
        return [
            dict(row) for row in conn.execute(
                """
                SELECT e.product_detail_id, e.field_name, e.old_value, e.new_value,
                       e.editor, e.edited_at, p.product, p.country
                FROM admin_edits e
                LEFT JOIN product_details p ON p.id = e.product_detail_id
                ORDER BY e.edited_at DESC, e.id DESC LIMIT ?
                """,
                (limit,),
            )
        ]
