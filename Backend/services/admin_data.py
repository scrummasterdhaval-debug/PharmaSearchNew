"""Bulk data management: purge a source, prune stale rows, export a slice.

The two destructive operations each come in two halves. The preview counts
exactly what would go without touching anything, so the confirmation an
operator types is against a real number rather than a guess.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from export_service import write_excel_export
from repository import (
    PRODUCT_DETAIL_CHILD_TABLES,
    get_connection,
    initialize_database,
    list_product_details,
    search_product_details,
)


logger = get_logger(__name__)

EXPORT_ROW_LIMIT = 5000

# Rows hanging off a registration. Deleting the registration must take them
# too, or they outlive it as rows nothing can reach.
_CHILD_TABLES = PRODUCT_DETAIL_CHILD_TABLES


def _counts_for(conn, where: str, args: list[Any]) -> dict[str, int]:
    scope = f"SELECT id FROM product_details {where}"
    counts = {
        "products": conn.execute(
            f"SELECT COUNT(*) FROM product_details {where}", args
        ).fetchone()[0]
    }
    for table in _CHILD_TABLES:
        counts[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE product_detail_id IN ({scope})", args
        ).fetchone()[0]
    return {key: int(value) for key, value in counts.items()}


def _delete_where(where: str, args: list[Any]) -> dict[str, int]:
    scope = f"SELECT id FROM product_details {where}"
    with get_connection() as conn:
        counts = _counts_for(conn, where, args)
        for table in _CHILD_TABLES:
            conn.execute(
                f"DELETE FROM {table} WHERE product_detail_id IN ({scope})", args
            )
        conn.execute(f"DELETE FROM product_details {where}", args)
    return counts


def purge_preview(source: str) -> dict[str, int]:
    initialize_database()
    with get_connection() as conn:
        return _counts_for(conn, "WHERE source = ?", [source])


def purge_source(source: str, actor: str) -> dict[str, int]:
    counts = _delete_where("WHERE source = ?", [source])
    logger.warning(
        "Admin %s purged %s: %s rows and %s assertions",
        actor, source, counts["products"], counts["evidence"],
    )
    return counts


def _stale_cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def prune_preview(days: int) -> dict[str, int]:
    initialize_database()
    with get_connection() as conn:
        return _counts_for(
            conn,
            "WHERE last_checked <> '' AND last_checked < ?",
            [_stale_cutoff(days)],
        )


def prune_stale(days: int, actor: str) -> dict[str, int]:
    counts = _delete_where(
        "WHERE last_checked <> '' AND last_checked < ?", [_stale_cutoff(days)]
    )
    logger.warning(
        "Admin %s pruned rows unchecked for %s days: %s removed",
        actor, days, counts["products"],
    )
    return counts


def source_row_counts() -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as conn:
        return [
            {"source": row[0] or "(unattributed)", "rows": int(row[1])}
            for row in conn.execute(
                """SELECT source, COUNT(*) FROM product_details
                   GROUP BY source ORDER BY COUNT(*) DESC"""
            )
        ]


def export_slice(
    substance: str = "", source: str = "", country: str = "", actor: str = ""
) -> tuple[Path | None, int]:
    """Write the filtered rows to a workbook. Returns the path and the count."""
    substance = substance.strip()
    rows = search_product_details(substance) if substance else list_product_details()
    if source:
        rows = [row for row in rows if (row.get("source") or "") == source]
    if country:
        rows = [row for row in rows if (row.get("country") or "") == country]
    if not rows:
        return None, 0

    truncated = rows[:EXPORT_ROW_LIMIT]
    label = substance or source or country or "all-records"
    path = write_excel_export(label, truncated)
    logger.info("Admin %s exported %s rows to %s", actor, len(truncated), path.name)
    return path, len(truncated)
