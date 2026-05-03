"""Trade repository — CRUD + daily counters."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    CloseReason,
    DailyCounter,
    Signal,
    Trade,
    TradeStatus,
)
from app.utils.time import utcnow


class TradesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- CRUD ------------------------------------------------------------

    async def create(self, trade: Trade) -> Trade:
        self.session.add(trade)
        await self.session.flush()
        return trade

    async def get(self, trade_id: int) -> Optional[Trade]:
        res = await self.session.execute(select(Trade).where(Trade.id == trade_id))
        return res.scalar_one_or_none()

    async def list_open(self, user_id: Optional[int] = None) -> list[Trade]:
        stmt = select(Trade).where(Trade.status == TradeStatus.OPEN)
        if user_id is not None:
            stmt = stmt.where(Trade.user_id == user_id)
        stmt = stmt.order_by(Trade.opened_at.desc())
        res = await self.session.execute(stmt)
        return list(res.scalars())

    async def list_all_open(self) -> list[Trade]:
        return await self.list_open(user_id=None)

    async def has_open_on_market(self, user_id: int, market_id: str) -> bool:
        res = await self.session.execute(
            select(Trade.id).where(
                Trade.user_id == user_id,
                Trade.market_id == market_id,
                Trade.status == TradeStatus.OPEN,
            )
        )
        return res.first() is not None

    async def count_open(self, user_id: int) -> int:
        res = await self.session.execute(
            select(func.count(Trade.id)).where(
                Trade.user_id == user_id, Trade.status == TradeStatus.OPEN
            )
        )
        return int(res.scalar_one() or 0)

    @staticmethod
    def _trade_is_non_crypto() -> object:
        return or_(
            Trade.signal_id.is_(None),
            Signal.category.is_(None),
            Signal.category != "crypto",
        )

    async def count_open_non_crypto(self, user_id: int) -> int:
        res = await self.session.execute(
            select(func.count(Trade.id))
            .select_from(Trade)
            .outerjoin(Signal, Trade.signal_id == Signal.id)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.OPEN,
                self._trade_is_non_crypto(),
            )
        )
        return int(res.scalar_one() or 0)

    async def count_open_crypto(self, user_id: int) -> int:
        res = await self.session.execute(
            select(func.count(Trade.id))
            .select_from(Trade)
            .join(Signal, Trade.signal_id == Signal.id)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.OPEN,
                Signal.category == "crypto",
            )
        )
        return int(res.scalar_one() or 0)

    async def list_open_non_crypto(self, user_id: int) -> list[Trade]:
        stmt = (
            select(Trade)
            .outerjoin(Signal, Trade.signal_id == Signal.id)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.OPEN,
                self._trade_is_non_crypto(),
            )
            .order_by(Trade.opened_at.desc())
        )
        res = await self.session.execute(stmt)
        return list(res.scalars())

    async def list_open_crypto(self, user_id: int) -> list[Trade]:
        stmt = (
            select(Trade)
            .join(Signal, Trade.signal_id == Signal.id)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.OPEN,
                Signal.category == "crypto",
            )
            .order_by(Trade.opened_at.desc())
        )
        res = await self.session.execute(stmt)
        return list(res.scalars())

    async def update_price(self, trade_id: int, price: Decimal, pnl: Decimal, pnl_pct: Decimal) -> None:
        await self.session.execute(
            update(Trade)
            .where(Trade.id == trade_id)
            .values(current_price=price, pnl=pnl, pnl_pct=pnl_pct)
        )

    async def update_trailing(
        self,
        trade_id: int,
        *,
        peak_price: Optional[Decimal] = None,
        trailing_active: Optional[bool] = None,
        exit_state: Optional[dict] = None,
    ) -> None:
        values: dict = {}
        if peak_price is not None:
            values["peak_price"] = peak_price
        if trailing_active is not None:
            values["trailing_active"] = trailing_active
        if exit_state is not None:
            values["exit_state"] = exit_state
        if not values:
            return
        await self.session.execute(
            update(Trade).where(Trade.id == trade_id).values(**values)
        )

    async def apply_partial(
        self,
        trade_id: int,
        *,
        new_shares: Decimal,
        new_amount_usd: Decimal,
        exit_state: dict,
        peak_price: Optional[Decimal] = None,
        trailing_active: Optional[bool] = None,
    ) -> None:
        """Persist the result of a partial close.

        The ``Trade`` row is mutated in place — there is no child table
        for partial fills; the JSON ``exit_state`` column carries the
        audit trail (``partials[]``).  ``shares`` and ``amount_usd``
        shrink with every rung of the ladder.
        """
        values: dict = {
            "shares": new_shares,
            "amount_usd": new_amount_usd,
            "exit_state": exit_state,
        }
        if peak_price is not None:
            values["peak_price"] = peak_price
        if trailing_active is not None:
            values["trailing_active"] = trailing_active
        await self.session.execute(
            update(Trade).where(Trade.id == trade_id).values(**values)
        )

    async def list_recent_closed(self, limit: int = 50) -> list[Trade]:
        res = await self.session.execute(
            select(Trade)
            .where(Trade.status == TradeStatus.CLOSED)
            .order_by(Trade.closed_at.desc())
            .limit(limit)
        )
        return list(res.scalars())

    async def close(
        self,
        trade_id: int,
        *,
        close_price: Decimal,
        pnl: Decimal,
        pnl_pct: Decimal,
        reason: CloseReason,
        exit_state: Optional[dict] = None,
    ) -> None:
        values: dict = {
            "status": TradeStatus.CLOSED,
            "current_price": close_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "close_reason": reason,
            "closed_at": utcnow(),
        }
        if exit_state is not None:
            values["exit_state"] = exit_state
        await self.session.execute(
            update(Trade).where(Trade.id == trade_id).values(**values)
        )

    async def mark_failed(self, trade_id: int) -> None:
        await self.session.execute(
            update(Trade)
            .where(Trade.id == trade_id)
            .values(status=TradeStatus.FAILED, closed_at=utcnow(), close_reason=CloseReason.ERROR)
        )

    # ---- Daily counters (anti-overtrading) -----------------------------

    async def get_today_count(self, user_id: int, day: Optional[date] = None) -> int:
        day = day or utcnow().date()
        res = await self.session.execute(
            select(DailyCounter.trades_count).where(
                DailyCounter.user_id == user_id, DailyCounter.day == day
            )
        )
        val = res.scalar_one_or_none()
        return int(val or 0)

    async def get_last_trade_at(self, user_id: int) -> Optional[datetime]:
        res = await self.session.execute(
            select(func.max(Trade.opened_at)).where(Trade.user_id == user_id)
        )
        return res.scalar_one_or_none()

    async def get_last_trade_at_non_crypto(self, user_id: int) -> Optional[datetime]:
        """Most recent ``opened_at`` among trades whose signal is **not** crypto."""
        res = await self.session.execute(
            select(func.max(Trade.opened_at))
            .select_from(Trade)
            .outerjoin(Signal, Trade.signal_id == Signal.id)
            .where(
                Trade.user_id == user_id,
                self._trade_is_non_crypto(),
            )
        )
        return res.scalar_one_or_none()

    async def get_last_close_on_market(
        self, user_id: int, market_id: str
    ) -> Optional[datetime]:
        """Return the most recent ``closed_at`` for this (user, market).

        Used by the no-re-entry control: after we exit a market, we
        refuse to reopen on it until the per-market cooldown has
        elapsed.  This prevents the "exit → spike → chase-back late →
        whip-saw out" loop.
        """
        res = await self.session.execute(
            select(func.max(Trade.closed_at)).where(
                Trade.user_id == user_id,
                Trade.market_id == market_id,
                Trade.status == TradeStatus.CLOSED,
            )
        )
        return res.scalar_one_or_none()

    async def bump_daily_counter(self, user_id: int, day: Optional[date] = None) -> None:
        day = day or utcnow().date()
        stmt = (
            pg_insert(DailyCounter)
            .values(user_id=user_id, day=day, trades_count=1, last_trade_at=utcnow())
            .on_conflict_do_update(
                index_elements=[DailyCounter.user_id, DailyCounter.day],
                set_={
                    "trades_count": DailyCounter.trades_count + 1,
                    "last_trade_at": utcnow(),
                },
            )
        )
        await self.session.execute(stmt)

    # ---- Portfolio metrics -------------------------------------------

    async def total_pnl(self, user_id: int) -> Decimal:
        res = await self.session.execute(
            select(func.coalesce(func.sum(Trade.pnl), 0)).where(Trade.user_id == user_id)
        )
        return Decimal(res.scalar_one() or 0)

    async def winrate(self, user_id: int) -> float:
        res = await self.session.execute(
            select(
                func.count(Trade.id).filter(Trade.pnl > 0),
                func.count(Trade.id).filter(Trade.status == TradeStatus.CLOSED),
            ).where(Trade.user_id == user_id)
        )
        wins, closed = res.one()
        closed = int(closed or 0)
        return round((int(wins or 0) / closed) * 100.0, 2) if closed else 0.0

    # ---- Feedback-loop support ---------------------------------------

    async def count_closed_with_feature_vector(self) -> int:
        """Count all closed trades whose ``feature_vector`` is populated.

        The feedback loop uses this as a cold-start gate: until enough
        labelled samples exist, weight updates are skipped.
        """
        res = await self.session.execute(
            select(func.count(Trade.id)).where(
                Trade.status == TradeStatus.CLOSED,
                Trade.feature_vector.is_not(None),
            )
        )
        return int(res.scalar_one() or 0)
