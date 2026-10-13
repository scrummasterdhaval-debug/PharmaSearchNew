"""Walks every connector across the harvest vocabulary, with no substance typed.

A search asks 33 registries about one molecule. A harvest asks them about all
of them, which is the same connectors driven from the other side, so nothing
here re-implements a source: it iterates the registry and persists what comes
back.

Three things make that survivable over thousands of molecules:

* one worker per source rather than a flat thread pool, so a registry is never
  asked two questions at once and its own rate limit is what paces it;
* every (source, molecule) pair recorded as it completes, so a run that is
  stopped -- or that dies overnight -- resumes instead of starting again;
* a consecutive-failure breaker per source, because a registry that has gone
  down stays down, and 3,000 further timeouts against it cost hours and teach
  nothing.
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from core.logging_config import get_logger
from repository import get_connection, initialize_database, save_product_detail, save_source_run
from services.harvest_vocabulary import load_vocabulary
from sources.source_registry import CONNECTORS


logger = get_logger(__name__)

# A registry that has failed this many molecules in a row is down, not slow.
CONSECUTIVE_FAILURE_LIMIT = 8
# Applies when a source declares no rate limit of its own.
DEFAULT_REQUESTS_PER_MINUTE = 30
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# SQLite takes one writer at a time, and a harvest has a worker per source, so
# writes are serialized here rather than left to collide and raise "database is
# locked" -- which would silently cost rows the connectors already paid for.
_write_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_harvest_tables() -> None:
    """Progress lives in the same database as the results it produced."""
    initialize_database()
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS harvest_progress (
                source TEXT NOT NULL,
                molecule TEXT NOT NULL,
                status TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, molecule)
            )
            """
        )


def completed_pairs() -> set[tuple[str, str]]:
    """(source, molecule) pairs already harvested, so a resumed run skips them.

    Failures are deliberately not counted as complete: a registry that timed
    out is worth asking again on the next run.
    """
    initialize_harvest_tables()
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT source, molecule FROM harvest_progress WHERE status = ?",
            (STATUS_DONE,),
        ).fetchall()
    return {(str(row["source"]), str(row["molecule"])) for row in rows}


def record_progress(
    source: str, molecule: str, status: str, row_count: int = 0, error: str = ""
) -> None:
    with _write_lock, get_connection() as connection:
        connection.execute(
            """
            INSERT INTO harvest_progress (source, molecule, status, row_count, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, molecule) DO UPDATE SET
                status = excluded.status,
                row_count = excluded.row_count,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (source, molecule, status, row_count, error[:500], _now()),
        )


@dataclass
class SourceOutcome:
    """What a harvest did to one source."""

    source: str
    molecules: int = 0
    rows: int = 0
    failures: int = 0
    skipped: int = 0
    stopped_early: bool = False
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "molecules": self.molecules,
            "rows": self.rows,
            "failures": self.failures,
            "skipped": self.skipped,
            "stopped_early": self.stopped_early,
            "last_error": self.last_error,
        }


@dataclass
class HarvestResult:
    started_at: str
    finished_at: str = ""
    outcomes: dict[str, SourceOutcome] = field(default_factory=dict)

    @property
    def rows(self) -> int:
        return sum(outcome.rows for outcome in self.outcomes.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "rows": self.rows,
            "sources": [outcome.to_dict() for outcome in self.outcomes.values()],
        }


class _RateLimiter:
    """Spaces one source's requests; each source gets its own."""

    def __init__(self, per_minute: int | None) -> None:
        rate = per_minute or DEFAULT_REQUESTS_PER_MINUTE
        self._interval = 60.0 / max(1, rate)
        self._next_allowed = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_allowed:
            time.sleep(self._next_allowed - now)
        self._next_allowed = max(now, self._next_allowed) + self._interval


def _persist(rows: Iterable[dict[str, Any]], molecule: str) -> int:
    """Store what a connector returned, keeping the harvest molecule as the
    substance so a row is findable under the term that produced it."""
    stored = 0
    for row in rows:
        record = dict(row)
        if not str(record.get("substance") or "").strip():
            record["substance"] = molecule
        try:
            with _write_lock:
                result = save_product_detail(record)
            if result.get("persistence_status") != "SOURCE_RUN_ONLY":
                stored += 1
        except Exception as exc:  # a single malformed row must not end a run
            logger.warning("Could not store a %s row for %s: %s", record.get("source"), molecule, exc)
    return stored


def _harvest_source(
    connector: Any,
    molecules: list[str],
    done: set[tuple[str, str]],
    stop_event: threading.Event,
    on_progress: Callable[[str, str, int], None] | None,
) -> SourceOutcome:
    source = connector.metadata.name
    outcome = SourceOutcome(source=source)
    limiter = _RateLimiter(connector.metadata.rate_limit_per_minute)
    consecutive_failures = 0

    for molecule in molecules:
        if stop_event.is_set():
            outcome.skipped += 1
            continue
        if (source, molecule) in done:
            outcome.skipped += 1
            continue

        limiter.wait()
        try:
            rows = connector.search(molecule) or []
        except Exception as exc:
            consecutive_failures += 1
            outcome.failures += 1
            outcome.last_error = str(exc)
            record_progress(source, molecule, STATUS_FAILED, error=str(exc))
            save_source_run(source, molecule, "PARSER_FAILED", error=str(exc))
            logger.warning("%s failed on %s: %s", source, molecule, exc)
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                # The registry is down. Leave the rest unrecorded so the next
                # run picks them up rather than treating them as harvested.
                outcome.stopped_early = True
                logger.error(
                    "%s stopped after %s consecutive failures", source, consecutive_failures
                )
                break
            continue

        consecutive_failures = 0
        stored = _persist(rows, molecule)
        handoff = next(
            (row for row in rows if str(row.get("connector_mode") or "") == "manual_registry"),
            None,
        )
        save_source_run(
            source,
            molecule,
            "SOURCE_UNSUPPORTED" if handoff and not stored else ("SUCCESS" if stored else "NOT_PUBLISHED"),
            records_found=stored,
            evidence_url=str((handoff or {}).get("source_url") or ""),
        )
        outcome.molecules += 1
        outcome.rows += stored
        record_progress(source, molecule, STATUS_DONE, row_count=stored)
        if on_progress:
            on_progress(source, molecule, stored)

    return outcome


def run_harvest(
    sources: list[str] | None = None,
    molecule_limit: int | None = None,
    molecules: list[str] | None = None,
    resume: bool = True,
    stop_event: threading.Event | None = None,
    on_progress: Callable[[str, str, int], None] | None = None,
) -> HarvestResult:
    """Harvest every selected source across the vocabulary.

    Sources run in parallel with each other and sequentially within themselves,
    which is the only arrangement that respects a per-registry rate limit while
    still finishing in a sensible wall time.
    """
    initialize_harvest_tables()
    terms = molecules if molecules is not None else load_vocabulary(limit=molecule_limit)
    selected = [
        connector
        for connector in CONNECTORS
        if connector.metadata.enabled
        and (sources is None or connector.metadata.name in set(sources))
    ]
    done = completed_pairs() if resume else set()
    stop_event = stop_event or threading.Event()
    result = HarvestResult(started_at=_now())

    logger.info(
        "Harvest starting: %s sources x %s molecules (%s pairs already done)",
        len(selected),
        len(terms),
        len(done),
    )

    threads = []
    outcomes: dict[str, SourceOutcome] = {}
    lock = threading.Lock()

    def worker(connector: Any) -> None:
        outcome = _harvest_source(connector, terms, done, stop_event, on_progress)
        with lock:
            outcomes[outcome.source] = outcome

    for connector in selected:
        thread = threading.Thread(
            target=worker, args=(connector,), name=f"harvest-{connector.metadata.name}", daemon=True
        )
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()

    result.outcomes = outcomes
    result.finished_at = _now()
    logger.info("Harvest finished: %s rows stored", result.rows)
    return result


def harvest_coverage() -> list[dict[str, Any]]:
    """Rows harvested per source, for reporting what a run actually achieved."""
    initialize_harvest_tables()
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT source,
                   COUNT(*) AS molecules,
                   SUM(row_count) AS rows_stored,
                   SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS failures
            FROM harvest_progress
            GROUP BY source
            ORDER BY rows_stored DESC
            """,
            (STATUS_FAILED,),
        ).fetchall()
    return [
        {
            "source": row["source"],
            "molecules": row["molecules"],
            "rows": row["rows_stored"] or 0,
            "failures": row["failures"] or 0,
        }
        for row in rows
    ]


def _main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Harvest every source across the molecule vocabulary")
    parser.add_argument("--sources", nargs="*", default=None, help="Source names; default is all enabled")
    parser.add_argument("--molecules", type=int, default=25, help="How many vocabulary molecules to cover")
    parser.add_argument("--no-resume", action="store_true", help="Re-harvest pairs already recorded as done")
    parser.add_argument("--coverage", action="store_true", help="Print harvested rows per source and exit")
    args = parser.parse_args()

    if args.coverage:
        print(json.dumps(harvest_coverage(), indent=1))
        return

    def report(source: str, molecule: str, stored: int) -> None:
        print(f"{source:<28} {molecule:<28} {stored:>5} rows", flush=True)

    result = run_harvest(
        sources=args.sources,
        molecule_limit=args.molecules,
        resume=not args.no_resume,
        on_progress=report,
    )
    print(json.dumps(result.to_dict(), indent=1))


if __name__ == "__main__":
    _main()
