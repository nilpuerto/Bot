"""Market-price history repository.

Feeds the mispricing z-score service.  Rows are appended every sampler
tick (default 60 s) for each market we actively care about.  A 30-day
window is considered the canonical rolling baseline.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MarketPriceHistory
from app.utils.time import utcnow


class MarketHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        market_id: str,
        price: Optional[float],
        volume_24h: Optional[float],
    ) -> None:
        row = MarketPriceHistory(
            market_id=market_id,
            price=Decimal(str(price)) if price is not None else None,
            volume_24h=Decimal(str(volume_24h)) if volume_24h is not None else None,
            observed_at=utcnow(),
        )
        self.session.add(row)
        await self.session.flush()

    async def prices_since(
        self, market_id: str, since: datetime
    ) -> Sequence[MarketPriceHistory]:
        res = await self.session.execute(
            select(MarketPriceHistory)
            .where(
                MarketPriceHistory.market_id == market_id,
                MarketPriceHistory.observed_at >= since,
            )
            .order_by(MarketPriceHistory.observed_at.asc())
        )
        return list(res.scalars())

    async def prices_last_days(
        self, market_id: str, days: int = 30
    ) -> Sequence[MarketPriceHistory]:
        return await self.prices_since(market_id, utcnow() - timedelta(days=days))

    async def prune_older_than(self, days: int = 60) -> int:
        """Keep the table bounded; rows older than ``days`` are pruned."""
        cutoff = utcnow() - timedelta(days=days)
        res = await self.session.execute(
            delete(MarketPriceHistory).where(MarketPriceHistory.observed_at < cutoff)
        )
        return int(res.rowcount or 0)
