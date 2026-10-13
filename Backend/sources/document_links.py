"""Which document links still open.

MHRA replaces a PDF with a new file when a label is revised and deletes the old
one, and its search index goes on listing some deleted files. A result that
links a deleted file opens an error page from Azure ("The specified blob does
not exist"). Checking is a HEAD request, about 6 ms of wall time per link at
32 at a time, so links are checked before they are shown.

Only a definite answer counts: 404 and 410 are dead. A timeout or a refused
connection says nothing about the document, so the link is kept.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from typing import Iterable

import requests

from core.logging_config import get_logger


logger = get_logger(__name__)

DEAD_STATUS = {404, 410}
CACHE_SECONDS = 6 * 3600
WORKERS = 32

_cache: dict[str, tuple[bool, float]] = {}
_cache_lock = threading.Lock()


def _is_dead(session: requests.Session, url: str) -> bool | None:
    try:
        response = session.head(url, timeout=10, allow_redirects=True)
    except requests.RequestException:
        return None
    return response.status_code in DEAD_STATUS


def dead_links(urls: Iterable[str], deadline_seconds: float | None = None) -> set[str]:
    """The links among these that answer 404 or 410.

    With a deadline, links not checked in time are treated as alive, so a slow
    network costs a few dead links rather than a search that times out.
    """
    now = time.monotonic()
    wanted = {url for url in urls if url and url.startswith("http")}
    dead: set[str] = set()
    unchecked = []
    with _cache_lock:
        for url in wanted:
            cached = _cache.get(url)
            if cached and now - cached[1] < CACHE_SECONDS:
                if cached[0]:
                    dead.add(url)
            else:
                unchecked.append(url)
    if not unchecked:
        return dead

    session = requests.Session()
    executor = ThreadPoolExecutor(max_workers=min(WORKERS, len(unchecked)))
    futures = {executor.submit(_is_dead, session, url): url for url in unchecked}
    try:
        for future in as_completed(futures, timeout=deadline_seconds):
            url = futures[future]
            answer = future.result()
            if answer is None:
                continue
            with _cache_lock:
                _cache[url] = (answer, time.monotonic())
            if answer:
                dead.add(url)
    except FuturesTimeout:
        logger.info("Checked %s of %s document links before the deadline",
                    sum(1 for future in futures if future.done()), len(unchecked))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return dead
