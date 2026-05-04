"""Crypto Mode orchestrator — the BTC binary lag-arb engine.

This module is the only producer of trades for users in
:attr:`~app.database.models.UserMode.CRYPTO`.  It is wired into the
existing :class:`~app.core.orchestrator.Orchestrator.start` lifecycle
as one more asyncio task and is fully independent of the news /
cluster pipelines.

End-to-end flow on each new BTC market discovered by the scanner::

    spot = price_feed.snapshot()                    # Binance + Coinbase median
    if not spot.is_fresh:           skip(feed_stale)
    if not spot.is_warm:            skip(feed_warming)

    p_fair = lag_arb.fair_prob_above(spot, strike, sigma, T)
    book   = polymarket.get_order_book(yes_token_id)  # + no_token_id
    quote  = lag_arb.choose_side(p_fair, ask_yes, ask_no, fee, slip)
    if quote is None or quote.edge_pct < CRYPTO_MIN_EDGE_PCT:  skip(no_edge)

    ta gate: skip(low_confluence) only when min_conf for horizon is > 0

    overlay = news_overlay.modifier(side, horizon)
    if overlay.action == "veto":                                skip(news_veto)

    sizing = crypto_sizer.first_entry_size(balance, edge, open_usd) * overlay.scale
    open_trade(...)
    schedule(_late_scoop_for(market))
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings
from app.database.models import User, UserMode
from app.database.repositories.users_repo import UsersRepository
from app.database.session import session_scope
from app.integrations.crypto_price_feed import CryptoPriceFeed, PriceSnapshot
from app.integrations.polymarket_client import PolymarketClient
from app.services.balance import LiveBalanceProvider
from app.services.crypto_market_scanner import CryptoMarket, CryptoMarketScanner, Horizon
from app.services.crypto_news_overlay import CryptoNewsOverlay
from app.services.crypto_sizer import (
    CryptoSizing,
    first_entry_size,
    late_scoop_size,
)
from app.services.lag_arb_pricer import (
    EdgeQuote,
    choose_side,
    edge_diagnostic,
    fair_prob_above,
)
from app.services.ta_confluence import CandleCache, score as ta_score
from app.services.trade_executor import TradeExecutor
from app.telegram.formatters import (
    crypto_entry_card,
    crypto_late_scoop_card,
    crypto_mode_switched,
)
from app.utils.logger import get_logger
from app.utils.time import utcnow


logger = get_logger(__name__)


# Sentinel "synthetic" Signal we attach to crypto trades so the existing
# TradeExecutor can persist them without touching the news pipeline.  We
# must stay schema-compatible with :class:`~app.database.models.Signal`.


@dataclass
class _SyntheticSignal:
    """Adapter so we can call ``TradeExecutor.open_trade`` without a DB Signal.

    The executor reads ``signal.id``, ``signal.feature_vector`` and
    ``signal.category`` (the last one is how it routes the trade through
    the crypto branch of :class:`~app.services.trade_limiter.TradeLimiter`,
    bypassing the news daily cap and cooldown).
    """

    id: int
    feature_vector: dict
    category: str = "crypto"


class CryptoOrchestrator:
    """End-to-end Crypto Mode loop."""

    def __init__(
        self,
        *,
        polymarket: PolymarketClient,
        executor: TradeExecutor,
        balance: LiveBalanceProvider,
        bot,  # ``telegram.Bot``; kept untyped to stay test-friendly
    ) -> None:
        self._poly = polymarket
        self._executor = executor
        self._balance = balance
        self._bot = bot

        self._feed = CryptoPriceFeed()
        self._scanner = CryptoMarketScanner(polymarket)
        self._candles = CandleCache()
        self._overlay = CryptoNewsOverlay()

        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._open_usd_by_user: dict[int, float] = {}
        self._daily_taken: dict[tuple[int, str], int] = {}  # (user_id, "YYYY-MM-DD")
        self._daily_taken_1h: dict[tuple[int, str], int] = {}
        self._already_notified_mode: set[int] = set()

    # ---- public access for outside ingest -------------------------------

    @property
    def overlay(self) -> CryptoNewsOverlay:
        return self._overlay

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        if not settings.crypto_mode_enabled:
            logger.info("crypto_orchestrator_disabled")
            return
        await self._feed.start()
        self._tasks.append(
            asyncio.create_task(self._scanner.run(self._on_new_market), name="crypto_scanner")
        )
        self._tasks.append(
            asyncio.create_task(self._mode_announcement_loop(), name="crypto_mode_announce")
        )
        logger.info("crypto_orchestrator_started")

    async def stop(self) -> None:
        self._stop.set()
        self._scanner.stop()
        await self._feed.stop()
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        logger.info("crypto_orchestrator_stopped")

    # ---- market handler -------------------------------------------------

    async def _on_new_market(self, cm: CryptoMarket) -> Optional[str]:
        users = await self._crypto_users()
        if not users:
            logger.debug("crypto_no_users", market_id=cm.market.id)
            return None

        snap = self._feed.snapshot()
        if not snap.is_fresh:
            logger.warning(
                "crypto_skip", reason="feed_stale", age_ms=snap.age_ms,
                sources=snap.sources, market_id=cm.market.id,
            )
            return "retry"
        if not snap.is_warm:
            logger.info(
                "crypto_skip", reason="feed_warming",
                samples=snap.sample_count, market_id=cm.market.id,
            )
            return "retry"
        spot = snap.spot or 0.0

        strike = cm.strike if cm.strike_kind == "absolute" and cm.strike else spot
        seconds_left = cm.seconds_left
        if seconds_left <= 5:
            logger.info("crypto_skip", reason="market_too_close_to_close", market_id=cm.market.id)
            return

        p_fair_yes = fair_prob_above(spot, strike, snap.sigma_per_sec, seconds_left)

        # --- order book on both sides for the lag-arb decision ---
        ask_yes = await self._best_ask(cm.yes_token_id)
        ask_no = await self._best_ask(cm.no_token_id)
        diag = edge_diagnostic(
            p_fair_yes,
            ask_yes=ask_yes,
            ask_no=ask_no,
            fee_bps=settings.crypto_fee_bps,
            slip_bps=settings.crypto_slippage_bps,
        )
        quote = choose_side(
            p_fair_yes,
            ask_yes=ask_yes,
            ask_no=ask_no,
            fee_bps=settings.crypto_fee_bps,
            slip_bps=settings.crypto_slippage_bps,
        )
        min_e = float(settings.crypto_min_edge_pct)
        if quote is None or quote.edge_pct < min_e:
            gap_pp: Optional[float] = None
            if quote is not None:
                gap_pp = round(min_e - float(quote.edge_pct), 3)
            elif diag.best_edge_pct is not None:
                gap_pp = round(min_e - float(diag.best_edge_pct), 3)
            reason = "edge_below_min" if quote is not None else "no_positive_edge"
            logger.info(
                "crypto_skip",
                reason=reason,
                slug=cm.market.slug,
                horizon=cm.horizon,
                market_id=cm.market.id,
                p_fair_yes=round(p_fair_yes, 4),
                ask_yes=ask_yes,
                ask_no=ask_no,
                edge_yes_pct=round(diag.edge_yes_pct, 4) if diag.edge_yes_pct is not None else None,
                edge_no_pct=round(diag.edge_no_pct, 4) if diag.edge_no_pct is not None else None,
                best_side=diag.best_side,
                best_edge_pct=round(diag.best_edge_pct, 4) if diag.best_edge_pct is not None else None,
                chosen_side=quote.side if quote else None,
                chosen_edge_pct=round(quote.edge_pct, 4) if quote else None,
                min_edge_pct=min_e,
                gap_pp=gap_pp,
                tune_hint="si gap_pp es pequeño (ej. 0.2-1.0), bajar CRYPTO_MIN_EDGE_PCT acerca el trade",
            )
            return

        # --- TA confluence filter (0 = disabled for that horizon) ---
        min_conf = self._min_confluence(cm.horizon)
        if min_conf <= 0:
            ta_reasons = ["ta_disabled_min_confluence_0"]
        else:
            candles = await self._candles.get()
            side_long_short = "long" if quote.side == "yes" else "short"
            ta = ta_score(candles, side_long_short)
            if ta.confluence < min_conf:
                logger.info(
                    "crypto_skip",
                    reason="low_confluence",
                    horizon=cm.horizon,
                    confluence=ta.confluence,
                    min=min_conf,
                    edge_pct=round(quote.edge_pct, 2),
                    rsi=ta.rsi,
                    trend_15m=ta.trend_15m,
                    market_id=cm.market.id,
                )
                return
            ta_reasons = ta.reasons

        # --- News overlay (context only) ---
        overlay_decision = self._overlay.modifier(quote.side, cm.horizon)
        if overlay_decision.action == "veto":
            logger.info(
                "crypto_skip",
                reason="news_veto",
                horizon=cm.horizon,
                sentiment=round(overlay_decision.sentiment, 3),
                market_id=cm.market.id,
            )
            return

        # --- Per-user execution ---
        for user in users:
            if not self._under_daily_cap(user, cm.horizon):
                logger.info(
                    "crypto_skip",
                    reason="daily_cap",
                    horizon=cm.horizon,
                    user_id=user.id,
                    market_id=cm.market.id,
                )
                continue
            await self._execute_first_entry(
                user=user,
                cm=cm,
                quote=quote,
                spot=spot,
                p_fair_yes=p_fair_yes,
                ta_reasons=ta_reasons,
                overlay_scale=overlay_decision.scale,
                overlay_sentiment=overlay_decision.sentiment,
            )

    async def _execute_first_entry(
        self,
        *,
        user: User,
        cm: CryptoMarket,
        quote: EdgeQuote,
        spot: float,
        p_fair_yes: float,
        ta_reasons: list[str],
        overlay_scale: float,
        overlay_sentiment: float,
    ) -> None:
        breakdown = await self._balance.effective_balance(user)
        balance = float(breakdown.effective)
        if balance <= 0:
            logger.info("crypto_skip", reason="zero_balance", user_id=user.id)
            return

        open_usd = self._open_usd_by_user.get(user.id, 0.0)
        sizing = first_entry_size(
            balance=balance, edge_pct=quote.edge_pct, currently_open_usd=open_usd
        )
        if sizing.amount_usd <= 0:
            logger.info(
                "crypto_skip",
                reason=f"sizing_{sizing.reason}",
                user_id=user.id,
                anchor=round(sizing.anchor_usd, 2),
                kelly=round(sizing.kelly_usd, 2),
                cap=round(sizing.per_trade_cap_usd, 2),
                concurrent=round(sizing.concurrent_cap_usd, 2),
            )
            return
        size_usd = sizing.amount_usd * overlay_scale
        if size_usd < settings.min_trade_usd:
            logger.info(
                "crypto_skip",
                reason="overlay_shrunk_below_min",
                user_id=user.id,
                size_usd=round(size_usd, 2),
            )
            return

        plan = sizing.to_plan(entry_price=quote.ask, band="crypto_first")
        plan.amount_usd = round(size_usd, 4)

        signal = await self._persist_synthetic_signal(
            cm=cm, quote=quote, p_fair_yes=p_fair_yes, ta_reasons=ta_reasons
        )
        result = await self._executor.open_trade(
            user=user, signal=signal, market=cm.market, side=quote.side, plan=plan
        )
        if not result.ok:
            logger.warning(
                "crypto_trade_failed",
                user_id=user.id,
                reason=result.reason,
                market_id=cm.market.id,
            )
            return

        self._open_usd_by_user[user.id] = open_usd + size_usd
        self._bump_daily(user, cm.horizon)
        await self._notify_entry(
            user=user,
            cm=cm,
            quote=quote,
            spot=spot,
            p_fair_yes=p_fair_yes,
            size_usd=size_usd,
            balance=balance,
            ta_reasons=ta_reasons,
            sentiment=overlay_sentiment if overlay_scale != 1.0 else None,
        )

        # Schedule the late scoop for the same market.
        self._tasks.append(
            asyncio.create_task(
                self._late_scoop_loop(user_id=user.id, cm=cm, side=quote.side),
                name=f"crypto_scoop_{cm.market.id}_{user.id}",
            )
        )

    # ---- late scoop -----------------------------------------------------

    async def _late_scoop_loop(
        self, *, user_id: int, cm: CryptoMarket, side: str
    ) -> None:
        target_window = max(10, settings.crypto_late_scoop_window_seconds)
        # Wait until we are inside the late window.
        delay = max(0.0, cm.seconds_left - target_window)
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
            return  # stop event fired
        except asyncio.TimeoutError:
            pass

        # Inside the late window: poll every 3 s until close or scoop fires.
        async with session_scope() as session:
            user = await session.get(User, user_id)
        if user is None or user.mode != UserMode.CRYPTO:
            return

        while not self._stop.is_set() and cm.seconds_left > 1:
            ask_yes = await self._best_ask(cm.yes_token_id)
            ask_no = await self._best_ask(cm.no_token_id)
            ask = ask_yes if side == "yes" else ask_no
            if ask is None:
                await asyncio.sleep(3)
                continue
            breakdown = await self._balance.effective_balance(user)
            balance = float(breakdown.effective)
            open_usd = self._open_usd_by_user.get(user.id, 0.0)
            scoop = late_scoop_size(
                balance=balance,
                market_price=ask,
                currently_open_usd=open_usd,
            )
            if scoop.amount_usd <= 0:
                await asyncio.sleep(3)
                continue
            plan = scoop.to_plan(entry_price=ask, band="crypto_scoop")
            signal = await self._persist_synthetic_signal(
                cm=cm,
                quote=EdgeQuote(
                    side=side,  # type: ignore[arg-type]
                    p_fair=ask,
                    ask=ask,
                    edge_pct=0.0,
                    cost_bps=settings.crypto_fee_bps + settings.crypto_slippage_bps,
                ),
                p_fair_yes=ask if side == "yes" else (1.0 - ask),
                ta_reasons=["late_scoop_imbalance"],
            )
            result = await self._executor.open_trade(
                user=user, signal=signal, market=cm.market, side=side, plan=plan
            )
            if result.ok:
                self._open_usd_by_user[user.id] = open_usd + scoop.amount_usd
                seconds_left = int(cm.seconds_left)
                try:
                    await self._bot.send_message(
                        chat_id=user.telegram_id,
                        text=crypto_late_scoop_card(
                            horizon=cm.horizon,
                            side=side,
                            entry_price=ask,
                            size_usd=scoop.amount_usd,
                            balance_pct=(scoop.amount_usd / balance * 100.0) if balance > 0 else 0.0,
                            seconds_left=seconds_left,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("crypto_scoop_notify_failed", error=str(exc))
            return

    # ---- notifications --------------------------------------------------

    async def _mode_announcement_loop(self) -> None:
        """Send a one-shot 'Crypto Mode active' message per user."""
        while not self._stop.is_set():
            try:
                users = await self._crypto_users()
                for u in users:
                    if u.id in self._already_notified_mode:
                        continue
                    if not u.notifications_enabled:
                        continue
                    try:
                        await self._bot.send_message(
                            chat_id=u.telegram_id, text=crypto_mode_switched()
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "crypto_mode_announce_failed",
                            user_id=u.id,
                            error=str(exc),
                        )
                    self._already_notified_mode.add(u.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("crypto_mode_announce_loop_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _notify_entry(
        self,
        *,
        user: User,
        cm: CryptoMarket,
        quote: EdgeQuote,
        spot: float,
        p_fair_yes: float,
        size_usd: float,
        balance: float,
        ta_reasons: list[str],
        sentiment: Optional[float],
    ) -> None:
        if not user.notifications_enabled:
            return
        text = crypto_entry_card(
            horizon=cm.horizon,
            side=quote.side,
            entry_price=quote.ask,
            size_usd=size_usd,
            balance_pct=(size_usd / balance * 100.0) if balance > 0 else 0.0,
            edge_pct=quote.edge_pct,
            p_fair=quote.p_fair,
            spot=spot,
            seconds_left=int(cm.seconds_left),
            reasons=ta_reasons,
            sentiment=sentiment,
        )
        try:
            await self._bot.send_message(chat_id=user.telegram_id, text=text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("crypto_notify_entry_failed", error=str(exc))

    # ---- helpers --------------------------------------------------------

    async def _crypto_users(self) -> list[User]:
        async with session_scope() as session:
            users = await UsersRepository(session).list_allowed()
        return [u for u in users if u.mode == UserMode.CRYPTO and u.is_active]

    async def _best_ask(self, token_id: Optional[str]) -> Optional[float]:
        if not token_id:
            return None
        book = await self._poly.get_order_book(token_id)
        if book is None:
            return None
        return book.best_ask

    def _min_confluence(self, horizon: Horizon) -> int:
        if horizon == "5m":
            return int(settings.crypto_5m_min_confluence)
        return int(settings.crypto_1h_min_confluence)

    def _under_daily_cap(self, user: User, horizon: Horizon) -> bool:
        day = utcnow().strftime("%Y-%m-%d")
        if horizon == "1d":
            taken = self._daily_taken.get((user.id, day), 0)
            return taken < int(settings.crypto_daily_max_trades)
        if horizon == "1h":
            taken = self._daily_taken_1h.get((user.id, day), 0)
            return taken < int(settings.crypto_1h_max_trades)
        # 5m: rate limited only by sizing concurrent cap and TradeLimiter.
        return True

    def _bump_daily(self, user: User, horizon: Horizon) -> None:
        day = utcnow().strftime("%Y-%m-%d")
        if horizon == "1d":
            self._daily_taken[(user.id, day)] = self._daily_taken.get((user.id, day), 0) + 1
        elif horizon == "1h":
            self._daily_taken_1h[(user.id, day)] = (
                self._daily_taken_1h.get((user.id, day), 0) + 1
            )

    async def _persist_synthetic_signal(
        self,
        *,
        cm: CryptoMarket,
        quote: EdgeQuote,
        p_fair_yes: float,
        ta_reasons: list[str],
    ) -> _SyntheticSignal:
        """Persist a Signal row so the trade has a real foreign key.

        We reuse the existing Signal table — keeps reporting / ``/signals``
        consistent — but tag the row with ``category='crypto'`` and
        encode the lag-arb context in ``feature_vector``.
        """
        from decimal import Decimal

        from app.database.models import Signal, SignalImpact, SignalStatus
        from app.database.repositories.signals_repo import SignalsRepository
        from app.utils.text import stable_hash

        feature_vector = {
            "source": "crypto",
            "horizon": cm.horizon,
            "p_fair_yes": round(p_fair_yes, 6),
            "ask": round(quote.ask, 6),
            "edge_pct": round(quote.edge_pct, 4),
            "ta_reasons": ta_reasons,
        }
        impact = SignalImpact.BULLISH if quote.side == "yes" else SignalImpact.BEARISH
        signal = Signal(
            news_title=f"[CRYPTO] BTC {cm.horizon} {quote.side.upper()} edge={quote.edge_pct:.2f}%",
            news_url=None,
            news_source="crypto_mode",
            news_published_at=utcnow(),
            news_hash=stable_hash(
                f"crypto:{cm.market.id}:{quote.side}:{int(time.time())}"
            ),
            market_id=cm.market.id,
            market_question=cm.market.question,
            market_slug=cm.market.slug,
            market_price=Decimal(str(quote.ask)),
            market_volume_24h=Decimal(str(cm.market.volume_24h or 0)),
            impact=impact,
            urgency=8,
            ai_raw=None,
            score=Decimal("80"),
            trader_confirmation=False,
            trader_aligned_count=0,
            trader_conviction_usd=Decimal("0"),
            status=SignalStatus.SENT,
            quality_score=None,
            category="crypto",
            magnitude=0,
            rarity=0,
            timing_phase=1,
            mispricing_z=None,
            liquidity_score=None,
            expected_edge_pct=Decimal(str(quote.edge_pct)),
            slippage_bps=Decimal(str(settings.crypto_slippage_bps)),
            entities=["bitcoin"],
            feature_vector=feature_vector,
        )
        async with session_scope() as session:
            signal = await SignalsRepository(session).create(signal)
        return _SyntheticSignal(id=signal.id, feature_vector=feature_vector)


__all__ = ["CryptoOrchestrator"]
