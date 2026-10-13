"""Which sources a search still has to ask, and which the store already answers.

A full live search takes minutes; the stored rows answer in seconds. But "the
molecule is in the store" is not the same as "the store is complete for it":
most stored molecules come from one or two registries, and a live search used
to save only the page of rows it showed. So the store answers for a source
only when both hold:

- the source was checked for this molecule within ``LIVE_MAX_AGE_DAYS``, and
  that check did not fail; and
- the check found nothing (NOT_PUBLISHED: there is nothing to store), or a
  live search since that check stored its answer and read every row of it
  back from the store (``covered_sources``).

The read-back is what makes the second test exact. Counting rows is not
enough: the store keeps one row per registration as it identifies them, and
some sources publish several page rows under one -- MHRA an SmPC and a
leaflet per licence, BPOM a pack per product page, CDSCO the same generic
URL for every approval -- so a count of stored rows can match the page's
while rows are missing, padded by older rows the live answer no longer has.
A source whose rows do not all come back is simply asked live every time.

Two kinds of source are always asked live, whatever the log says:

- the registers read from a local copy (``open_registers.REGISTERS``), which
  answer in milliseconds from a file the daily job keeps current; and
- view-only sources (BfArM), whose rows the store is not allowed to keep, so
  it can never answer for them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from repository import get_connection, initialize_database


def _max_age_days() -> float:
    try:
        return float(os.getenv("PHARMASEARCH_LIVE_MAX_AGE_DAYS", "7"))
    except ValueError:
        return 7.0


# The weekly live refresh re-checks the stored molecules that have gone
# longest without a check, so a week keeps a search and the schedule in step.
LIVE_MAX_AGE_DAYS = _max_age_days()
# Outcomes that say what the source holds. A parser failure says nothing.
CHECKED_STATUSES = ("SUCCESS", "NOT_PUBLISHED", "SOURCE_UNSUPPORTED")
VIEW_ONLY_SOURCES = frozenset({"BfArM Germany"})


@dataclass(frozen=True)
class SourceFreshness:
    source: str
    ask_live: bool
    reason: str
    checked_at: str = ""


def _page_keys(rows: Iterable[dict[str, Any]]) -> dict[str, set[tuple[str, ...]]]:
    """Per source, the rows as the results page tells them apart."""
    from services.search_pipeline import result_key

    keys: dict[str, set[tuple[str, ...]]] = {}
    for row in rows:
        keys.setdefault(str(row.get("source") or ""), set()).add(result_key(row))
    return keys


def covered_sources(substance: str, rows: list[dict[str, Any]]) -> dict[str, int]:
    """The sources whose every page row comes back from the store, with their row counts.

    ``rows`` are the rows a live search stored for ``substance``. Each is looked
    for among the rows a store search returns, the way the page tells rows apart.
    """
    from repository import search_medicines, search_product_details
    from services.search_pipeline import row_relevant_to_substance

    wanted = _page_keys(rows)
    if not wanted:
        return {}
    stored = _page_keys(
        row for row in search_product_details(substance) + search_medicines(substance)
        if row_relevant_to_substance(row, substance)
    )
    return {
        source: len(keys)
        for source, keys in wanted.items()
        if keys <= stored.get(source, set())
    }


def _local_register_sources() -> frozenset[str]:
    from sources.open_registers import REGISTERS

    return frozenset(register.source for register in REGISTERS.values())


def _latest_checks(sources: list[str], substance: str, since: str) -> dict[str, tuple[str, str, int]]:
    """Per source: (last check time, its status, the most rows any recent check found).

    A source runs one check per synonym, each logged on its own.
    """
    if not sources:
        return {}
    initialize_database()
    placeholders = ", ".join("?" for _ in sources)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT source, status, records_found, checked_at
            FROM source_runs
            WHERE source IN ({placeholders})
              AND lower(query) = lower(?)
              AND checked_at >= ?
            ORDER BY checked_at DESC
            """,
            [*sources, substance.strip(), since],
        ).fetchall()
    checks: dict[str, tuple[str, str, int]] = {}
    for row in rows:
        source = row["source"]
        if source not in checks:
            checks[source] = (row["checked_at"], row["status"], int(row["records_found"] or 0))
        else:
            checked_at, status, most = checks[source]
            checks[source] = (checked_at, status, max(most, int(row["records_found"] or 0)))
    return checks


def _latest_saves(sources: list[str], substance: str, since: str) -> dict[str, str]:
    """Per source: when a live search last stored the molecule's rows and read them all back."""
    if not sources:
        return {}
    placeholders = ", ".join("?" for _ in sources)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT source, MAX(saved_at) AS saved_at
            FROM live_saves
            WHERE source IN ({placeholders})
              AND lower(query) = lower(?)
              AND saved_at >= ?
            GROUP BY source
            """,
            [*sources, substance.strip(), since],
        ).fetchall()
    return {row["source"]: row["saved_at"] for row in rows}


def source_freshness(
    substance: str,
    sources: list[str],
    max_age_days: float | None = None,
    now: datetime | None = None,
) -> list[SourceFreshness]:
    """Decide, per source, whether a search for ``substance`` must ask it live."""
    age = LIVE_MAX_AGE_DAYS if max_age_days is None else max_age_days
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=age)).isoformat()
    local = _local_register_sources()

    remote = [source for source in sources if source not in local and source not in VIEW_ONLY_SOURCES]
    checks = _latest_checks(remote, substance, since) if age > 0 else {}
    saves = _latest_saves(list(checks), substance, since) if checks else {}

    decisions = []
    for source in sources:
        if source in local:
            decisions.append(SourceFreshness(source, True, "local register"))
            continue
        if source in VIEW_ONLY_SOURCES:
            decisions.append(SourceFreshness(source, True, "view only, never stored"))
            continue
        check = checks.get(source)
        if not check:
            decisions.append(SourceFreshness(source, True, "not checked recently"))
            continue
        checked_at, status, found = check
        if status not in CHECKED_STATUSES:
            decisions.append(SourceFreshness(source, True, "last check failed", checked_at))
            continue
        if found == 0:
            decisions.append(SourceFreshness(source, False, "checked recently, lists none", checked_at))
            continue
        # A save counts only if no check came after it: a synonym that
        # answered after the search stopped waiting logs a later check whose
        # rows were never stored.
        saved_at = saves.get(source, "")
        if not saved_at or saved_at < checked_at:
            decisions.append(SourceFreshness(source, True, "answer not held in the store", checked_at))
            continue
        decisions.append(SourceFreshness(source, False, "checked recently", checked_at))
    return decisions
