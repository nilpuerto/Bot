"""``PendingNewsQueue`` — TTL, dedup, eviction, FIFO snapshot."""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from app.services.pending_news import PendingNewsQueue


@dataclass
class _StubItem:
    title: str = "Headline"


@dataclass
class _StubIngested:
    hash: str
    item: _StubItem = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.item is None:
            self.item = _StubItem(title=f"Headline-{self.hash}")


@dataclass
class _StubAnalysis:
    market: str | None = "hint"


@pytest.mark.asyncio
async def test_add_is_deduplicated_by_hash() -> None:
    q = PendingNewsQueue(ttl_seconds=900, max_size=10)

    added_first = await q.add(_StubIngested("a"), _StubAnalysis())
    added_second = await q.add(_StubIngested("a"), _StubAnalysis())

    assert added_first is True
    assert added_second is False
    assert q.size == 1


@pytest.mark.asyncio
async def test_snapshot_returns_oldest_first() -> None:
    q = PendingNewsQueue(ttl_seconds=900, max_size=10)
    await q.add(_StubIngested("a"), _StubAnalysis())
    # Spin briefly so the second entry has a strictly later timestamp.
    time.sleep(0.01)
    await q.add(_StubIngested("b"), _StubAnalysis())

    snap = await q.snapshot()
    hashes = [e.hash for e in snap]
    assert hashes == ["a", "b"]


@pytest.mark.asyncio
async def test_max_size_evicts_oldest() -> None:
    q = PendingNewsQueue(ttl_seconds=900, max_size=2)
    await q.add(_StubIngested("a"), _StubAnalysis())
    time.sleep(0.01)
    await q.add(_StubIngested("b"), _StubAnalysis())
    time.sleep(0.01)
    await q.add(_StubIngested("c"), _StubAnalysis())

    snap = await q.snapshot()
    hashes = [e.hash for e in snap]
    assert "a" not in hashes
    assert hashes == ["b", "c"]


@pytest.mark.asyncio
async def test_evict_expired_drops_old_entries() -> None:
    q = PendingNewsQueue(ttl_seconds=1, max_size=10)
    await q.add(_StubIngested("old"), _StubAnalysis())
    # Force the entry to look ancient.
    entry = (await q.snapshot())[0]
    entry.first_seen_ts = time.time() - 60.0

    expired = await q.evict_expired()
    assert [e.hash for e in expired] == ["old"]
    assert q.size == 0


@pytest.mark.asyncio
async def test_drop_removes_entry() -> None:
    q = PendingNewsQueue(ttl_seconds=900, max_size=10)
    await q.add(_StubIngested("a"), _StubAnalysis())
    dropped = await q.drop("a")
    assert dropped is not None
    assert q.size == 0


@pytest.mark.asyncio
async def test_mark_attempt_increments_counter() -> None:
    q = PendingNewsQueue(ttl_seconds=900, max_size=10)
    await q.add(_StubIngested("a"), _StubAnalysis())
    await q.mark_attempt("a")
    await q.mark_attempt("a")

    snap = await q.snapshot()
    assert snap[0].attempts == 2
    assert snap[0].last_attempt_ts > 0
