"""Read-only figures for the admin dashboard.

Every query here reads; none of them writes. The source_runs table in
particular has been recording connector outcomes with nothing to show them,
and this is what reads it back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from repository import get_connection, initialize_database


# Rows carrying at least one assertion that someone read a document or a
# product page for, as opposed to taking the register's word for it.
_DOCUMENT_VERIFIED_SQL = """
    SELECT COUNT(DISTINCT product_detail_id) FROM evidence
    WHERE verification_status IN (
        'VERIFIED_OFFICIAL_DOCUMENT', 'VERIFIED_PRODUCT_PAGE', 'VERIFIED_MANUAL_ENTRY'
    )
"""


# The fields a regulatory row is judged complete on, in the order an analyst
# tends to look for them.
COVERAGE_FIELDS = [
    ("registration_number", "Registration number"),
    ("registration_date", "Registration date"),
    ("company", "MA holder"),
    ("applicant_sponsor", "Applicant / sponsor"),
    ("manufacturer_name", "Manufacturer name"),
    ("manufacturer_country", "Manufacturer country"),
    ("manufacturer_address", "Manufacturer address"),
    ("atc_code", "ATC code"),
    ("smpc_url", "SmPC URL"),
    ("pil_url", "PIL URL"),
    ("assessment_report_url", "Assessment report"),
    ("evidence_url", "Evidence URL"),
]

# Worst first, so the connectors needing attention sort to the top.
STATE_ORDER = {
    "down": 0, "poor": 1, "degraded": 2, "empty": 3,
    "handoff": 4, "healthy": 5, "unknown": 6,
}

STATE_LABELS = {
    "down": "down",
    "poor": "poor",
    "degraded": "degraded",
    "empty": "no records",
    "handoff": "manual register",
    "healthy": "healthy",
    "unknown": "not run",
}


def _scalar(conn, sql: str, args: tuple = ()) -> int:
    row = conn.execute(sql, args).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _percent(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def coverage() -> dict[str, Any]:
    """What a search can reach today, counting only sources that return products.

    A downloaded register counts when its index exists; a live source counts
    once it has returned rows. Connectors that only hand back a link to the
    regulator's search page, or have never produced a row, are left out.
    """
    from sources.open_registers import REGISTERS, index_info
    from sources.source_registry import CONNECTORS

    initialize_database()
    with get_connection() as conn:
        producing = {
            str(row[0])
            for row in conn.execute("SELECT DISTINCT source FROM source_runs WHERE status = 'SUCCESS'")
        }
        producing |= {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT source FROM product_details WHERE COALESCE(source,'')<>''"
                " AND COALESCE(document_type,'') NOT LIKE '%handoff%'"
            )
        }
    countries: set[str] = set()
    sources = 0
    register_rows = 0
    for connector in CONNECTORS:
        meta = connector.metadata
        if not meta.enabled:
            continue
        register = REGISTERS.get(meta.name)
        info = index_info(register) if register is not None else None
        if info is not None:
            try:
                register_rows += int(info.get("rows", 0))
            except (TypeError, ValueError):
                pass
        elif meta.name not in producing:
            continue
        sources += 1
        countries.update(meta.countries)
    return {"countries": len(countries), "sources": sources, "register_rows": register_rows}


def overview() -> dict[str, Any]:
    initialize_database()
    with get_connection() as conn:
        products = _scalar(conn, "SELECT COUNT(*) FROM product_details")
        return {
            "products": products,
            "sources": _scalar(conn, "SELECT COUNT(DISTINCT source) FROM product_details WHERE COALESCE(source,'')<>''"),
            "countries": _scalar(conn, "SELECT COUNT(DISTINCT country) FROM product_details WHERE COALESCE(country,'')<>''"),
            "substances": _scalar(conn, "SELECT COUNT(DISTINCT substance_key) FROM product_details WHERE COALESCE(substance_key,'')<>''"),
            "evidence": _scalar(conn, "SELECT COUNT(*) FROM evidence"),
            "documents": _scalar(conn, "SELECT COUNT(*) FROM regulatory_documents"),
            "organizations": _scalar(conn, "SELECT COUNT(*) FROM organizations"),
            "sites": _scalar(conn, "SELECT COUNT(*) FROM manufacturing_sites"),
            "roles": _scalar(conn, "SELECT COUNT(*) FROM registration_organization_roles"),
            "source_runs": _scalar(conn, "SELECT COUNT(*) FROM source_runs"),
            # Counted from the assertions rather than the row's own status.
            # Every harvested row rests on a register entry, so a status of
            # "not unverified" is now true of nearly all of them and says
            # nothing. What is worth watching is how many rows anyone has
            # actually opened a document to confirm.
            "verified": _scalar(conn, _DOCUMENT_VERIFIED_SQL),
            "verified_percent": _percent(_scalar(conn, _DOCUMENT_VERIFIED_SQL), products),
            "register_sourced": _scalar(
                conn,
                "SELECT COUNT(*) FROM product_details WHERE COALESCE(evidence_url,'') <> ''",
            ),
        }


def source_health() -> list[dict[str, Any]]:
    """One row per connector: how its runs have gone and when it last ran."""
    initialize_database()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT source,
                   COUNT(*) AS runs,
                   SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) AS successes,
                   SUM(CASE WHEN status='PARSER_FAILED' THEN 1 ELSE 0 END) AS failures,
                   SUM(CASE WHEN status='SOURCE_UNSUPPORTED' THEN 1 ELSE 0 END) AS unsupported,
                   SUM(CASE WHEN status='NOT_PUBLISHED' THEN 1 ELSE 0 END) AS not_published,
                   SUM(records_found) AS records,
                   MAX(checked_at) AS last_run
            FROM source_runs
            GROUP BY source
            ORDER BY source
            """
        ).fetchall()

        stored = {
            row["source"]: row["n"]
            for row in conn.execute(
                "SELECT source, COUNT(*) AS n FROM product_details GROUP BY source"
            )
        }

    health = []
    for row in rows:
        runs = int(row["runs"] or 0)
        successes = int(row["successes"] or 0)
        failures = int(row["failures"] or 0)
        unsupported = int(row["unsupported"] or 0)

        # A run that handed off to a manual register never tried to return
        # records, so counting it as a failed run would call a working
        # connector broken. Rate it only against the runs that could publish.
        attempted = runs - unsupported
        if runs == 0:
            state, rate, rate_known = "unknown", 0.0, False
        elif attempted == 0:
            state, rate, rate_known = "handoff", 0.0, False
        else:
            rate, rate_known = _percent(successes, attempted), True
            if successes == 0 and failures:
                state = "down"
            elif rate >= 80:
                state = "healthy"
            elif rate >= 40:
                state = "degraded"
            elif rate > 0:
                state = "poor"
            else:
                state = "empty"

        health.append({
            "source": row["source"],
            "runs": runs,
            "successes": successes,
            "failures": failures,
            "unsupported": unsupported,
            "not_published": int(row["not_published"] or 0),
            "records": int(row["records"] or 0),
            "stored": stored.get(row["source"], 0),
            "success_rate": rate,
            "rate_known": rate_known,
            "state": state,
            "last_run": row["last_run"] or "",
        })
    health.sort(key=lambda item: (STATE_ORDER.get(item["state"], 9), -item["failures"], item["source"]))
    return health


def recent_failures(limit: int = 15) -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT source, query, status, error, checked_at
                FROM source_runs
                WHERE status <> 'SUCCESS' AND COALESCE(error,'') <> ''
                ORDER BY checked_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]


def field_coverage() -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as conn:
        total = _scalar(conn, "SELECT COUNT(*) FROM product_details")
        coverage = []
        for column, label in COVERAGE_FIELDS:
            filled = _scalar(
                conn,
                f"SELECT COUNT(*) FROM product_details WHERE TRIM(COALESCE({column},'')) <> ''",
            )
            coverage.append({
                "field": column,
                "label": label,
                "filled": filled,
                "missing": total - filled,
                "percent": _percent(filled, total),
            })
    coverage.sort(key=lambda item: item["percent"])
    return coverage


def top_sources(limit: int = 12) -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as conn:
        total = _scalar(conn, "SELECT COUNT(*) FROM product_details")
        return [
            {"name": row["source"] or "(unattributed)", "rows": row["n"], "percent": _percent(row["n"], total)}
            for row in conn.execute(
                "SELECT source, COUNT(*) AS n FROM product_details GROUP BY source ORDER BY n DESC LIMIT ?",
                (limit,),
            )
        ]


def top_countries(limit: int = 12) -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as conn:
        total = _scalar(conn, "SELECT COUNT(*) FROM product_details")
        return [
            {"name": row["country"] or "(unattributed)", "rows": row["n"], "percent": _percent(row["n"], total)}
            for row in conn.execute(
                "SELECT country, COUNT(*) AS n FROM product_details GROUP BY country ORDER BY n DESC LIMIT ?",
                (limit,),
            )
        ]


def staleness() -> list[dict[str, Any]]:
    """How long ago each row was last confirmed against its register."""
    initialize_database()
    now = datetime.now(timezone.utc)
    buckets = [
        ("Last 7 days", 0, 7),
        ("7 to 30 days", 7, 30),
        ("30 to 90 days", 30, 90),
        ("Over 90 days", 90, 100000),
    ]
    with get_connection() as conn:
        total = _scalar(conn, "SELECT COUNT(*) FROM product_details")
        rows = []
        for label, low, high in buckets:
            newest = (now - timedelta(days=low)).isoformat()
            oldest = (now - timedelta(days=high)).isoformat()
            count = _scalar(
                conn,
                "SELECT COUNT(*) FROM product_details WHERE last_checked <> '' AND last_checked <= ? AND last_checked > ?",
                (newest, oldest),
            )
            rows.append({"label": label, "rows": count, "percent": _percent(count, total)})
        never = _scalar(conn, "SELECT COUNT(*) FROM product_details WHERE COALESCE(last_checked,'') = ''")
        rows.append({"label": "Never recorded", "rows": never, "percent": _percent(never, total)})
    return rows


def migrations() -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT name, applied_at FROM schema_migrations ORDER BY applied_at DESC, name"
            )
        ]


def dashboard() -> dict[str, Any]:
    return {
        "overview": overview(),
        "coverage": coverage(),
        "source_health": source_health(),
        "recent_failures": recent_failures(),
        "field_coverage": field_coverage(),
        "top_sources": top_sources(),
        "top_countries": top_countries(),
        "staleness": staleness(),
        "migrations": migrations(),
        "state_labels": STATE_LABELS,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
