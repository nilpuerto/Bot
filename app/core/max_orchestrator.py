"""MAX Mode orchestrator.

Owns the lifecycle of :class:`~app.services.max_sniper.MaxSniper` and
the per-snipe trade execution path for users in
:attr:`~app.database.models.UserMode.MAX`.

Responsibilities:

* Spin up one shared :class:`~app.integrations.crypto_price_feed.CryptoPriceFeed`
  and :class:`~app.services.ta_confluence.CandleCache` (we *reuse* the
  ones from the existing :class:`~app.core.crypto_orchestrator.CryptoOrchestrator`
  when both modes coexist; otherwise we own them).
* Schedule the sniper which fires on each 5-minute boundary.
* On fire, for every active MAX user:
    * Compute their cumulative MAX-mode profit (used by
      :func:`~app.services.max_sizer.size_for_entry` to set the bet).
    * Skip if they already have an open position on this market.
    * Persist a synthetic Signal (category="max"), open the trade
      through the shared :class:`~app.services.trade_executor.TradeExecutor`,
      and notify Telegram.

We deliberately do *not* monitor / auto-close MAX trades — the existing
:class:`~app.services.trade_monitor.TradeMonitor` already handles them
via the standard exit rules (or you can flip the ``MAX_AUTO_MONITOR``
flag off if you want pure expire-at-close behaviour).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select

from app.config.settings import settings
from app.database.models import Signal, Trade, TradeStatus, User, UserMode
from app.database.repositories.signals_repo import SignalsRepository
from app.database.repositories.trades_repo import TradesRepository
from app.database.repositories.users_repo import UsersRepository
from app.database.session import session_scope
from app.integrations.crypto_price_feed import CryptoPriceFeed
from app.integrations.polymarket_chainlink_feed import PolymarketChainlinkBTC
from app.integrations.polymarket_client import PolymarketClient
from app.services.balance import LiveBalanceProvider
from app.services.max_sizer import size_for_entry
from app.services.max_sniper import MaxSniper, SnipeWindow
from app.services.max_strategy import MaxSignal
from app.services.ta_confluence import CandleCache
from app.services.trade_executor import TradeExecutor
from app.telegram.formatters import (
    max_entry_card,
    max_mode_switched,
)
from app.utils.logger import get_logger
from app.utils.text import stable_hash
from app.utils.time import utcnow


logger = get_logger(__name__)


@dataclass
class _SyntheticMaxSignal:
    """Adapter mirroring :class:`~app.services.trade_executor` expectations."""

    id: int
    feature_vector: dict
    category: str = "max"


class MaxOrchestrator:
    """End-to-end MAX Mode loop."""

    def __init__(
        self,
        *,
        polymarket: PolymarketClient,
        executor: TradeExecutor,
        balance: LiveBalanceProvider,
        bot,
        feed: Optional[CryptoPriceFeed] = None,
        candles: Optional[CandleCache] = None,
    ) -> None:
        self._poly = polymarket
        self._executor = executor
        self._balance = balance
        self._bot = bot
        self._owns_feed = feed is None
        self._owns_candles = candles is None
        self._feed = feed or CryptoPriceFeed()
        self._candles = candles or CandleCache()
        self._chainlink_oracle = (
            PolymarketChainlinkBTC() if settings.max_chainlink_oracle_enabled else None
        )
        self._sniper = MaxSniper(
            polymarket=polymarket,
            feed=self._feed,
            candles=self._candles,
            oracle=self._chainlink_oracle,
            on_snipe=self._on_snipe,
        )
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._open_usd_by_user: dict[int, float] = {}
        self._already_notified_mode: set[int] = set()

    async def start(self) -> None:
        if not settings.max_mode_enabled:
            logger.info("max_orchestrator_disabled")
            return
        if self._owns_feed:
            await self._feed.start()
        if self._chainlink_oracle is not None:
            await self._chainlink_oracle.start()
        self._tasks.append(
            asyncio.create_task(self._sniper.run(), name="max_sniper")
        )
        self._tasks.append(
            asyncio.create_task(self._mode_announcement_loop(), name="max_mode_announce")
        )
        logger.info("max_orchestrator_started")

    async def stop(self) -> None:
        self._stop.set()
        self._sniper.stop()
        if self._owns_feed:
            try:
                await self._feed.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("max_feed_stop_error", error=str(exc))
        if self._chainlink_oracle is not None:
            try:
                await self._chainlink_oracle.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("max_chainlink_stop_error", error=str(exc))
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        logger.info("max_orchestrator_stopped")

    # ---- snipe handler --------------------------------------------------

    async def _on_snipe(self, window: SnipeWindow, sig: MaxSignal) -> None:
        if window.market is None:
            logger.warning(
                "max_skip", reason="no_market", window_ts=window.window_ts
            )
            return
        users = await self._max_users()
        if not users:
            logger.debug("max_skip", reason="no_users", window_ts=window.window_ts)
            return

        token_id = window.market.yes_token_id if sig.side == "yes" else window.market.no_token_id
        if not token_id:
            logger.info(
                "max_skip",
                reason="no_token_id",
                window_ts=window.window_ts,
                slug=window.market.slug,
            )
            return

        ask = await self._best_ask(token_id)
        if ask is None or ask <= 0:
            if not settings.max_use_limit_fallback:
                logger.info(
                    "max_skip",
                    reason="no_liquidity",
                    window_ts=window.window_ts,
                    slug=window.market.slug,
                )
                return
            ask = float(settings.max_limit_fallback_price)
            logger.info(
                "max_limit_fallback",
                window_ts=window.window_ts,
                price=ask,
                slug=window.market.slug,
            )

        upside = 1.0 - float(ask)
        if upside < float(settings.max_min_token_upside):
            logger.info(
                "max_skip",
                reason="bad_token_upside",
                window_ts=window.window_ts,
                ask=ask,
                min_upside=float(settings.max_min_token_upside),
            )
            return

        is_decisive = "decisive" in " ".join(sig.reasons)
        if settings.max_relaxed_entry_decisive_only:
            eff_cap = (
                float(settings.max_relaxed_max_entry_price)
                if is_decisive
                else float(settings.max_max_entry_price)
            )
        else:
            eff_cap = float(settings.max_relaxed_max_entry_price)

        if ask >= eff_cap:
            logger.info(
                "max_skip",
                reason="ask_too_high",
                window_ts=window.window_ts,
                ask=ask,
                cap=eff_cap,
            )
            return

        for user in users:
            await self._execute(
                user=user,
                window=window,
                sig=sig,
                ask=ask,
                is_decisive=is_decisive,
            )

    async def _execute(
        self,
        *,
        user: User,
        window: SnipeWindow,
        sig: MaxSignal,
        ask: float,
        is_decisive: bool,
    ) -> None:
        breakdown = await self._balance.effective_balance(user)
        balance = float(breakdown.effective)
        if balance <= 0:
            logger.info("max_skip", reason="zero_balance", user_id=user.id)
            return

        cum_profit = await self._cumulative_max_profit(user.id)
        open_usd = self._open_usd_by_user.get(user.id, 0.0)
        sizing = size_for_entry(
            balance=balance,
            cumulative_profit=cum_profit,
            confidence=sig.confidence,
            currently_open_usd=open_usd,
            is_window_decisive=is_decisive,
            deadline_forced=window.deadline_forced,
            window_delta_abs_pct=abs(float(sig.window_delta_pct)),
        )
        if sizing.amount_usd <= 0:
            logger.info(
                "max_skip",
                reason=f"sizing_{sizing.reason}",
                user_id=user.id,
                cap=round(sizing.cap_usd, 2),
                balance=round(sizing.bankroll, 2),
                profit=round(sizing.cumulative_profit, 2),
            )
            return

        plan = sizing.to_plan(entry_price=ask, band="max_snipe")
        signal = await self._persist_signal(window=window, sig=sig, ask=ask)
        result = await self._executor.open_trade(
            user=user,
            signal=signal,
            market=window.market,  # type: ignore[arg-type]
            side=sig.side,
            plan=plan,
        )
        if not result.ok:
            logger.warning(
                "max_trade_failed",
                user_id=user.id,
                reason=result.reason,
                window_ts=window.window_ts,
            )
            return

        self._open_usd_by_user[user.id] = open_usd + sizing.amount_usd
        await self._notify_entry(
            user=user,
            window=window,
            sig=sig,
            ask=ask,
            sizing=sizing,
        )

    # ---- helpers --------------------------------------------------------

    async def _max_users(self) -> list[User]:
        async with session_scope() as session:
            users = await UsersRepository(session).list_allowed()
        return [u for u in users if u.mode == UserMode.MAX and u.is_active]

    async def _best_ask(self, token_id: str) -> Optional[float]:
        book = await self._poly.get_order_book(token_id)
        if book is None:
            return None
        return book.best_ask

    async def _cumulative_max_profit(self, user_id: int) -> float:
        """Sum of ``Trade.pnl`` for closed MAX trades belonging to ``user_id``.

        We restrict to ``Signal.category='max'`` so existing crypto/news
        P&L is not pulled in.  Open trades are intentionally excluded —
        their unrealised P&L is too noisy to bet against.
        """
        async with session_scope() as session:
            stmt = (
                select(func.coalesce(func.sum(Trade.pnl), 0))
                .select_from(Trade)
                .join(Signal, Trade.signal_id == Signal.id)
                .where(
                    Trade.user_id == user_id,
                    Trade.status == TradeStatus.CLOSED,
                    Signal.category == "max",
                )
            )
            res = await session.execute(stmt)
            return float(res.scalar_one() or 0)

    async def _persist_signal(
        self, *, window: SnipeWindow, sig: MaxSignal, ask: float
    ) -> _SyntheticMaxSignal:
        from app.database.models import SignalImpact, SignalStatus

        market = window.market
        assert market is not None
        feature_vector = {
            "source": "max",
            "window_ts": window.window_ts,
            "window_open": window.open_price,
            "score": round(sig.score, 4),
            "confidence": round(sig.confidence, 4),
            "window_delta_pct": round(sig.window_delta_pct, 4),
            "reasons": sig.reasons,
            "ask": round(ask, 6),
        }
        impact = SignalImpact.BULLISH if sig.side == "yes" else SignalImpact.BEARISH
        signal = Signal(
            news_title=(
                f"[MAX] BTC 5m {sig.side.upper()} "
                f"score={sig.score:+.2f} conf={sig.confidence:.2f}"
            ),
            news_url=None,
            news_source="max_mode",
            news_published_at=utcnow(),
            news_hash=stable_hash(
                f"max:{market.id}:{sig.side}:{int(time.time())}"
            ),
            market_id=market.id,
            market_question=market.question,
            market_slug=market.slug,
            market_price=Decimal(str(ask)),
            market_volume_24h=Decimal(str(market.volume_24h or 0)),
            impact=impact,
            urgency=9,
            ai_raw=None,
            score=Decimal("85"),
            trader_confirmation=False,
            trader_aligned_count=0,
            trader_conviction_usd=Decimal("0"),
            status=SignalStatus.SENT,
            quality_score=None,
            category="max",
            magnitude=0,
            rarity=0,
            timing_phase=1,
            mispricing_z=None,
            liquidity_score=None,
            expected_edge_pct=Decimal("0"),
            slippage_bps=Decimal(str(settings.crypto_slippage_bps)),
            entities=["bitcoin"],
            feature_vector=feature_vector,
        )
        async with session_scope() as session:
            signal = await SignalsRepository(session).create(signal)
        return _SyntheticMaxSignal(id=signal.id, feature_vector=feature_vector)

    async def _mode_announcement_loop(self) -> None:
        while not self._stop.is_set():
            try:
                users = await self._max_users()
                for u in users:
                    if u.id in self._already_notified_mode:
                        continue
                    if not u.notifications_enabled:
                        self._already_notified_mode.add(u.id)
                        continue
                    try:
                        await self._bot.send_message(
                            chat_id=u.telegram_id, text=max_mode_switched()
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "max_mode_announce_failed",
                            user_id=u.id,
                            error=str(exc),
                        )
                    self._already_notified_mode.add(u.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("max_mode_announce_loop_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _notify_entry(
        self,
        *,
        user: User,
        window: SnipeWindow,
        sig: MaxSignal,
        ask: float,
        sizing,
    ) -> None:
        if not user.notifications_enabled:
            return
        market = window.market
        assert market is not None
        seconds_left = int((window.close_at - utcnow()).total_seconds())
        text = max_entry_card(
            side=sig.side,
            entry_price=ask,
            size_usd=sizing.amount_usd,
            balance=sizing.bankroll,
            cumulative_profit=sizing.cumulative_profit,
            confidence=sig.confidence,
            window_delta_pct=sig.window_delta_pct,
            reasons=sig.reasons[:5],
            seconds_left=seconds_left,
            fallback_used=sizing.fallback_used,
            slug=market.slug,
        )
        try:
            await self._bot.send_message(chat_id=user.telegram_id, text=text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("max_notify_entry_failed", error=str(exc))


__all__ = ["MaxOrchestrator"]
