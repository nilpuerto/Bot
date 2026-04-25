"""Unit tests for the Market Intelligence feature layer.

These are *pure* tests — no DB, no network.  They exercise the three
sub-modules (microstructure / momentum / whales), then the aggregator
that fuses them into the two advisory scalars the orchestrator reads:

* ``market_context_score`` (0..100, observational only)
* ``edge_adjustment_score`` (± ``mi_max_edge_adjustment_pct`` pp)

Key invariants we pin down:

1. Neutral report keeps ``edge_adjustment_score == 0`` so a disabled
   layer is a perfect no-op from the orchestrator's point of view.
2. Adjustment is clipped to ``±max_adjustment_pct`` even when all three
   sub-modules push hard in the same direction.
3. Missing inputs never raise — every module has a safe neutral fallback.
4. Direction is side-aware: a YES signal with a rising price is
   ``aligned``, while the same price path with a NO signal is not.
5. Whales aligned with the signal add positive flow; opposite whales
   subtract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

from app.services.market_intelligence import (
    MarketIntelligenceAggregator,
    compute_microstructure,
    compute_momentum,
    compute_whales,
    neutral_report,
)
from app.services.microstructure import MicrostructureFeatures


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _book(
    *,
    mid: float = 0.20,
    spread: float = 0.01,
    bid_depth: float = 2_000,
    ask_depth: float = 2_000,
    has_book: bool = True,
) -> MicrostructureFeatures:
    return MicrostructureFeatures(
        token_id="tok",
        best_bid=mid - spread / 2,
        best_ask=mid + spread / 2,
        mid=mid,
        spread=spread,
        spread_pct=spread / mid if mid else None,
        top5_bid_depth=bid_depth,
        top5_ask_depth=ask_depth,
        ofi=0.0,
        has_book=has_book,
    )


def _price_row(price: float, minutes_ago: int) -> SimpleNamespace:
    """Mimic an ORM :class:`MarketPriceHistory` row well enough for the
    momentum module (needs ``price`` + ``observed_at``)."""
    return SimpleNamespace(
        price=price,
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


@dataclass
class _Pos:
    """Lightweight stand-in for a TraderPosition row."""

    trader_id: int
    side: str
    size_usd: float


# ---------------------------------------------------------------------------
# Microstructure
# ---------------------------------------------------------------------------


def test_microstructure_healthy_book_scores_high():
    ctx = compute_microstructure(micro=_book(spread=0.002, bid_depth=5_000, ask_depth=5_000))
    assert ctx.context_score >= 0.85
    assert ctx.spread_bps is not None and ctx.spread_bps < 200
    assert ctx.top_depth_usd == 10_000


def test_microstructure_missing_book_returns_neutral():
    ctx = compute_microstructure(micro=None)
    assert ctx.context_score == 0.5
    assert ctx.spread_bps is None


def test_microstructure_widening_spread_penalised():
    healthy = _book(spread=0.005)
    widening_ctx = compute_microstructure(
        micro=healthy, previous_spread_bps=100.0
    )  # was tight, now 250 bps ⇒ +150 bps trend
    assert widening_ctx.spread_trend_bps is not None
    assert widening_ctx.spread_trend_bps > 0


def test_microstructure_extreme_imbalance_penalised():
    skewed = _book(bid_depth=10_000, ask_depth=500)
    neutral = _book(bid_depth=3_000, ask_depth=3_000)
    assert compute_microstructure(micro=skewed).context_score < compute_microstructure(
        micro=neutral
    ).context_score


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------


def test_momentum_insufficient_data_is_neutral():
    ctx = compute_momentum(history=[_price_row(0.2, 2)], side="yes")
    assert ctx.context_score == 0.5
    assert ctx.velocity_pct_per_min is None


def test_momentum_rising_price_on_yes_is_aligned():
    # Linear ramp: 0.10 → 0.16 over 15 minutes ⇒ ~+4 %/min in pp.
    history = [_price_row(0.10 + i * 0.01, 14 - i) for i in range(7)]
    ctx = compute_momentum(history=history, side="yes")
    assert ctx.aligned is True
    assert ctx.context_score > 0.5


def test_momentum_rising_price_on_no_is_anti_aligned():
    history = [_price_row(0.10 + i * 0.01, 14 - i) for i in range(7)]
    ctx = compute_momentum(history=history, side="no")
    assert ctx.aligned is False
    assert ctx.context_score < 0.5


def test_momentum_flat_price_scores_neutralish():
    history = [_price_row(0.20, 14 - i) for i in range(7)]
    ctx = compute_momentum(history=history, side="yes")
    assert 0.4 <= ctx.context_score <= 0.6


# ---------------------------------------------------------------------------
# Whales
# ---------------------------------------------------------------------------


def test_whales_empty_positions_is_neutral():
    ctx = compute_whales(positions=[], side="yes")
    assert ctx.flow_usd == 0.0
    assert ctx.gross_usd == 0.0
    assert ctx.context_score == 0.5


def test_whales_aligned_flow_pushes_score_up():
    positions = [_Pos(trader_id=i, side="yes", size_usd=25_000) for i in range(3)]
    ctx = compute_whales(positions=positions, side="yes")
    assert ctx.flow_usd == 75_000.0
    assert ctx.context_score > 0.8
    assert ctx.unusual_accumulation is True


def test_whales_opposite_flow_pushes_score_down():
    positions = [_Pos(trader_id=i, side="no", size_usd=25_000) for i in range(3)]
    ctx = compute_whales(positions=positions, side="yes")
    assert ctx.flow_usd == -75_000.0
    assert ctx.context_score < 0.2


def test_whales_mixed_flow_averages_out():
    positions = [
        _Pos(trader_id=1, side="yes", size_usd=10_000),
        _Pos(trader_id=2, side="no", size_usd=10_000),
    ]
    ctx = compute_whales(positions=positions, side="yes")
    assert ctx.flow_usd == 0.0
    assert ctx.alignment_ratio == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def test_neutral_report_is_zero_adjustment():
    r = neutral_report()
    assert r.enabled is False
    assert r.edge_adjustment_score == 0.0
    assert r.market_context_score == 50.0


def test_aggregator_neutral_inputs_produce_zero_adjustment():
    agg = MarketIntelligenceAggregator(max_adjustment_pct=2.0)
    r = agg.compute(
        side="yes",
        micro=None,
        price_history=(),
        whale_positions=(),
    )
    assert r.enabled is True
    assert r.edge_adjustment_score == pytest.approx(0.0, abs=1e-6)
    assert 40.0 <= r.market_context_score <= 60.0


def test_aggregator_all_positive_clipped_to_max_adjustment():
    agg = MarketIntelligenceAggregator(
        max_adjustment_pct=2.0,
        weight_micro=1.0,
        weight_momentum=1.0,
        weight_whales=1.0,
    )
    healthy_book = _book(spread=0.002, bid_depth=5_000, ask_depth=5_000)
    strong_up = [_price_row(0.10 + i * 0.02, 14 - i) for i in range(10)]
    aligned_whales = [_Pos(trader_id=i, side="yes", size_usd=50_000) for i in range(5)]
    r = agg.compute(
        side="yes",
        micro=healthy_book,
        price_history=strong_up,
        whale_positions=aligned_whales,
    )
    assert r.edge_adjustment_score <= 2.0 + 1e-6  # never exceeds the clip
    assert r.edge_adjustment_score > 1.0  # but clearly positive
    assert r.market_context_score > 70


def test_aggregator_all_negative_clipped_to_min_adjustment():
    agg = MarketIntelligenceAggregator(
        max_adjustment_pct=2.0,
        weight_micro=1.0,
        weight_momentum=1.0,
        weight_whales=1.0,
    )
    ugly_book = _book(spread=0.06, bid_depth=50, ask_depth=50)  # 6 % spread, thin
    down_vs_yes = [_price_row(0.30 - i * 0.01, 14 - i) for i in range(10)]
    opposite_whales = [_Pos(trader_id=i, side="no", size_usd=50_000) for i in range(5)]
    r = agg.compute(
        side="yes",
        micro=ugly_book,
        price_history=down_vs_yes,
        whale_positions=opposite_whales,
    )
    assert r.edge_adjustment_score >= -2.0 - 1e-6
    assert r.edge_adjustment_score < -1.0
    assert r.market_context_score < 30


def test_aggregator_feature_dict_has_sub_reports_when_enabled():
    agg = MarketIntelligenceAggregator(max_adjustment_pct=2.0)
    r = agg.compute(
        side="yes",
        micro=_book(),
        price_history=[_price_row(0.2 + i * 0.001, 14 - i) for i in range(5)],
        whale_positions=[_Pos(1, "yes", 10_000)],
    )
    d = r.as_feature_dict()
    assert d["mi_enabled"] is True
    assert "mi_micro" in d
    assert "mi_momentum" in d
    assert "mi_whales" in d


def test_neutral_report_feature_dict_has_no_submodules():
    r = neutral_report()
    d = r.as_feature_dict()
    assert d == {
        "mi_enabled": False,
        "mi_context_score": 50.0,
        "mi_edge_adjustment_pct": 0.0,
    }
