"""Execution cost model.

Transforms a prospective trade into a realistic net-edge estimate:

* ``expected_fill`` — VWAP from walking the order book.
* ``slippage_bps`` — fill − mid, in basis points.
* ``spread_cost_pct`` — half-spread as a percent of mid (the single-tick
                        cost of crossing the book).
* ``fees_pct`` — configured Polymarket fee (default 0 — Polymarket has
                 no maker/taker fees at the moment; retained as a knob).
* ``edge_pct`` — (target − fill) / fill × 100  — gross edge before costs.
* ``net_edge_pct`` — ``edge_pct − spread_cost_pct − slippage_pct − fees_pct``.

A trade is allowed iff ``net_edge_pct >= settings.min_edge_pct``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings
from app.integrations.polymarket_client import OrderBook
from app.services.microstructure import MicrostructureService


@dataclass
class ExecutionCost:
    size_usd: float
    side: str  # 'yes' / 'no'
    mid: Optional[float]
    expected_fill: Optional[float]
    filled_usd: float
    slippage_bps: Optional[float]
    spread_cost_pct: float
    fees_pct: float
    target_price: Optional[float]
    edge_pct: Optional[float]
    net_edge_pct: Optional[float]
    passes: bool
    reason: str

    @property
    def fully_filled(self) -> bool:
        return self.filled_usd >= self.size_usd - 1e-6

    @property
    def fill_ratio(self) -> float:
        """Fraction of the requested size that the book could actually fill.

        Used as the execution-hard-gate in ``SignalScoringSystem`` — a
        book that can only fill 40 % of our intended stake without
        blowing past the target price is not tradeable.
        """
        if self.size_usd <= 0:
            return 0.0
        return max(0.0, min(1.0, self.filled_usd / self.size_usd))


class ExecutionCostModel:
    def __init__(
        self,
        *,
        min_edge_pct: Optional[float] = None,
        fees_pct: Optional[float] = None,
    ) -> None:
        self.min_edge_pct = (
            min_edge_pct if min_edge_pct is not None else settings.min_edge_pct
        )
        self.fees_pct = fees_pct if fees_pct is not None else settings.polymarket_fee_pct
        self._micro = MicrostructureService(polymarket=None)  # type: ignore[arg-type]

    def evaluate(
        self,
        *,
        book: Optional[OrderBook],
        size_usd: float,
        side: str,
        target_price: float,
    ) -> ExecutionCost:
        if book is None or size_usd <= 0:
            return ExecutionCost(
                size_usd=size_usd,
                side=side,
                mid=None,
                expected_fill=None,
                filled_usd=0.0,
                slippage_bps=None,
                spread_cost_pct=0.0,
                fees_pct=self.fees_pct,
                target_price=target_price,
                edge_pct=None,
                net_edge_pct=None,
                passes=False,
                reason="no_book",
            )

        mid = book.mid
        spread_cost_pct = 0.0
        if mid and mid > 0 and book.spread is not None:
            # Half-spread in %.
            spread_cost_pct = (book.spread / 2.0) / mid * 100.0

        vwap, slip_bps, filled = self._micro.estimate_slippage(
            book, side=side, size_usd=size_usd
        )

        edge_pct: Optional[float] = None
        net_edge_pct: Optional[float] = None
        if vwap and vwap > 0:
            edge_pct = (target_price - vwap) / vwap * 100.0
            slippage_pct = abs(slip_bps or 0.0) / 100.0  # bps → %
            net_edge_pct = edge_pct - spread_cost_pct - slippage_pct - self.fees_pct

        fill_floor = float(settings.min_fill_ratio)
        passes = (
            vwap is not None
            and net_edge_pct is not None
            and net_edge_pct >= self.min_edge_pct
            and (filled >= size_usd * fill_floor)
        )
        reason = "ok"
        if vwap is None:
            reason = "no_fill"
        elif filled < size_usd * fill_floor:
            reason = f"partial_fill_{filled:.2f}/{size_usd:.2f}_floor_{fill_floor}"
        elif net_edge_pct is None:
            reason = "no_edge"
        elif net_edge_pct < self.min_edge_pct:
            reason = f"edge_below_min_{net_edge_pct:.2f}<{self.min_edge_pct}"

        return ExecutionCost(
            size_usd=size_usd,
            side=side,
            mid=mid,
            expected_fill=vwap,
            filled_usd=filled,
            slippage_bps=slip_bps,
            spread_cost_pct=round(spread_cost_pct, 4),
            fees_pct=self.fees_pct,
            target_price=target_price,
            edge_pct=round(edge_pct, 4) if edge_pct is not None else None,
            net_edge_pct=round(net_edge_pct, 4) if net_edge_pct is not None else None,
            passes=passes,
            reason=reason,
        )
