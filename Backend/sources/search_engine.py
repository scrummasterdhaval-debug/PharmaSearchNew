from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from copy import deepcopy
from functools import lru_cache
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


def search_substance(
    substance: str,
    source_names: list[str] | None = None,
    timeout_seconds: int = LIVE_SEARCH_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    timeout_seconds = timeout_seconds or LIVE_SEARCH_TIMEOUT_SECONDS
    source_key = tuple(sorted(name.strip().lower() for name in source_names or [] if name.strip()))
    return deepcopy(_search_substance_cached(substance.strip(), source_key, timeout_seconds))


@lru_cache(maxsize=128)
def _search_substance_cached(
    substance: str,
    source_key: tuple[str, ...],
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    allowed_sources = None
    if source_key:
        allowed_sources = set(source_key)

    jobs = {}
    executor = ThreadPoolExecutor(max_workers=min(8, max(1, len(SOURCES) * 2)))
    try:
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
                job = executor.submit(_run_source_search, source, search_term, substance)
                jobs[job] = source["name"]

        results = []
        try:
            completed_jobs = as_completed(
                jobs,
                timeout=timeout_seconds,
            )
            for job in completed_jobs:
                results.extend(job.result())
        except TimeoutError:
            pending_sources = sorted(
                {source_name for job, source_name in jobs.items() if not job.done()}
            )
            if pending_sources:
                logger.warning("Search timeout skipped sources: %s", ", ".join(pending_sources))
            for job in jobs:
                if not job.done():
                    job.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

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

    logger.info("Search returned %s raw results and %s unique results", len(results), len(unique))

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
        return []
