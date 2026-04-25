"""Prym edge-first strategy — the single gate that turns a scored
opportunity into a ``(GO, side, entry, target, size)`` tuple.

The strategy gate is strictly sequential — EVERY condition must pass
before we can trade.  Short-circuiting on the cheapest checks first
keeps the latency budget tight:

1. **Direction gate** — neutral AI impact = veto.
2. **Price bounds** — entry price ∈ [``entry_min_price``,
   ``entry_max_price``].
3. **Hard-gate cluster** — delegated to ``ScoreBreakdown.passes_trade``
   which combines freshness, phase, |z| ≥ Z_MIN, net_edge ≥ MIN_EDGE
   and fill ≥ MIN_FILL_RATIO.  The 0..100 score is cosmetic and is
   **not** part of this gate under the edge-first refactor.
4. **Edge-after-costs gate** — :mod:`execution_cost` confirms
   ``net_edge_pct ≥ MIN_EDGE_PCT`` for the actual USD notional.  When
   the orchestrator pre-computes the cost and passes it in, we reuse
   that result instead of probing the book a second time.

Sizing is delegated entirely to :mod:`app.services.sizing` so the same
logic powers AUTO execution, SEMI anchoring, and manual overrides.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings
from app.integrations.mistral_client import AIAnalysis
from app.integrations.polymarket_client import MarketSnapshot, OrderBook
from app.services.execution_cost import ExecutionCost, ExecutionCostModel
from app.services.signal_scoring import ScoreBreakdown
from app.services.sizing import SizingQuote, compute_sizing
from app.strategies.base_strategy import BaseStrategy, SizingPlan, StrategyDecision
from app.utils.money import clamp, round_price


@dataclass
class StrategyVerdict:
    """Extended decision carrying cost-model output for telemetry."""

    decision: StrategyDecision
    cost: Optional[ExecutionCost] = None


class PrymStrategy(BaseStrategy):
    name = "prym_v2"

    def __init__(
        self,
        *,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        score_trade_threshold: Optional[float] = None,
        min_edge_pct: Optional[float] = None,
    ) -> None:
        self.min_price = (
            min_price if min_price is not None else settings.entry_min_price
        )
        self.max_price = (
            max_price if max_price is not None else settings.entry_max_price
        )
        self.score_trade_threshold = (
            score_trade_threshold
            if score_trade_threshold is not None
            else settings.score_threshold_trade
        )
        self.min_edge_pct = (
            min_edge_pct if min_edge_pct is not None else settings.min_edge_pct
        )
        self._cost_model = ExecutionCostModel(min_edge_pct=self.min_edge_pct)

    # ---- evaluation -----------------------------------------------------

    def evaluate(
        self,
        *,
        ai: AIAnalysis,
        market: MarketSnapshot,
        score: ScoreBreakdown,
        book: Optional[OrderBook] = None,
        size_usd: Optional[float] = None,
        precomputed_cost: Optional[ExecutionCost] = None,
    ) -> StrategyDecision:
        verdict = self.evaluate_with_cost(
            ai=ai,
            market=market,
            score=score,
            book=book,
            size_usd=size_usd,
            precomputed_cost=precomputed_cost,
        )
        return verdict.decision

    def evaluate_with_cost(
        self,
        *,
        ai: AIAnalysis,
        market: MarketSnapshot,
        score: ScoreBreakdown,
        book: Optional[OrderBook] = None,
        size_usd: Optional[float] = None,
        precomputed_cost: Optional[ExecutionCost] = None,
    ) -> StrategyVerdict:
        # 1. Direction gate.
        if ai.impact == "neutral":
            return StrategyVerdict(StrategyDecision(False, "neutral_impact"))

        side = "yes" if ai.impact == "bullish" else "no"
        price = market.best_yes_price if side == "yes" else market.best_no_price
        if price is None:
            return StrategyVerdict(StrategyDecision(False, "no_price", side=side))

        # 2. Price bounds.
        if price < self.min_price or price > self.max_price:
            return StrategyVerdict(
                StrategyDecision(False, f"price_out_of_range_{price:.3f}", side=side)
            )

        # 3. Hard-gate cluster (measurable).  The 0..100 score is
        # cosmetic — the real gates live on ``ScoreBreakdown``.
        if not score.passes_trade:
            return StrategyVerdict(
                StrategyDecision(
                    False,
                    f"edge_gate_{score.gate_reason or 'failed'}",
                    side=side,
                )
            )

        # 4. Edge after costs (reuse pre-computed when available).
        if precomputed_cost is not None:
            if not precomputed_cost.passes:
                return StrategyVerdict(
                    StrategyDecision(
                        False, f"edge_gate_{precomputed_cost.reason}", side=side
                    ),
                    cost=precomputed_cost,
                )
            return StrategyVerdict(
                StrategyDecision(True, "ok", side=side), cost=precomputed_cost
            )

        target = clamp(
            round_price(price * (1 + settings.take_profit_pct / 100)), 0.001, 0.999
        )
        size = float(size_usd or settings.min_trade_usd)
        if book is not None:
            cost = self._cost_model.evaluate(
                book=book, size_usd=size, side=side, target_price=target
            )
            if not cost.passes:
                return StrategyVerdict(
                    StrategyDecision(
                        False, f"edge_gate_{cost.reason}", side=side
                    ),
                    cost=cost,
                )
            return StrategyVerdict(
                StrategyDecision(True, "ok", side=side), cost=cost
            )

        # No book available — allow based on the hard gates already
        # passed but flag for observability.
        return StrategyVerdict(
            StrategyDecision(True, "ok_no_book", side=side), cost=None
        )

    # ---- sizing ---------------------------------------------------------

    def sizing(
        self,
        *,
        balance: float,
        risk_pct: float,
        entry_price: float,
        high_confidence: bool = False,
        stop_loss_enabled: bool = True,
        score: Optional[float] = None,
        user_override: Optional[float] = None,
        net_edge_pct: Optional[float] = None,
        abs_z: Optional[float] = None,
    ) -> SizingPlan:
        """Edge-first balance-% sizing.

        The band (low/mid/high) is picked from measurable metrics when
        they are available:

        * ``net_edge_pct`` + ``abs_z`` → :func:`tier_from_edge`
          (preferred under the edge-first refactor)
        * falls back to the legacy ``score`` → :func:`band_for_score`
          when the caller has not been upgraded yet.

        ``risk_pct`` tightens the band (per-user ceiling) but never
        widens it; ``MIN_/MAX_TRADE_USD`` are absolute guard-rails.

        The fixed ``stop_loss`` is retired — protective exits now live
        in the trade monitor:

        * hard SL at ``-HARD_SL_PCT`` (PnL-based, not a price level);
        * partial-TP ladder + progressively tightening trailing stop
          drive the upside;
        * time-exit closes cold trades that never reprice.

        Consequently the ``take_profit`` field is set to ``None`` here —
        the strategy does NOT pre-commit the trade to any hard ceiling.
        ``stop_loss_enabled`` is kept for backwards compatibility but
        ignored.
        """
        effective_score = score if score is not None else (90.0 if high_confidence else 60.0)
        quote: SizingQuote = compute_sizing(
            score=effective_score,
            balance=balance,
            risk_pct=risk_pct,
            user_override=user_override,
            net_edge_pct=net_edge_pct,
            abs_z=abs_z,
            entry_price=entry_price,
        )

        _ = stop_loss_enabled

        plan = SizingPlan(
            amount_usd=quote.amount_usd,
            entry_price=entry_price,
            stop_loss=None,
            take_profit=None,
            high_confidence=(
                quote.band == "high" or effective_score >= 85.0
            ),
        )
        plan.band = quote.band  # type: ignore[attr-defined]
        plan.sizing_quote = quote  # type: ignore[attr-defined]
        return plan
