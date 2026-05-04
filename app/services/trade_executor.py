"""Trade executor — the only place that writes orders.

Two implementations behind one async interface:

* **Simulation** (``SIMULATION_MODE=true`` or no CLOB creds): fill at the
  current price, mark the trade ``is_simulated=True``.  No on-chain activity.
* **Real**: balance-check via web3 USDC.e, translate USD→shares, sign and
  submit the order through ``py-clob-client-v2``.  Any failure transitions the
  trade to ``failed`` rather than leaving it in a limbo state.

Callers always receive a :class:`ExecutionResult`.  They are never required
to handle half-executed writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.config.settings import settings
from app.database.models import (
    CloseReason,
    Signal,
    SignalStatus,
    Trade,
    TradeSide,
    TradeStatus,
    User,
)
from app.database.repositories.signals_repo import SignalsRepository
from app.database.repositories.trades_repo import TradesRepository
from app.database.session import session_scope
from app.integrations.polymarket_client import (
    MarketSnapshot,
    PolymarketClient,
    PolymarketWriteDisabled,
)
from app.services.exit_strategy import empty_exit_state, record_partial
from app.services.trade_limiter import TradeLimiter
from app.strategies.base_strategy import SizingPlan
from app.utils.logger import get_logger
from app.utils.money import pnl_pct, pnl_usd, shares_from_usd
from app.utils.time import utcnow


logger = get_logger(__name__)


@dataclass
class ExecutionResult:
    ok: bool
    trade_id: Optional[int]
    reason: str
    is_simulated: bool


class TradeExecutor:
    def __init__(self, polymarket: PolymarketClient, limiter: TradeLimiter) -> None:
        self._poly = polymarket
        self._limiter = limiter

    # ---- open ------------------------------------------------------------

    async def open_trade(
        self,
        *,
        user: User,
        signal: Signal,
        market: MarketSnapshot,
        side: str,
        plan: SizingPlan,
    ) -> ExecutionResult:
        """Perform all final-leg checks + write the trade."""
        is_crypto = getattr(signal, "category", None) == "crypto"
        decision = await self._limiter.check(
            user=user,
            market_id=market.id,
            market_question=market.question,
            is_crypto=is_crypto,
        )
        if not decision.allowed:
            logger.info(
                "trade_blocked",
                user_id=user.id,
                market_id=market.id,
                reason=decision.reason,
                is_crypto=is_crypto,
                signal_id=getattr(signal, "id", None),
                category=getattr(signal, "category", None),
            )
            return ExecutionResult(False, None, decision.reason, settings.simulation_mode)

        if plan.amount_usd <= 0:
            return ExecutionResult(False, None, "zero_sizing", settings.simulation_mode)

        use_simulation = settings.simulation_mode or not settings.has_polymarket_write_credentials

        if not use_simulation:
            # Real execution: verify wallet USDC.e balance first.
            balance = await self._poly.get_usdc_balance()
            if balance < Decimal(str(plan.amount_usd)):
                logger.warning(
                    "insufficient_usdc_balance",
                    required=plan.amount_usd,
                    available=float(balance),
                )
                return ExecutionResult(
                    False, None, "insufficient_usdc_balance", is_simulated=False
                )

        # Persist as pending so any later failure leaves an auditable row.
        shares = shares_from_usd(plan.amount_usd, plan.entry_price)
        feature_vector = getattr(signal, "feature_vector", None)
        trade = Trade(
            user_id=user.id,
            signal_id=signal.id,
            market_id=market.id,
            market_question=market.question,
            market_slug=market.slug,
            side=TradeSide(side),
            entry_price=Decimal(str(plan.entry_price)),
            current_price=Decimal(str(plan.entry_price)),
            amount_usd=Decimal(str(plan.amount_usd)),
            shares=Decimal(str(shares)),
            # ``stop_loss`` is NULL when the user has disabled the safety net.
            stop_loss=(
                Decimal(str(plan.stop_loss)) if plan.stop_loss is not None else None
            ),
            take_profit=(
                Decimal(str(plan.take_profit))
                if plan.take_profit is not None
                else None
            ),
            status=TradeStatus.PENDING,
            is_simulated=use_simulation,
            band=getattr(plan, "band", None),
            feature_vector=feature_vector,
            exit_state=empty_exit_state(),
        )
        async with session_scope() as session:
            trade = await TradesRepository(session).create(trade)

        if use_simulation:
            # Flip status to OPEN — simulated fill at the entry mid price.
            async with session_scope() as session:
                trade_obj = await TradesRepository(session).get(trade.id)
                if trade_obj:
                    trade_obj.status = TradeStatus.OPEN
            await self._mark_signal_and_bump(signal.id, user.id)
            logger.info(
                "trade_opened_simulated",
                trade_id=trade.id,
                market_id=market.id,
                side=side,
                amount_usd=plan.amount_usd,
                entry=plan.entry_price,
            )
            return ExecutionResult(True, trade.id, "simulated", True)

        # Real CLOB submission
        try:
            token_id = _token_id_for_side(market, side)
            if not token_id:
                raise ValueError("token_id_not_available")
            order = await self._poly.place_order(
                token_id=token_id,
                side="BUY",
                price=plan.entry_price,
                size_shares=shares,
            )
        except PolymarketWriteDisabled as exc:
            async with session_scope() as session:
                await TradesRepository(session).mark_failed(trade.id)
            return ExecutionResult(False, trade.id, f"write_disabled:{exc}", False)
        except Exception as exc:  # pragma: no cover - network dependent
            logger.exception("clob_place_order_failed", error=str(exc))
            async with session_scope() as session:
                await TradesRepository(session).mark_failed(trade.id)
            return ExecutionResult(False, trade.id, f"clob_error:{exc}", False)

        order_id = (
            order.get("orderID")
            or order.get("orderId")
            or order.get("id")
            or order.get("order_id")
        )
        async with session_scope() as session:
            t = await TradesRepository(session).get(trade.id)
            if t:
                t.status = TradeStatus.OPEN
                t.clob_order_id = str(order_id) if order_id else None

        await self._mark_signal_and_bump(signal.id, user.id)
        logger.info(
            "trade_opened_real",
            trade_id=trade.id,
            order_id=order_id,
            market_id=market.id,
            side=side,
            amount_usd=plan.amount_usd,
        )
        return ExecutionResult(True, trade.id, "real_executed", False)

    async def _mark_signal_and_bump(self, signal_id: int, user_id: int) -> None:
        async with session_scope() as session:
            repo = SignalsRepository(session)
            sig = await repo.get(signal_id)
            bump_daily = getattr(sig, "category", None) != "crypto" if sig else True
            await repo.set_status(signal_id, SignalStatus.ACTED)
        await self._limiter.register_trade(user_id, bump_global_daily=bump_daily)

    # ---- close -----------------------------------------------------------

    async def close_trade(
        self,
        trade_id: int,
        *,
        reason: CloseReason = CloseReason.MANUAL,
        close_price: Optional[float] = None,
    ) -> ExecutionResult:
        async with session_scope() as session:
            repo = TradesRepository(session)
            trade = await repo.get(trade_id)
            if trade is None:
                return ExecutionResult(False, None, "not_found", is_simulated=False)
            if trade.status != TradeStatus.OPEN:
                return ExecutionResult(
                    False, trade.id, f"not_open:{trade.status.value}", trade.is_simulated
                )

        if close_price is None:
            market = await self._poly.get_market(trade.market_id)
            if market is None:
                return ExecutionResult(False, trade.id, "market_unavailable", trade.is_simulated)
            close_price = (
                market.best_yes_price if trade.side == TradeSide.YES else market.best_no_price
            ) or float(trade.current_price or trade.entry_price)

        # Real close would require posting a sell order; out of MVP scope.
        # Simulated: unrealized on the *remaining* shares + realized
        # accumulated from prior partial exits = total PnL on the trade.
        remaining_shares = float(trade.shares)
        entry_price = float(trade.entry_price)
        unrealized = pnl_usd(entry_price, close_price, remaining_shares)
        realized = float(
            (trade.exit_state or {}).get("realized_pnl_usd", 0.0)
        )
        total_pnl = round(realized + unrealized, 6)

        # Percent relative to the ORIGINAL notional — that is the only
        # denominator that keeps the figure comparable across trades
        # regardless of how much of the position was scaled out.
        original_amount = float(trade.amount_usd) + _partials_notional(
            entry_price, trade.exit_state
        )
        if original_amount > 0:
            total_pnl_pct = round((total_pnl / original_amount) * 100.0, 4)
        else:
            total_pnl_pct = pnl_pct(entry_price, close_price)

        async with session_scope() as session:
            await TradesRepository(session).close(
                trade.id,
                close_price=Decimal(str(close_price)),
                pnl=Decimal(str(total_pnl)),
                pnl_pct=Decimal(str(total_pnl_pct)),
                reason=reason,
            )
        logger.info(
            "trade_closed",
            trade_id=trade.id,
            reason=reason.value,
            pnl=total_pnl,
            pnl_pct=total_pnl_pct,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
        )
        return ExecutionResult(True, trade.id, "closed", trade.is_simulated)

    # ---- partial close --------------------------------------------------

    async def partial_close(
        self,
        trade_id: int,
        *,
        tier: float,
        close_shares: float,
        close_price: float,
        new_trailing_pct: float,
        peak_price: Optional[float] = None,
        trailing_active: Optional[bool] = None,
    ) -> ExecutionResult:
        """Realise ``close_shares`` of an open trade at ``close_price``.

        The trade row is mutated in place: ``shares`` and ``amount_usd``
        shrink, ``exit_state`` gains a new ``partials[]`` entry and the
        tier threshold is recorded in ``tiers_hit``.  The trade stays
        ``status=OPEN`` — the runner keeps running.

        Real-mode CLOB partial sells are out of MVP scope (matches the
        limitation on full ``close_trade``).  The book-keeping here is
        identical in both modes; when a real CLOB sell path lands, it
        can plug in above the ``apply_partial`` call.
        """
        async with session_scope() as session:
            trade = await TradesRepository(session).get(trade_id)
            if trade is None:
                return ExecutionResult(
                    False, None, "not_found", is_simulated=False
                )
            if trade.status != TradeStatus.OPEN:
                return ExecutionResult(
                    False,
                    trade.id,
                    f"not_open:{trade.status.value}",
                    trade.is_simulated,
                )

        current_shares = float(trade.shares)
        if close_shares <= 0 or close_shares > current_shares + 1e-9:
            return ExecutionResult(
                False, trade.id, "invalid_partial_shares", trade.is_simulated
            )

        entry_price = float(trade.entry_price)
        new_shares = max(0.0, current_shares - close_shares)
        new_amount_usd = max(0.0, new_shares * entry_price)

        new_state = record_partial(
            state=trade.exit_state or {},
            tier=tier,
            close_shares=close_shares,
            close_price=close_price,
            entry_price=entry_price,
        )
        new_state["trailing_pct"] = float(new_trailing_pct)

        async with session_scope() as session:
            await TradesRepository(session).apply_partial(
                trade.id,
                new_shares=Decimal(str(new_shares)),
                new_amount_usd=Decimal(str(new_amount_usd)),
                exit_state=new_state,
                peak_price=(
                    Decimal(str(peak_price)) if peak_price is not None else None
                ),
                trailing_active=trailing_active,
            )

        logger.info(
            "trade_partial_close",
            trade_id=trade.id,
            tier=tier,
            close_shares=close_shares,
            close_price=close_price,
            realized_pnl=new_state["realized_pnl_usd"],
            new_trailing_pct=new_trailing_pct,
            remaining_shares=new_shares,
        )
        return ExecutionResult(
            True, trade.id, f"partial_{int(tier)}", trade.is_simulated
        )


def _partials_notional(entry_price: float, exit_state: Optional[dict]) -> float:
    """Reconstruct the USD notional that was carved out by prior partials
    (shares × entry_price), so final PnL % can be expressed against the
    original trade size rather than the shrunken residual.
    """
    if not exit_state:
        return 0.0
    total = 0.0
    for partial in exit_state.get("partials", []) or []:
        try:
            total += float(partial.get("shares", 0.0)) * entry_price
        except (TypeError, ValueError):
            continue
    return total


def _token_id_for_side(market: MarketSnapshot, side: str) -> Optional[str]:
    """Return the CLOB ERC-1155 token id for the requested side.

    v2 pipelines parse ``clobTokenIds`` from the Gamma response into
    :attr:`MarketSnapshot.yes_token_id` / ``.no_token_id``.  When the
    snapshot was built from a cached or partial payload we return
    ``None`` and let the caller either refresh the market or abort the
    real-mode submission (simulation mode is unaffected).
    """
    return market.token_id_for_side(side)
