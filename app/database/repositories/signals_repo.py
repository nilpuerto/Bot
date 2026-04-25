"""Signal repository — dedup, persistence, status transitions."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import NewsSeen, Signal, SignalStatus
from app.utils.time import utcnow


class SignalsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def has_seen(self, news_hash: str) -> bool:
        res = await self.session.execute(
            select(NewsSeen.hash).where(NewsSeen.hash == news_hash)
        )
        return res.scalar_one_or_none() is not None

    async def mark_seen(self, news_hash: str, source: Optional[str] = None) -> None:
        stmt = (
            pg_insert(NewsSeen)
            .values(hash=news_hash, source=source)
            .on_conflict_do_nothing(index_elements=[NewsSeen.hash])
        )
        await self.session.execute(stmt)

    async def create(self, signal: Signal) -> Signal:
        self.session.add(signal)
        await self.session.flush()
        return signal

    async def get(self, signal_id: int) -> Optional[Signal]:
        res = await self.session.execute(select(Signal).where(Signal.id == signal_id))
        return res.scalar_one_or_none()

    async def recent(self, limit: int = 10) -> list[Signal]:
        res = await self.session.execute(
            select(Signal).order_by(Signal.created_at.desc()).limit(limit)
        )
        return list(res.scalars())

    async def set_status(self, signal_id: int, status: SignalStatus) -> None:
        await self.session.execute(
            update(Signal).where(Signal.id == signal_id).values(status=status)
        )

    async def prune_news_seen_older_than(self, days: int = 7) -> int:
        """Drop ``news_seen`` rows older than ``days``.

        The table exists only for dedup + short-window corroboration.
        A week is plenty — anything older cannot realistically be
        "the same news event" and the hash index stays small.
        """
        cutoff = utcnow() - timedelta(days=days)
        res = await self.session.execute(
            delete(NewsSeen).where(NewsSeen.seen_at < cutoff)
        )
        return int(res.rowcount or 0)

    async def prune_expired_signals_older_than(self, days: int = 30) -> int:
        """Drop ``signals`` that never fired a trade and are stale.

        We only ever prune ``EXPIRED`` rows — anything that led to a
        trade is kept forever for the audit trail.
        """
        cutoff = utcnow() - timedelta(days=days)
        res = await self.session.execute(
            delete(Signal).where(
                Signal.status == SignalStatus.EXPIRED,
                Signal.created_at < cutoff,
            )
        )
        return int(res.rowcount or 0)

    # ---- v2 helpers ----------------------------------------------------

    async def recent_sources_for_title(
        self, normalized_title: str, lookback_minutes: int = 30
    ) -> list[str]:
        """Sources which reported a headline with the same canonical hash.

        Used by the DataQualityScorer to count corroborating outlets.
        """
        from app.utils.text import stable_hash

        cutoff = utcnow() - timedelta(minutes=lookback_minutes)
        h = stable_hash(normalized_title)
        res = await self.session.execute(
            select(NewsSeen.source)
            .where(NewsSeen.hash == h, NewsSeen.seen_at >= cutoff)
        )
        return [r for r in res.scalars() if r]

    async def recent_seen_sources(
        self, lookback_minutes: int = 30
    ) -> list[tuple[str, str]]:
        cutoff = utcnow() - timedelta(minutes=lookback_minutes)
        res = await self.session.execute(
            select(NewsSeen.hash, NewsSeen.source).where(NewsSeen.seen_at >= cutoff)
        )
        return [(h, s or "") for h, s in res.all()]

    async def count_secondary_since_midnight(self) -> int:
        """How many secondary-scout signals we produced today.

        Used as a global daily budget so the opportunistic scout never
        crowds out the primary news-driven trades.
        """
        return await self._count_by_source_today("secondary")

    async def count_cluster_since_midnight(self) -> int:
        """Daily budget counter for wallet-cluster signals."""
        return await self._count_by_source_today("cluster")

    async def _count_by_source_today(self, source: str) -> int:
        from datetime import time as _time

        today = utcnow().date()
        midnight = datetime.combine(today, _time(0, 0, 0), tzinfo=utcnow().tzinfo)
        res = await self.session.execute(
            select(func.count(Signal.id)).where(
                Signal.news_source == source,
                Signal.created_at >= midnight,
            )
        )
        return int(res.scalar_one() or 0)

    async def tracked_market_ids(
        self, lookback_minutes: int = 180, limit: int = 100
    ) -> Sequence[str]:
        """Markets attached to recent non-terminal signals — the sampler
        writes their prices into ``market_price_history`` every tick.
        """
        cutoff = utcnow() - timedelta(minutes=lookback_minutes)
        res = await self.session.execute(
            select(Signal.market_id)
            .where(
                Signal.market_id.is_not(None),
                Signal.created_at >= cutoff,
            )
            .order_by(Signal.created_at.desc())
            .limit(limit)
        )
        seen: list[str] = []
        for mid in res.scalars():
            if mid and mid not in seen:
                seen.append(mid)
        return seen
