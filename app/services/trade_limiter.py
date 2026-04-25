"""Trade limiter — the single choke point before any order is placed.

Philosophy: Prym Signals is a *precision* trader, not a volume trader.
If any of these rules trips, the trade is blocked, no exceptions:

* Max trades per day (per user).
* Cooldown since the last trade.
* No duplicate open trade on the same market.
* No re-entry on the same market for ``POST_CLOSE_REENTRY_SECONDS``
  after we close it (anti-whipsaw).
* No duplicate open trade on a "similar" market (normalized title match).
* Max concurrent opens.

Each check returns a structured :class:`LimiterDecision` so that the
caller can log / message exactly why a trade was refused.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings
from app.database.models import User
from app.database.repositories.trades_repo import TradesRepository
from app.database.session import session_scope
from app.utils.logger import get_logger
from app.utils.text import topic_slug
from app.utils.time import seconds_since, utcnow


logger = get_logger(__name__)


@dataclass
class LimiterDecision:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:  # convenience
        return self.allowed


class TradeLimiter:
    def __init__(
        self,
        cooldown_seconds: Optional[int] = None,
        max_open_trades: Optional[int] = None,
        post_close_reentry_seconds: Optional[int] = None,
    ) -> None:
        self.cooldown_seconds = (
            cooldown_seconds
            if cooldown_seconds is not None
            else settings.trade_cooldown_seconds
        )
        self.max_open_trades = (
            max_open_trades if max_open_trades is not None else settings.max_open_trades
        )
        self.post_close_reentry_seconds = (
            post_close_reentry_seconds
            if post_close_reentry_seconds is not None
            else settings.post_close_reentry_seconds
        )

    async def check(
        self, *, user: User, market_id: str, market_question: str
    ) -> LimiterDecision:
        async with session_scope() as session:
            repo = TradesRepository(session)

            # 1. Daily budget
            today_count = await repo.get_today_count(user.id)
            if today_count >= int(user.max_trades_per_day or settings.max_trades_per_day):
                return LimiterDecision(False, "daily_limit_reached")

            # 2. Cooldown
            last_trade_at = await repo.get_last_trade_at(user.id)
            if last_trade_at is not None:
                elapsed = seconds_since(last_trade_at)
                if elapsed < self.cooldown_seconds:
                    return LimiterDecision(
                        False,
                        f"cooldown_active_{int(self.cooldown_seconds - elapsed)}s",
                    )

            # 3. No duplicate on the exact same market
            if await repo.has_open_on_market(user.id, market_id):
                return LimiterDecision(False, "duplicate_market")

            # 4. No re-entry on a freshly closed market
            if self.post_close_reentry_seconds > 0:
                last_close = await repo.get_last_close_on_market(user.id, market_id)
                if last_close is not None:
                    elapsed = seconds_since(last_close)
                    if elapsed < self.post_close_reentry_seconds:
                        remaining = int(self.post_close_reentry_seconds - elapsed)
                        return LimiterDecision(
                            False, f"reentry_cooldown_active_{remaining}s"
                        )

            # 5. No duplicate on a "similar" market
            slug = topic_slug(market_question)
            if slug:
                open_trades = await repo.list_open(user.id)
                for t in open_trades:
                    if t.market_question and topic_slug(t.market_question) == slug:
                        return LimiterDecision(False, "similar_open_trade")

            # 6. Max concurrent
            open_count = await repo.count_open(user.id)
            if open_count >= self.max_open_trades:
                return LimiterDecision(False, "max_open_trades_reached")

        return LimiterDecision(True, "ok")

    async def register_trade(self, user_id: int) -> None:
        """Call after a trade is successfully opened."""
        async with session_scope() as session:
            await TradesRepository(session).bump_daily_counter(user_id)
        logger.info("trade_limiter_counter_bumped", user_id=user_id, at=utcnow().isoformat())
