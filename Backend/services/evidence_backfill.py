"""Give the rows harvested before the evidence table existed their provenance.

Evidence is written where a record is saved, so rows harvested before the save
path recorded it carry none. Their provenance was never lost -- it sits in the
columns evidence_assertions reads -- and this walks the stored rows turning
those columns back into the per-field assertions the evidence table holds.

Rows saved from now on get the same assertions at save time, so this is a
one-off catch-up for what is already stored rather than a step in the pipeline.
"""

from __future__ import annotations

from typing import Any, Callable

from repository import EVIDENCE_INSERT_SQL, get_connection
from services.evidence_assertions import (
    COMPLETABLE_FIELDS,
    MANUFACTURER_FIELDS,
    REGISTER_FIELDS,
    REGISTER_SECTION,
    VERIFIED_RECORD,
    assertions_for_row,
    missing_reason_for_row,
    register_url,
)


_SELECT_COLUMNS = (
    "id", "source", "source_url", "product_url", "last_checked", "document_type",
    "manufacturer_source", "completion_source", "smpc_url", "pil_url",
    "assessment_report_url", "evidence_url", "verification_status", "missing_reason",
    *REGISTER_FIELDS,
    *COMPLETABLE_FIELDS,
    *MANUFACTURER_FIELDS,
)


def backfill_evidence(
    *,
    limit: int | None = None,
    batch_size: int = 1000,
    dry_run: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Reconstruct evidence for stored rows that were saved without it.

    Idempotent: the unique index on the evidence table means a second run
    refreshes the same assertions rather than duplicating them, and rows
    already stamped are skipped, so an interrupted pass resumes where it
    stopped.
    """
    columns = ", ".join(f'"{name}"' for name in _SELECT_COLUMNS)
    summary = {
        "rows_scanned": 0,
        "rows_with_evidence": 0,
        "assertions_written": 0,
        "rows_stamped": 0,
        "reasons_filled": 0,
        "rows_without_source_url": 0,
        "dry_run": dry_run,
    }

    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM product_details WHERE COALESCE(evidence_url,'') = ''"
        ).fetchone()[0]
        if limit is not None:
            total = min(total, limit)

        last_id = 0
        while True:
            remaining = None if limit is None else limit - summary["rows_scanned"]
            if remaining is not None and remaining <= 0:
                break
            take = batch_size if remaining is None else min(batch_size, remaining)
            batch = [
                dict(item) for item in conn.execute(
                    f"""SELECT {columns} FROM product_details
                        WHERE id > ? AND COALESCE(evidence_url,'') = ''
                        ORDER BY id LIMIT ?""",
                    (last_id, take),
                )
            ]
            if not batch:
                break
            last_id = batch[-1]["id"]
            summary["rows_scanned"] += len(batch)

            assertions: list[tuple] = []
            stamps: list[tuple] = []
            for row in batch:
                url = register_url(row)
                if not url:
                    summary["rows_without_source_url"] += 1
                    continue
                row_assertions = assertions_for_row(row)
                if row_assertions:
                    assertions.extend(row_assertions)
                    summary["rows_with_evidence"] += 1
                reason = missing_reason_for_row(row)
                if reason:
                    summary["reasons_filled"] += 1
                stamps.append((
                    url, REGISTER_SECTION,
                    str(row.get("verification_status") or "").strip() or VERIFIED_RECORD,
                    reason, row["id"],
                ))

            summary["assertions_written"] += len(assertions)
            summary["rows_stamped"] += len(stamps)

            if not dry_run:
                if assertions:
                    conn.executemany(EVIDENCE_INSERT_SQL, assertions)
                if stamps:
                    conn.executemany(
                        """UPDATE product_details
                           SET evidence_url = ?, evidence_section = ?,
                               verification_status = ?, missing_reason = ?
                           WHERE id = ?""",
                        stamps,
                    )
                conn.commit()

            if progress:
                progress(summary["rows_scanned"], total)

    return summary


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Backfill evidence for stored rows.")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many rows")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true", help="count without writing")
    args = parser.parse_args()

    def report(scanned: int, total: int) -> None:
        print(f"  {scanned}/{total} rows", flush=True)

    print(json.dumps(
        backfill_evidence(
            limit=args.limit,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            progress=report,
        ),
        indent=2,
    ))
