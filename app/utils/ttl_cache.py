"""Minimal async-friendly TTL cache used by the orchestrator to avoid
hammering the same Polymarket market endpoint when a news event affects
multiple users at once.

Not thread-safe; intended for single-loop usage.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Generic, Hashable, Optional, TypeVar


T = TypeVar("T")


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = float(ttl_seconds)
        self._store: dict[Hashable, tuple[float, T]] = {}

    def get(self, key: Hashable) -> Optional[T]:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: Hashable, value: T) -> None:
        self._store[key] = (time.monotonic() + self.ttl, value)

    def invalidate(self, key: Hashable) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    async def get_or_fetch(
        self,
        key: Hashable,
        fetcher: Callable[[], Awaitable[Any]],
    ) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = await fetcher()
        if value is not None:
            self.set(key, value)
        return value
