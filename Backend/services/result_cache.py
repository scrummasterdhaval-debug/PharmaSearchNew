"""A small, bounded store for search results the export will ask for again.

The search page keeps its results so that "Export All" can write the same rows
without running the search a second time. That store used to be a plain dict:
nothing was ever removed, every sort or filter click added an entry under a new
key, and each entry holds a whole result list. Server memory climbed from
193 MB to 309 MB over 24 searches and would have kept going for as long as the
server ran.

This keeps the same two operations the callers use -- ``cache[key] = rows``
and ``cache.get(key)`` -- but holds only the most recently used entries, and
lets an entry lapse once nobody could still be about to export it.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Callable, Hashable


class BoundedResultCache:
    def __init__(
        self,
        max_entries: int = 16,
        ttl_seconds: float = 30 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        # Routes run on a thread pool, so two searches can write at once.
        self._lock = Lock()

    def __setitem__(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._entries[key] = (self._clock(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return default
            stored_at, value = entry
            if self._clock() - stored_at > self.ttl_seconds:
                del self._entries[key]
                return default
            self._entries.move_to_end(key)
            return value

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
