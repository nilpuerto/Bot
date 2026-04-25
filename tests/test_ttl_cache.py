"""TTL cache used by the orchestrator to dedupe market lookups."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.utils.ttl_cache import TTLCache


def test_set_and_get_within_ttl() -> None:
    cache: TTLCache[int] = TTLCache(ttl_seconds=1.0)
    cache.set("k", 42)
    assert cache.get("k") == 42


def test_expires_after_ttl() -> None:
    cache: TTLCache[int] = TTLCache(ttl_seconds=0.05)
    cache.set("k", 1)
    time.sleep(0.07)
    assert cache.get("k") is None


@pytest.mark.asyncio
async def test_get_or_fetch_only_calls_fetcher_once() -> None:
    cache: TTLCache[int] = TTLCache(ttl_seconds=5.0)
    calls = 0

    async def fetch() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return 7

    a = await cache.get_or_fetch("k", fetch)
    b = await cache.get_or_fetch("k", fetch)
    assert a == 7 and b == 7
    assert calls == 1
