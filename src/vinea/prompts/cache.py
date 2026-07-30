"""An in-process TTL cache that serves stale-while-revalidate.

The registry must never sit on the hot path of a single advisory run
A slow or momentarily-down registry should add latency to the
*next* refresh, not to the request in front of a grower. So a fetched template
is cached with a short TTL, and past that TTL it is served *stale* while a
refresh happens in the background -- the grower's request gets yesterday's
wording instantly rather than waiting on the network.

Deliberately tiny and dependency-free: a dict, a lock, and a timestamp. The
lesson is the *policy* (fresh -> serve; stale -> serve-and-revalidate; absent
-> the caller falls through to fetch/bundled), not a caching library.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class CacheEntry:
    template: str
    version: str | None
    fetched_at: float


class PromptCache:
    """Thread-safe TTL cache with a fresh/stale distinction.

    `ttl_seconds` is the freshness window. Inside it, an entry is fresh and
    served directly. Past it, the entry is *stale* -- still returned, but the
    caller is expected to kick off a background revalidation (that's the SWR in
    the name; this class holds the state, registry.py runs the refresh).
    """

    def __init__(self, ttl_seconds: float = 60.0, *, clock=time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, CacheEntry] = {}

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            return self._entries.get(key)

    def is_fresh(self, entry: CacheEntry) -> bool:
        return (self._clock() - entry.fetched_at) < self._ttl

    def put(self, key: str, template: str, version: str | None = None) -> None:
        with self._lock:
            self._entries[key] = CacheEntry(
                template=template, version=version, fetched_at=self._clock()
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
