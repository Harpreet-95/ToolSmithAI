"""
Request-scoped SQLite read session — Phase 3.2A / Tasks 1-3.

One instance per planning request. Opens exactly one sqlite3 connection
(PRAGMAs applied once, by data.db.get_connection), and is threaded down
through the planning call graph so hot-path helpers (search, candidate
hydration, synonym/snapshot lookups) reuse it instead of each opening their
own — profiling on real CCPP data showed 82 separate connections and 499
SQL statements in a single planning request, driven largely by the same
small set of lookups (latest schema snapshot, latest profiling snapshot,
the owning data source row) being independently re-resolved by several
unrelated modules.

NOT a shared/global connection. Each request constructs and owns exactly
one RequestMetadataSession and exactly one underlying sqlite3.Connection;
nothing here is process-wide state, so concurrent requests never share a
connection or cache entry — safety comes from isolation, not locking the
connection itself. The internal dicts (constants/search-result caches) are
lock-guarded only so a single session used across threads within one
request (not expected in this codebase's synchronous request handling
today, but cheap to guarantee) can't corrupt its own bookkeeping.

Every hot-path function accepting a session takes it as an optional
keyword-only parameter defaulting to None: nothing behaves differently
without one, so legacy callers (and any call site not yet threaded through
this session) keep opening their own short-lived connection exactly as
before.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional, TypeVar

from data.db import get_connection

T = TypeVar("T")


class MetadataSearchFailedError(Exception):
    """Raised when metadata-search infrastructure itself fails (e.g. the
    sqlite connection/session is unusable) — distinct from "no candidates
    matched", so callers can tell "the search subsystem is broken" from
    "the search subsystem worked and found nothing" (see Task 6: a prior
    change let a MemoryError during retrieval be silently swallowed and
    misreported as zero candidates)."""


class RequestMetadataSession:
    """Request-scoped connection + constant-lookup cache + search-result
    dedup map for one planning request. Use as a context manager so the
    connection is always closed, including on exception:

        with RequestMetadataSession(source_id, user_id) as session:
            ...
    """

    def __init__(self, source_id: int, user_id: str):
        self.source_id = source_id
        self.user_id = user_id
        self.conn = get_connection()
        self.connections_opened = 1  # this session's own connection

        self._lock = threading.Lock()
        self._constants: dict[str, object] = {}
        self._search_cache: dict[tuple, object] = {}
        self._closed = False

        # Task 2 instrumentation — request-scoped constant lookups.
        self.request_context_hits = 0
        self.request_context_misses = 0

        # Task 3 instrumentation — request-local search-result dedup.
        self.search_cache_hits = 0
        self.search_cache_misses = 0

        self.search_failed = False
        self.search_failed_reason: Optional[str] = None

    # -- constants (Task 2) --------------------------------------------------

    def get_or_compute(self, key: str, compute_fn: Callable[[], T]) -> T:
        """Resolve *key* once per request. compute_fn is called at most once
        per key per session, even if it returns None or a falsy value —
        presence in the dict, not truthiness, decides hit vs. miss."""
        with self._lock:
            if key in self._constants:
                self.request_context_hits += 1
                return self._constants[key]  # type: ignore[return-value]
        value = compute_fn()
        with self._lock:
            # Another thread may have raced this fill in; keep the first
            # value written so every caller in this request sees the same
            # constant, matching "request-scoped constant data".
            if key not in self._constants:
                self._constants[key] = value
            else:
                self.request_context_hits += 1
                return self._constants[key]  # type: ignore[return-value]
            self.request_context_misses += 1
        return value

    # -- search-result dedup map (Task 3) ------------------------------------

    def get_or_compute_search(self, key: tuple, compute_fn: Callable[[], T]) -> T:
        """Same contract as get_or_compute, in a separate namespace/dict —
        keyed by (source_id, normalized terms, search type/mode, filter
        options, metadata revision) by callers, so an identical search
        within this one request executes only once."""
        with self._lock:
            if key in self._search_cache:
                self.search_cache_hits += 1
                return self._search_cache[key]  # type: ignore[return-value]
        value = compute_fn()
        with self._lock:
            if key not in self._search_cache:
                self._search_cache[key] = value
            else:
                self.search_cache_hits += 1
                return self._search_cache[key]  # type: ignore[return-value]
            self.search_cache_misses += 1
        return value

    # -- infrastructure-failure signaling (Task 6) ---------------------------

    def mark_search_failed(self, reason: str) -> None:
        """Record that metadata-search infrastructure (not the semantic
        search itself) failed for this request — see MetadataSearchFailedError."""
        with self._lock:
            self.search_failed = True
            self.search_failed_reason = reason

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.conn.close()
        finally:
            self._closed = True

    def __enter__(self) -> "RequestMetadataSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False  # never suppress exceptions
