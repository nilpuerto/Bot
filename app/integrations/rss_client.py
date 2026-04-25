"""RSS fetcher.

Uses ``httpx`` for async transport (feedparser is sync) and then parses
the bytes with ``feedparser``.  Supports ETag / Last-Modified caching to
avoid re-downloading unchanged feeds.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx

from app.utils.logger import get_logger


logger = get_logger(__name__)


@dataclass
class NewsItem:
    title: str
    url: Optional[str]
    source: str
    published_at: Optional[datetime]
    summary: Optional[str] = None


@dataclass
class _FeedCache:
    etag: Optional[str] = None
    last_modified: Optional[str] = None


class RSSClient:
    """Asynchronously poll a set of RSS URLs."""

    USER_AGENT = "PrymSignals/1.0 (+https://github.com/)"

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._cache: dict[str, _FeedCache] = {}
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "RSSClient":
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": self.USER_AGENT, "Accept": "application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.5"},
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str) -> list[NewsItem]:
        assert self._client is not None, "RSSClient must be used as async context manager"

        headers: dict[str, str] = {}
        cache = self._cache.get(url)
        if cache:
            if cache.etag:
                headers["If-None-Match"] = cache.etag
            if cache.last_modified:
                headers["If-Modified-Since"] = cache.last_modified

        try:
            resp = await self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("rss_fetch_error", url=url, error=str(exc))
            return []

        if resp.status_code == 304:
            return []
        if resp.status_code != 200:
            logger.warning("rss_bad_status", url=url, status=resp.status_code)
            return []

        # Update cache
        self._cache[url] = _FeedCache(
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
        )

        # feedparser is CPU-bound; run in a thread so we don't block the loop.
        parsed = await asyncio.to_thread(feedparser.parse, resp.content)
        source = parsed.feed.get("title") or url
        items: list[NewsItem] = []
        for entry in parsed.entries:
            items.append(
                NewsItem(
                    title=(entry.get("title") or "").strip(),
                    url=entry.get("link"),
                    source=source,
                    published_at=_parse_entry_date(entry),
                    summary=entry.get("summary"),
                )
            )
        return items

    async def fetch_many(self, urls: list[str]) -> list[NewsItem]:
        tasks = [self.fetch(u) for u in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[NewsItem] = []
        for url, res in zip(urls, results):
            if isinstance(res, Exception):
                logger.warning("rss_exception", url=url, error=str(res))
                continue
            out.extend(res)
        return out


def _parse_entry_date(entry: dict) -> Optional[datetime]:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
