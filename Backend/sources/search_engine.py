import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from copy import deepcopy
from typing import Any

from sources.source_registry import SOURCES
from sources.open_registers import REGISTERS
from sources.synonyms import get_substance_search_terms
from core.logging_config import get_logger
from repository import save_source_run


LIVE_SEARCH_TIMEOUT_SECONDS = 10
# What an export waits for. The registers answer a combination in about 50
# seconds, so 30 left Italy and Brazil out of the workbook.
COMPLETE_SEARCH_TIMEOUT_SECONDS = 90
logger = get_logger(__name__)
SINGLE_TERM_SOURCES = {
    "cdsco india",
    "hong kong drug office",
    # One search at a time and a minute per molecule: asking BfArM the same
    # question again under each synonym would cost minutes and return the
    # same products, because its own search already ends-masks the term.
    "bfarm germany",
}


# Which sources a live search for a molecule could not include: those that
# did not answer before the deadline, and those that raised. Kept per
# molecule for the results page to say what is missing and why.
_OUTCOMES: dict[str, dict[str, Any]] = {}
_OUTCOMES_LOCK = threading.Lock()
OUTCOME_MAX_AGE_SECONDS = 30 * 60


def search_outcome(substance: str) -> dict[str, list[str]]:
    """The sources the latest live search for this molecule left out, if recent."""
    with _OUTCOMES_LOCK:
        outcome = _OUTCOMES.get(substance.strip().lower())
    if not outcome or time.time() - outcome["at"] > OUTCOME_MAX_AGE_SECONDS:
        return {"timed_out": [], "failed": []}
    return {"timed_out": list(outcome["timed_out"]), "failed": list(outcome["failed"])}


def _remember_outcome(substance: str, timed_out: set[str], failed: set[str]) -> None:
    with _OUTCOMES_LOCK:
        _OUTCOMES[substance.strip().lower()] = {
            "at": time.time(),
            "timed_out": sorted(timed_out),
            "failed": sorted(failed - timed_out),
        }


# A live search keeps running after the page that started it stops waiting:
# the page shows what the quick sources found, the slow ones go on answering
# here, and when the last has answered (or the deadline passed) the complete
# result is handed to ``on_complete`` (the store) and kept for a reload.
RUN_MAX_AGE_SECONDS = 30 * 60
MAX_RUNS = 128


class _Run:
    def __init__(self, timeout_seconds: int) -> None:
        self.started = time.time()
        self.timeout_seconds = timeout_seconds
        self.results: list[dict[str, Any]] = []
        self.pending: dict[str, int] = {}
        self.failed: set[str] = set()
        self.timed_out: set[str] = set()
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.on_complete: list[Any] = []


_RUNS: dict[tuple[str, tuple[str, ...], int], _Run] = {}
_RUNS_LOCK = threading.Lock()


def search_substance(
    substance: str,
    source_names: list[str] | None = None,
    timeout_seconds: int = LIVE_SEARCH_TIMEOUT_SECONDS,
    first_wait_seconds: int | None = None,
    on_complete: Any = None,
    also_for_sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Live rows for the molecule from the named sources (all when None).

    Waits up to ``timeout_seconds`` for every source. With
    ``first_wait_seconds`` it waits only that long and returns what has
    arrived; the rest keeps running, and ``live_sources_pending`` says which
    sources are still out. ``on_complete`` is called once with every row when
    the search ends, whether or not anyone is still waiting.
    ``also_for_sources`` files the search under a wider source list too: the
    one a caller asked for before the store answered for some of them, so
    ``completed_live_rows`` finds it under that list.
    """
    timeout_seconds = timeout_seconds or LIVE_SEARCH_TIMEOUT_SECONDS
    substance = substance.strip()
    source_key = tuple(sorted(name.strip().lower() for name in source_names or [] if name.strip()))
    run = _run_for(substance, source_key, timeout_seconds, on_complete)
    if also_for_sources is not None:
        alias = tuple(sorted(name.strip().lower() for name in also_for_sources if name.strip()))
        with _RUNS_LOCK:
            _RUNS[(substance.lower(), alias, timeout_seconds)] = run
    wait = timeout_seconds if first_wait_seconds is None else min(first_wait_seconds, timeout_seconds)
    run.done.wait(max(0.0, wait - (time.time() - run.started)) if not run.done.is_set() else 0)
    with run.lock:
        rows = list(run.results)
    return deepcopy(_unique(rows))


def live_sources_pending(substance: str, source_names: list[str] | None = None) -> list[str]:
    """The sources a running live search for this molecule is still waiting on."""
    wanted = {name.strip().lower() for name in source_names or [] if name.strip()}
    pending: set[str] = set()
    with _RUNS_LOCK:
        runs = [run for key, run in _RUNS.items() if key[0] == substance.strip().lower()]
    for run in runs:
        if run.done.is_set():
            continue
        with run.lock:
            names = [name for name, count in run.pending.items() if count > 0]
        pending.update(name for name in names if not wanted or name.lower() in wanted)
    return sorted(pending)


def completed_live_rows(
    substance: str,
    source_names: list[str] | None,
    timeout_seconds: int = LIVE_SEARCH_TIMEOUT_SECONDS,
) -> list[dict[str, Any]] | None:
    """Every row of a recent live search over these sources that has ended, or None.

    What a page reloads from once its slow sources have answered, and what a
    change of filter or page reads instead of asking the regulators again.
    """
    source_key = tuple(sorted(name.strip().lower() for name in source_names or [] if name.strip()))
    with _RUNS_LOCK:
        run = _RUNS.get((substance.strip().lower(), source_key, timeout_seconds or LIVE_SEARCH_TIMEOUT_SECONDS))
    if run is None or not run.done.is_set() or time.time() - run.started >= RUN_MAX_AGE_SECONDS:
        return None
    with run.lock:
        rows = list(run.results)
    return deepcopy(_unique(rows))


def _run_for(substance: str, source_key: tuple[str, ...], timeout_seconds: int, on_complete: Any) -> _Run:
    key = (substance.lower(), source_key, timeout_seconds)
    with _RUNS_LOCK:
        run = _RUNS.get(key)
        fresh = run is not None and (not run.done.is_set() or time.time() - run.started < RUN_MAX_AGE_SECONDS)
        if fresh:
            return run
        run = _Run(timeout_seconds)
        if on_complete is not None:
            run.on_complete.append(on_complete)
        _RUNS[key] = run
        if len(_RUNS) > MAX_RUNS:
            for old_key in sorted(_RUNS, key=lambda item: _RUNS[item].started)[: len(_RUNS) - MAX_RUNS]:
                if _RUNS[old_key].done.is_set():
                    del _RUNS[old_key]
    _start_run(run, substance, set(source_key) if source_key else None)
    return run


def _start_run(run: _Run, substance: str, allowed_sources: set[str] | None) -> None:
    executor = ThreadPoolExecutor(max_workers=min(8, max(1, len(SOURCES) * 2)))
    jobs = {}
    # A register held locally answers in milliseconds, a website in tens of
    # seconds. Queued behind the websites they waited for a worker and were
    # cut off with them: a full search for rosuvastatin + ezetimibe lost
    # Italy, Brazil and half of Belgium. They go first now.
    local = frozenset(register.source for register in REGISTERS.values())
    for source in sorted(SOURCES, key=lambda item: item["name"] not in local):
        if allowed_sources is not None and source["name"].strip().lower() not in allowed_sources:
            continue
        source_name = source["name"].strip().lower()
        search_terms = [substance] if source_name in SINGLE_TERM_SOURCES else get_substance_search_terms(substance)
        for search_term in search_terms:
            run.pending[source["name"]] = run.pending.get(source["name"], 0) + 1
            job = executor.submit(_run_source_search, source, search_term, substance, run.failed)
            jobs[job] = source["name"]

    def collect() -> None:
        try:
            for job in as_completed(jobs, timeout=run.timeout_seconds):
                rows = job.result()
                with run.lock:
                    run.results.extend(rows)
                    run.pending[jobs[job]] -= 1
        except TimeoutError:
            with run.lock:
                run.timed_out.update(name for job, name in jobs.items() if not job.done())
            if run.timed_out:
                logger.warning("Search timeout skipped sources: %s", ", ".join(sorted(run.timed_out)))
            for job in jobs:
                job.cancel()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            with run.lock:
                for name in run.pending:
                    run.pending[name] = 0
                rows = _unique(list(run.results))
            _remember_outcome(substance, run.timed_out, run.failed)
            run.done.set()
            for callback in run.on_complete:
                try:
                    callback(deepcopy(rows))
                except Exception:
                    logger.exception("Could not hand over the complete live search for %s", substance)
            logger.info("Search returned %s raw results and %s unique results", len(run.results), len(rows))

    if jobs:
        threading.Thread(target=collect, name=f"live-search-{substance[:20]}", daemon=True).start()
    else:
        executor.shutdown(wait=False)
        _remember_outcome(substance, set(), set())
        run.done.set()


def _unique(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = []
    seen = set()
    for item in results:
        if not item.get("product"):
            continue
        key = (
            item.get("source", "").strip().lower(),
            item.get("product", "").strip().lower(),
            item.get("country", "").strip().lower(),
            item.get("registration_number", "").strip().lower(),
            item.get("url", "").strip().lower(),
            # Two companies holding the same licence for the same pack are two
            # records, not one: Germany lists each parallel importer of Renagel
            # under the licence it imports against, and without the holder here
            # they collapsed into whichever arrived first.
            item.get("company", "").strip().lower(),
        )
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _is_handoff(item: dict[str, Any]) -> bool:
    return str(item.get("connector_mode") or "").strip().lower() == "manual_registry"


def _record_source_run(*args: Any, **kwargs: Any) -> None:
    """Write the audit row, but never at the cost of the results it describes.

    These run inside eight worker threads that share one SQLite file, so a
    contended write can raise. Letting that escape would discard rows this
    source had already fetched and abort every other source's search too.
    """
    try:
        save_source_run(*args, **kwargs)
    except Exception:
        logger.exception("Could not record the source run for %s", args[0] if args else "")


def _run_source_search(
    source: dict[str, Any],
    search_term: str,
    substance: str,
    failed: set[str] | None = None,
) -> list[dict[str, Any]]:
    try:
        logger.info("Running %s search for %s", source["name"], search_term)
        source_results = source["function"](search_term)
        results = []
        for item in source_results or []:
            item["source_substance"] = item.get("substance", "")
            item["searched_substance"] = substance
            item["substance"] = substance
            results.append(item)
        handoffs = [item for item in results if _is_handoff(item)]
        _record_source_run(
            source["name"],
            substance,
            "SOURCE_UNSUPPORTED" if handoffs and len(handoffs) == len(results)
            else ("SUCCESS" if results else "NOT_PUBLISHED"),
            records_found=len(results) - len(handoffs),
            evidence_url=str((results[0] if results else {}).get("source_url") or ""),
        )
        return results
    except Exception as exc:
        logger.exception("%s search failed", source["name"])
        _record_source_run(source["name"], substance, "PARSER_FAILED", error=str(exc))
        if failed is not None:
            failed.add(source["name"])
        return []
