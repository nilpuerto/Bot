"""Pending-news queue — keep good news alive until a market exists.

When a fresh, AI-tagged news item arrives we sometimes can't find a
matching Polymarket market *yet* — Polymarket lists the market a few
seconds or minutes after the headline crosses the wires, so an
immediate match attempt returns ``None`` and the bot used to drop the
opportunity forever.  That throws away the whole point of being early.

This queue keeps such news alive in memory for ``ttl_seconds`` and
exposes it to a periodic retry loop in the orchestrator.  Each retry
runs the same matching + execution pipeline, but against the *current*
market universe, so as soon as Polymarket lists the market the bot can
still enter at the early price.

Design constraints:

* Bounded — ``max_size`` is enforced by evicting the oldest entry on
  overflow, so a runaway news stream cannot grow this unbounded.
* Idempotent — repeated ``add`` calls for the same ``news_hash`` are
  deduplicated; we never re-analyse the same headline twice.
* Lock-protected — ``add`` / ``snapshot`` / ``drop`` are safe to call
  concurrently from the news consumer and the retry loop.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from app.integrations.mistral_client import AIAnalysis
from app.services.news_ingestion import IngestedNews
from app.utils.logger import get_logger


logger = get_logger(__name__)


@dataclass
class PendingEntry:
    """One news item waiting for a tradeable market to appear.

    ``first_seen_ts`` is wall-clock seconds so callers can compute age
    against ``time.time()`` without importing extra modules.
    """

    ingested: IngestedNews
    analysis: AIAnalysis
    first_seen_ts: float
    attempts: int = 0
    last_attempt_ts: float = 0.0

    @property
    def hash(self) -> str:
        return self.ingested.hash

    def age_seconds(self) -> float:
        return time.time() - self.first_seen_ts


@dataclass
class PendingNewsQueue:
    """Bounded in-memory queue of news awaiting a market match.

    Public methods are async to share a single :class:`asyncio.Lock`
    with the retry loop and the news consumer.
    """

    ttl_seconds: int = 900
    max_size: int = 200
    _items: dict[str, PendingEntry] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def size(self) -> int:
        return len(self._items)

    async def add(self, ingested: IngestedNews, analysis: AIAnalysis) -> bool:
        """Enqueue a news item.  Returns ``True`` if newly added."""
        async with self._lock:
            if ingested.hash in self._items:
                return False
            if len(self._items) >= self.max_size:
                oldest_hash = min(
                    self._items, key=lambda h: self._items[h].first_seen_ts
                )
                evicted = self._items.pop(oldest_hash, None)
                if evicted is not None:
                    logger.debug(
                        "pending_news_evicted_oldest",
                        title=evicted.ingested.item.title[:80],
                        age_s=int(evicted.age_seconds()),
                    )
            self._items[ingested.hash] = PendingEntry(
                ingested=ingested,
                analysis=analysis,
                first_seen_ts=time.time(),
            )
            logger.info(
                "pending_news_enqueued",
                title=ingested.item.title[:80],
                queue_size=len(self._items),
            )
            return True

    async def snapshot(self) -> list[PendingEntry]:
        """Return a list copy of the queue ordered oldest-first."""
        async with self._lock:
            return sorted(self._items.values(), key=lambda e: e.first_seen_ts)

    async def drop(self, news_hash: str) -> Optional[PendingEntry]:
        async with self._lock:
            return self._items.pop(news_hash, None)

    async def mark_attempt(self, news_hash: str) -> None:
        async with self._lock:
            entry = self._items.get(news_hash)
            if entry is not None:
                entry.attempts += 1
                entry.last_attempt_ts = time.time()

    async def evict_expired(self) -> list[PendingEntry]:
        """Drop and return every entry past its TTL."""
        now = time.time()
        async with self._lock:
            expired = [
                e
                for e in self._items.values()
                if (now - e.first_seen_ts) > self.ttl_seconds
            ]
            for entry in expired:
                self._items.pop(entry.hash, None)
        return expired

    async def clear(self) -> None:
        async with self._lock:
            self._items.clear()
