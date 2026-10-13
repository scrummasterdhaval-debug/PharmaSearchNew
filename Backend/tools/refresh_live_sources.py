"""Weekly: ask the regulators that are searched live about the molecules already stored.

    python tools/refresh_live_sources.py                  # one weekly slice
    python tools/refresh_live_sources.py --status         # what is oldest, and which sources run
    python tools/refresh_live_sources.py --molecules 40 --minutes 180

Twelve regulators publish a whole file, and tools/refresh_registers.py keeps
those copies current. The other twenty-odd -- MHRA, the EU national sites, the
Asian and Middle Eastern registers -- are searched one molecule at a time, so
their rows only become current when somebody happens to search that molecule.
Rows stored months ago then quietly describe last quarter's register.

This job re-asks them, oldest molecule first, so every stored molecule comes
round again. It works in slices: a weekly run takes the molecules that have
gone longest without a check and stops at its time budget, which keeps one run
predictable rather than letting it queue for days. What it stores goes through
the same connectors and the same save path as a search, so nothing here can
invent a value a regulator did not publish.

Sources that answer only with a link to the regulator's own search page -- the
ten that still have no parser -- are left out, because asking them again costs
the budget and returns no data. --all includes them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from repository import get_connection, initialize_database  # noqa: E402
from services.harvest import run_harvest  # noqa: E402
from sources.open_registers import REGISTERS  # noqa: E402
from sources.source_registry import CONNECTORS  # noqa: E402

LOG_PATH = Path(os.getenv("PHARMASEARCH_LOG_DIR") or (BACKEND / "logs")) / "refresh_live_sources.log"

# How much recent history decides whether a source still answers with products.
RECENT_RUNS = 30
DEFAULT_MOLECULES = 25
DEFAULT_MINUTES = 120


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def _answers_with_products(source: str) -> bool:
    """Has this source returned rows recently, rather than a search-page link?

    Judged on its own recent history rather than a hand-kept list, so a
    connector that has just been repaired rejoins by itself and one that has
    stopped parsing drops out without anyone editing a file here.
    """
    with get_connection() as connection:
        statuses = [
            str(row["status"])
            for row in connection.execute(
                "SELECT status FROM source_runs WHERE source = ? ORDER BY checked_at DESC LIMIT ?",
                (source, RECENT_RUNS),
            )
        ]
    if not statuses:
        return True  # never asked; give it the chance
    return any(status in {"SUCCESS", "NOT_PUBLISHED"} for status in statuses)


def live_sources(include_all: bool = False) -> list[str]:
    """Enabled connectors that are searched live, newest-first by usefulness.

    The file-based registers are excluded: refresh_registers.py replaces those
    copies whole, and asking their connectors molecule by molecule would only
    read the same local index back.
    """
    names = [
        connector.metadata.name
        for connector in CONNECTORS
        if connector.metadata.enabled and connector.metadata.name not in REGISTERS
    ]
    if include_all:
        return names
    initialize_database()
    return [name for name in names if _answers_with_products(name)]


def oldest_molecules(limit: int) -> list[str]:
    """Stored molecules that have gone longest without being checked.

    Two rows can hold the same molecule under different substance keys, and
    asking a registry the same question twice in one slice wastes budget, so
    the names are folded together here.
    """
    initialize_database()
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT substance, MIN(COALESCE(last_checked, '')) AS oldest
            FROM product_details
            WHERE TRIM(COALESCE(substance, '')) <> ''
            GROUP BY COALESCE(NULLIF(TRIM(substance_key), ''), LOWER(TRIM(substance)))
            ORDER BY oldest ASC, substance ASC
            """,
        ).fetchall()
    seen: dict[str, str] = {}
    for row in rows:
        name = str(row["substance"]).strip()
        seen.setdefault(" ".join(name.lower().split()), name)
        if len(seen) >= limit:
            break
    return list(seen.values())


def run_slice(molecules: int = DEFAULT_MOLECULES, minutes: float = DEFAULT_MINUTES, include_all: bool = False) -> dict:
    """One weekly slice: the oldest molecules, every live source, within a time budget."""
    sources = live_sources(include_all)
    terms = oldest_molecules(molecules)
    if not (sources and terms):
        log("nothing to do: no live sources or no stored molecules")
        return {"rows": 0, "sources": [], "molecules": terms}

    stop_event = threading.Event()
    # The budget is a wall clock, not a per-source one: harvest checks the
    # event between molecules, so a slow registry costs its own slice and not
    # the whole run.
    timer = threading.Timer(minutes * 60, stop_event.set)
    timer.daemon = True
    timer.start()
    started = time.time()
    try:
        result = run_harvest(
            sources=sources,
            molecules=terms,
            resume=False,  # the point of this job is to ask again
            stop_event=stop_event,
        )
    finally:
        timer.cancel()

    summary = result.to_dict()
    summary["molecules"] = terms
    summary["minutes"] = round((time.time() - started) / 60, 1)
    summary["stopped_at_budget"] = stop_event.is_set()
    return summary


def status_text() -> str:
    terms = oldest_molecules(10)
    initialize_database()
    with get_connection() as connection:
        oldest = connection.execute(
            "SELECT MIN(COALESCE(NULLIF(last_checked, ''), '')) FROM product_details"
        ).fetchone()[0]
        total = connection.execute(
            "SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(substance_key), ''), LOWER(TRIM(substance))))"
            " FROM product_details WHERE TRIM(COALESCE(substance, '')) <> ''"
        ).fetchone()[0]
    sources = live_sources()
    skipped = [name for name in live_sources(include_all=True) if name not in sources]
    lines = [
        f"{total:,} molecules stored; oldest check {oldest or 'never recorded'}",
        f"{len(sources)} live sources refreshed: " + ", ".join(sources),
    ]
    if skipped:
        lines.append(f"{len(skipped)} skipped (link-only today): " + ", ".join(skipped))
    # Joined with a bar, not a comma: a combination product's molecule is
    # itself a comma-separated list, and a comma here reads as two molecules.
    lines.append("next up: " + " | ".join(terms))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--molecules", type=int, default=DEFAULT_MOLECULES, help="How many molecules this slice covers")
    parser.add_argument("--minutes", type=float, default=DEFAULT_MINUTES, help="Wall-clock budget for the run")
    parser.add_argument("--all", action="store_true", help="Include sources that only return a registry link")
    parser.add_argument("--status", action="store_true", help="Print what the next run would do and exit")
    args = parser.parse_args(argv)

    if args.status:
        print(status_text())
        return 0

    started = datetime.now(timezone.utc)
    log(f"run started: {args.molecules} molecules, {args.minutes:.0f} min budget")
    try:
        summary = run_slice(args.molecules, args.minutes, args.all)
    except Exception:
        log("FAILED\n" + traceback.format_exc())
        raise

    stored = summary.get("rows", 0)
    stopped = [item["source"] for item in summary.get("sources", []) if item.get("stopped_early")]
    line = (
        f"run finished in {(datetime.now(timezone.utc) - started).total_seconds() / 60:.0f} min: "
        f"{stored:,} rows stored across {len(summary.get('sources', []))} sources"
        + (f"; {len(stopped)} stopped early: {', '.join(stopped)}" if stopped else "")
        + ("; budget reached" if summary.get("stopped_at_budget") else "")
    )
    log(line)
    log(json.dumps(summary)[:4000])
    print(line)
    # A run that stored nothing at all is a failure worth reporting; one slow
    # registry among twenty is not.
    return 0 if stored else 1


if __name__ == "__main__":
    raise SystemExit(main())
