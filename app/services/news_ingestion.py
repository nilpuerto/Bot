"""News ingestion — orchestrates RSS polling, dedup, hard filter AND the
Data-Quality gate.

Exposes an async generator ``stream()`` that yields only news items which:

1. Passed the cheap deterministic :class:`HardFilter` (keywords, source
   allowlist, freshness);
2. Were not seen before (``news_seen`` dedup);
3. Scored >= ``settings.dq_min_score`` on the
   :class:`~app.services.data_quality.DataQualityScorer`.

Every yielded :class:`IngestedNews` now carries the computed
:class:`DQScore` so downstream scoring can boost or penalise signals
proportionally to data quality — we don't throw the information away
after the gate.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from app.config.settings import settings
from app.database.repositories.signals_repo import SignalsRepository
from app.database.session import session_scope
from app.integrations.rss_client import NewsItem, RSSClient
from app.services.data_quality import (
    DataQualityScorer,
    DQScore,
    count_corroborators,
)
from app.services.hard_filter import HardFilter
from app.utils.logger import get_logger
from app.utils.text import stable_hash


logger = get_logger(__name__)


@dataclass
class IngestedNews:
    item: NewsItem
    hash: str
    dq: DQScore


class NewsIngestionService:
    def __init__(
        self,
        feeds: Optional[list[str]] = None,
        poll_interval_seconds: Optional[int] = None,
        filter_: Optional[HardFilter] = None,
        quality_scorer: Optional[DataQualityScorer] = None,
    ) -> None:
        self._feeds = feeds if feeds is not None else settings.rss_feeds
        self._interval = poll_interval_seconds or settings.news_poll_interval_seconds
        self._filter = filter_ or HardFilter()
        self._dq = quality_scorer or DataQualityScorer()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def stream(self) -> AsyncIterator[IngestedNews]:
        if not self._feeds:
            logger.warning("news_ingestion_no_feeds_configured")
            return

        async with RSSClient() as rss:
            while not self._stop.is_set():
                batch: list[NewsItem] = []
                try:
                    batch = await rss.fetch_many(self._feeds)
                except Exception as exc:  # defensive — never let the loop die
                    logger.exception("news_fetch_exception", error=str(exc))

                logger.info("news_fetched", count=len(batch))

                fresh_items = await self._process_batch(batch)
                for ingested in fresh_items:
                    yield ingested

                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass

    async def _process_batch(self, items: list[NewsItem]) -> list[IngestedNews]:
        out: list[IngestedNews] = []
        if not items:
            return out

        seen_in_batch: set[str] = set()
        async with session_scope() as session:
            repo = SignalsRepository(session)

            # Pre-load the rolling corroboration window once per batch.
            recent = await repo.recent_seen_sources(
                lookback_minutes=settings.dq_corroboration_window_minutes
            )
            by_hash: dict[str, list[str]] = {}
            for h, src in recent:
                by_hash.setdefault(h, []).append(src)

            for item in items:
                if not item.title:
                    continue
                h = stable_hash(item.title)
                if h in seen_in_batch:
                    continue
                seen_in_batch.add(h)

                if await repo.has_seen(h):
                    continue

                # 1. Deterministic hard filter (keywords/age/source).
                hf = self._filter.evaluate(item)
                if not hf.passed:
                    await repo.mark_seen(h, source=item.source)
                    logger.debug(
                        "news_filtered_out",
                        reason=hf.reason,
                        title=item.title[:120],
                    )
                    continue

                # 2. Data-quality score.
                corroborators = count_corroborators(
                    by_hash.get(h, []), candidate_source=item.source
                )
                dq = self._dq.score(item, corroborators=corroborators)

                # Always mark seen, even for rejected items, so the next
                # batch doesn't re-score them.  This is cheap and bounded.
                await repo.mark_seen(h, source=item.source)

                if not dq.passed:
                    logger.info(
                        "news_rejected_low_quality",
                        title=item.title[:120],
                        score=dq.total,
                        source=item.source,
                        source_tier=dq.details.get("source_tier"),
                    )
                    continue

                logger.info(
                    "news_passed_filter",
                    title=item.title[:120],
                    source=item.source,
                    dq=dq.total,
                    corroborators=corroborators,
                )
                out.append(IngestedNews(item=item, hash=h, dq=dq))
        return out
