"""Execution cost model — VWAP, slippage, net edge."""
from __future__ import annotations

from app.integrations.polymarket_client import OrderBook, OrderBookLevel
from app.services.execution_cost import ExecutionCostModel


def _book() -> OrderBook:
    # Buying YES consumes asks starting at 0.20.
    return OrderBook(
        token_id="tok",
        bids=[OrderBookLevel(0.19, 1000), OrderBookLevel(0.18, 2000)],
        asks=[OrderBookLevel(0.20, 1000), OrderBookLevel(0.22, 2000)],
    )


def test_passes_when_target_above_vwap_plus_costs() -> None:
    model = ExecutionCostModel(min_edge_pct=5.0, fees_pct=0.0)
    cost = model.evaluate(
        book=_book(), size_usd=20.0, side="yes", target_price=0.30
    )
    assert cost.passes is True
    assert cost.expected_fill is not None
    # We buy $20 at 0.20 -> fills entirely at first level; vwap == 0.20.
    assert abs(cost.expected_fill - 0.20) < 1e-6
    assert cost.edge_pct is not None and cost.edge_pct > 0


def test_fails_when_edge_too_small() -> None:
    model = ExecutionCostModel(min_edge_pct=50.0, fees_pct=0.0)
    cost = model.evaluate(
        book=_book(), size_usd=20.0, side="yes", target_price=0.21
    )
    assert cost.passes is False
    assert "edge_below_min" in cost.reason or cost.net_edge_pct is not None


def test_no_book_yields_no_fill() -> None:
    model = ExecutionCostModel(min_edge_pct=5.0)
    cost = model.evaluate(book=None, size_usd=20.0, side="yes", target_price=0.3)
    assert cost.passes is False
    assert cost.reason == "no_book"


def test_partial_fill_below_threshold_rejected() -> None:
    thin_book = OrderBook(
        token_id="t", bids=[], asks=[OrderBookLevel(0.5, 1.0)]
    )  # only $0.50 of liquidity
    model = ExecutionCostModel(min_edge_pct=1.0)
    cost = model.evaluate(
        book=thin_book, size_usd=20.0, side="yes", target_price=0.9
    )
    assert cost.passes is False
    assert "partial_fill" in cost.reason
