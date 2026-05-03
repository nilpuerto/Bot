"""Trade limiter — the single choke point before any order is placed.

Philosophy: Prym Signals is a *precision* trader, not a volume trader.

**Two independent pipelines**

* **News / cluster** (default): uses ``MAX_TRADES_PER_DAY`` + per-user DB cap,
  cooldown, duplicate market, re-entry delay, *similar-title* guard, and a
  concurrent cap that counts **only** non-crypto open trades.

* **Crypto** (``Signal.category == \"crypto\"``): does **not** consume the
  global daily counter or cooldown, and measures concurrency with
  ``CRYPTO_MAX_OPEN_TRADES`` counting **only** crypto opens.  The crypto
  orchestrator applies its own per-horizon limits (see ``CRYPTO_1H_MAX_TRADES``,
  etc.).

Duplicate-market, market re-entry, and similarity rules still apply to both —
they operate on IDs / questions, not pipelines.
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
        self,
        *,
        user: User,
        market_id: str,
        market_question: str,
        is_crypto: bool = False,
    ) -> LimiterDecision:
        async with session_scope() as session:
            repo = TradesRepository(session)

            # 1–2 News pipeline only — daily ticket + cooldown
            if not is_crypto:
                cfg_cap = int(settings.max_trades_per_day)
                user_cap_raw = getattr(user, "max_trades_per_day", None)
                user_cap = int(user_cap_raw) if user_cap_raw is not None else 0
                daily_cap = max(cfg_cap, user_cap) if user_cap > 0 else cfg_cap
                today_count = await repo.get_today_count(user.id)
                if today_count >= daily_cap:
                    return LimiterDecision(False, "daily_limit_reached")

                last_trade_at = await repo.get_last_trade_at_non_crypto(user.id)
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

            # 5. No duplicate on a "similar" market (same pipeline bucket)
            slug = topic_slug(market_question)
            if slug:
                if is_crypto:
                    open_trades = await repo.list_open_crypto(user.id)
                else:
                    open_trades = await repo.list_open_non_crypto(user.id)
                for t in open_trades:
                    if t.market_question and topic_slug(t.market_question) == slug:
                        return LimiterDecision(False, "similar_open_trade")

            # 6. Max concurrent (split by pipeline)
            if is_crypto:
                open_count = await repo.count_open_crypto(user.id)
                cap = int(settings.crypto_max_open_trades)
                if open_count >= cap:
                    return LimiterDecision(False, "crypto_max_open_trades_reached")
            else:
                open_count = await repo.count_open_non_crypto(user.id)
                if open_count >= self.max_open_trades:
                    return LimiterDecision(False, "max_open_trades_reached")

        return LimiterDecision(True, "ok")

    async def register_trade(self, user_id: int, *, bump_global_daily: bool = True) -> None:
        """Call after a trade is successfully opened.

        ``bump_global_daily=False`` for crypto — it must not consume the news
        ``DailyCounter`` budget.
        """
        if not bump_global_daily:
            return
        async with session_scope() as session:
            await TradesRepository(session).bump_daily_counter(user_id)
        logger.info("trade_limiter_counter_bumped", user_id=user_id, at=utcnow().isoformat())
