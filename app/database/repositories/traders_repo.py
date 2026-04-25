"""Top-trader repository — leaderboard storage & market lookups."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TopTrader, TraderPosition
from app.utils.time import days_ago, minutes_ago, utcnow


@dataclass
class ClusterRow:
    """Aggregated view of one ``(market_id, side)`` cluster inside a
    time window — produced by
    :meth:`TradersRepository.fetch_recent_clusters`.
    """

    market_id: str
    market_slug: Optional[str]
    side: str  # 'yes' / 'no'
    wallet_count: int
    total_conviction_usd: float
    first_observed_at: datetime
    last_observed_at: datetime


class TradersRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- Top traders -----------------------------------------------------

    async def upsert_trader(
        self,
        wallet_address: str,
        *,
        label: Optional[str] = None,
        roi_30d: Optional[Decimal] = None,
        winrate: Optional[Decimal] = None,
        volume_30d_usd: Optional[Decimal] = None,
        last_checked_at: Optional[datetime] = None,
    ) -> TopTrader:
        stmt = (
            pg_insert(TopTrader)
            .values(
                wallet_address=wallet_address,
                label=label,
                roi_30d=roi_30d,
                winrate=winrate,
                volume_30d_usd=volume_30d_usd,
                last_checked_at=last_checked_at,
            )
            .on_conflict_do_update(
                index_elements=[TopTrader.wallet_address],
                set_={
                    "label": label,
                    "roi_30d": roi_30d,
                    "winrate": winrate,
                    "volume_30d_usd": volume_30d_usd,
                    "last_checked_at": last_checked_at,
                },
            )
            .returning(TopTrader)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one()

    async def list_active(self) -> list[TopTrader]:
        res = await self.session.execute(
            select(TopTrader).where(TopTrader.is_active.is_(True))
        )
        return list(res.scalars())

    # ---- Positions -------------------------------------------------------

    async def record_positions(self, positions: Iterable[TraderPosition]) -> None:
        for p in positions:
            self.session.add(p)
        await self.session.flush()

    async def prune_positions_older_than(self, days: int = 7) -> int:
        """Delete ``trader_positions`` rows older than ``days``.

        Positions are re-fetched every ``TRADER_REFRESH_INTERVAL_SECONDS``
        (~5 min), so the table grows ~150 rows per wallet per cycle on
        its own.  Any row older than the cluster lookback window is
        dead weight — the scanner never reads that far back.
        """
        cutoff = days_ago(days)
        res = await self.session.execute(
            delete(TraderPosition).where(TraderPosition.observed_at < cutoff)
        )
        return int(res.rowcount or 0)

    async def recent_on_market(
        self, market_id: str, lookback_minutes: int = 120
    ) -> list[TraderPosition]:
        cutoff = minutes_ago(lookback_minutes)
        res = await self.session.execute(
            select(TraderPosition)
            .where(
                and_(
                    TraderPosition.market_id == market_id,
                    TraderPosition.observed_at >= cutoff,
                )
            )
            .order_by(TraderPosition.observed_at.desc())
        )
        return list(res.scalars())

    # ---- Cluster detection (smart-money follow) -------------------------

    async def fetch_recent_clusters(
        self,
        *,
        window_minutes: int,
        min_wallets: int,
        min_conviction_usd: float = 0.0,
        limit: int = 20,
    ) -> list[ClusterRow]:
        """Find ``(market, side)`` pairs where ``>= min_wallets`` distinct
        tracked wallets entered within the last ``window_minutes`` with
        combined size ``>= min_conviction_usd``.

        Returns the strongest clusters first (highest wallet count, then
        highest conviction USD).  One row per ``(market_id, side)``.
        """
        cutoff = minutes_ago(window_minutes)
        distinct_wallets = func.count(func.distinct(TraderPosition.trader_id)).label(
            "wallet_count"
        )
        total_usd = func.coalesce(
            func.sum(TraderPosition.size_usd), Decimal("0")
        ).label("total_conviction_usd")
        stmt = (
            select(
                TraderPosition.market_id,
                TraderPosition.market_slug,
                TraderPosition.side,
                distinct_wallets,
                total_usd,
                func.min(TraderPosition.observed_at).label("first_observed_at"),
                func.max(TraderPosition.observed_at).label("last_observed_at"),
            )
            .where(TraderPosition.observed_at >= cutoff)
            .group_by(
                TraderPosition.market_id,
                TraderPosition.market_slug,
                TraderPosition.side,
            )
            .having(distinct_wallets >= min_wallets)
            .having(total_usd >= Decimal(str(min_conviction_usd)))
            .order_by(distinct_wallets.desc(), total_usd.desc())
            .limit(limit)
        )
        res = await self.session.execute(stmt)
        out: list[ClusterRow] = []
        for (
            market_id,
            market_slug,
            side,
            wallet_count,
            total_conv,
            first_at,
            last_at,
        ) in res.all():
            out.append(
                ClusterRow(
                    market_id=market_id,
                    market_slug=market_slug,
                    side=side.value if hasattr(side, "value") else str(side),
                    wallet_count=int(wallet_count or 0),
                    total_conviction_usd=float(total_conv or 0),
                    first_observed_at=first_at or utcnow(),
                    last_observed_at=last_at or utcnow(),
                )
            )
        return out
