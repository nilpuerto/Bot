"""Trade monitor — background loop that prices open trades and drives
them through the **repricing exit state machine**.

The monitor itself is a thin scheduler: it fetches the current mid for
each open trade, computes PnL, and hands the result to
:func:`app.services.exit_strategy.evaluate_exit` — the single source of
truth for "what should happen on this tick?".

The state machine is asymmetric by design:

* hard stop-loss at ``-HARD_SL_PCT`` (absolute floor);
* partial take-profit ladder (``+40 / +100 / +200 %``) that realises a
  slice of the position and progressively tightens the trailing stop;
* trailing stop on the remaining runner — **no hard TP ceiling**, so
  exponential outliers can run;
* time exit for cold trades that never reprice.

Ordering inside a single tick: optional hard SL → partial ladder rung → trailing →
time exit → HOLD.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Callable, Optional

from app.config.settings import settings
from app.database.models import CloseReason, Trade, TradeSide
from app.database.repositories.trades_repo import TradesRepository
from app.database.session import session_scope
from app.integrations.polymarket_client import PolymarketClient
from app.services.exit_strategy import (
    ExitActionKind,
    TradeExitView,
    evaluate_exit,
)
from app.services.trade_executor import TradeExecutor
from app.utils.logger import get_logger
from app.utils.money import pnl_pct, pnl_usd


logger = get_logger(__name__)

OnCloseCallback = Callable[[Trade, CloseReason, float], "asyncio.Future | None"]


class TradeMonitor:
    def __init__(
        self,
        polymarket: PolymarketClient,
        executor: TradeExecutor,
        interval_seconds: Optional[int] = None,
        on_close: Optional[OnCloseCallback] = None,
    ) -> None:
        self._poly = polymarket
        self._executor = executor
        self._interval = interval_seconds or settings.trade_monitor_interval_seconds
        self._on_close = on_close
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # defensive
                logger.exception("trade_monitor_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        async with session_scope() as session:
            repo = TradesRepository(session)
            open_trades = await repo.list_all_open()
        if not open_trades:
            return

        price_cache: dict[str, Optional[float]] = {}
        for trade in open_trades:
            await self._process_trade(trade, price_cache)

    async def _process_trade(
        self, trade: Trade, price_cache: dict[str, Optional[float]]
    ) -> None:
        price = await self._fetch_price(trade.market_id, trade.side, price_cache)
        if price is None:
            return

        entry_price = float(trade.entry_price)
        shares = float(trade.shares)
        unrealized_pnl = pnl_usd(entry_price, price, shares)
        unrealized_pct = pnl_pct(entry_price, price)

        async with session_scope() as session:
            await TradesRepository(session).update_price(
                trade.id,
                price=Decimal(str(price)),
                pnl=Decimal(str(unrealized_pnl)),
                pnl_pct=Decimal(str(unrealized_pct)),
            )

        view = TradeExitView(
            entry_price=entry_price,
            current_shares=shares,
            opened_at=trade.opened_at,
            peak_price=(
                float(trade.peak_price) if trade.peak_price is not None else None
            ),
            trailing_active=bool(trade.trailing_active),
            exit_state=trade.exit_state or {},
        )
        evaluation = evaluate_exit(
            view, price=price, pnl_pct_value=unrealized_pct
        )

        action = evaluation.action
        if action.kind is ExitActionKind.HOLD:
            # Persist advancing state (max_pnl_pct_seen, trailing update,
            # peak price) so the next tick sees the progress.
            await self._persist_hold(trade, evaluation)
            return

        if action.kind is ExitActionKind.PARTIAL:
            # Partial closes always leave the runner open; bump trailing
            # parameters and continue.
            assert action.close_shares is not None
            assert action.tier is not None
            assert action.new_trailing_pct is not None
            result = await self._executor.partial_close(
                trade.id,
                tier=action.tier,
                close_shares=action.close_shares,
                close_price=price,
                new_trailing_pct=action.new_trailing_pct,
                peak_price=evaluation.new_peak_price,
                trailing_active=evaluation.new_trailing_active,
            )
            if not result.ok:
                logger.warning(
                    "partial_close_failed",
                    trade_id=trade.id,
                    reason=result.reason,
                )
            return

        # ExitActionKind.CLOSE — final exit.
        assert action.close_reason is not None
        # Persist the final exit_state (with the advanced
        # ``max_pnl_pct_seen``) before we close so telemetry is accurate.
        async with session_scope() as session:
            await TradesRepository(session).update_trailing(
                trade.id,
                peak_price=(
                    Decimal(str(evaluation.new_peak_price))
                    if evaluation.new_peak_price is not None
                    else None
                ),
                trailing_active=evaluation.new_trailing_active,
                exit_state=evaluation.new_exit_state,
            )

        result = await self._executor.close_trade(
            trade.id, reason=action.close_reason, close_price=price
        )
        if result.ok and self._on_close is not None:
            try:
                maybe = self._on_close(trade, action.close_reason, price)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception as exc:
                logger.warning("on_close_callback_error", error=str(exc))

    async def _persist_hold(self, trade: Trade, evaluation) -> None:
        """Write back the evolving ``exit_state`` + peak / trailing so the
        next tick has up-to-date context.
        """
        async with session_scope() as session:
            await TradesRepository(session).update_trailing(
                trade.id,
                peak_price=(
                    Decimal(str(evaluation.new_peak_price))
                    if evaluation.new_peak_price is not None
                    else None
                ),
                trailing_active=evaluation.new_trailing_active,
                exit_state=evaluation.new_exit_state,
            )

    async def _fetch_price(
        self, market_id: str, side: TradeSide, cache: dict[str, Optional[float]]
    ) -> Optional[float]:
        key = f"{market_id}:{side.value}"
        if key in cache:
            return cache[key]
        market = await self._poly.get_market(market_id)
        if market is None:
            cache[key] = None
            return None
        price = (
            market.best_yes_price if side == TradeSide.YES else market.best_no_price
        )
        cache[key] = price
        return price
