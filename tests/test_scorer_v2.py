"""Edge-first scorer — caps, weights, gate behaviour."""
from __future__ import annotations

from app.integrations.mistral_client import AIAnalysis
from app.integrations.polymarket_client import MarketSnapshot
from app.services.microstructure import MicrostructureFeatures
from app.services.mispricing import MispricingResult
from app.services.signal_scoring import (
    CAP_LIQUIDITY,
    CAP_MISPRICING,
    CAP_NEWS,
    CAP_TIMING,
    SignalScoringSystem,
)
from app.services.timing import TimingDecision


def _market() -> MarketSnapshot:
    return MarketSnapshot(
        id="m",
        slug="m",
        question="Does X?",
        outcomes=["Yes", "No"],
        outcome_prices=[0.2, 0.8],
        volume_24h=30_000,
        liquidity=5_000,
        best_yes_price=0.2,
        best_no_price=0.8,
    )


def _micro(spread: float = 0.01, bid_depth: float = 3000, ask_depth: float = 3000) -> MicrostructureFeatures:
    return MicrostructureFeatures(
        token_id="t",
        best_bid=0.195,
        best_ask=0.205,
        mid=0.2,
        spread=spread,
        spread_pct=spread / 0.2,
        top5_bid_depth=bid_depth,
        top5_ask_depth=ask_depth,
        ofi=0.0,
        has_book=True,
    )


def _mp(z: float = -2.5, adj: float = 0.8) -> MispricingResult:
    return MispricingResult(
        market_id="m",
        z=z,
        mean=0.3,
        stddev=0.04,
        samples=50,
        adj_vol_score=adj,
    )


def test_edge_first_caps() -> None:
    """News is now a hard gate (0 pts); mispricing dominates (60 cap)."""
    assert CAP_NEWS == 0.0
    assert CAP_MISPRICING == 60.0
    assert CAP_LIQUIDITY == 25.0
    assert CAP_TIMING == 15.0


def test_total_capped_at_100_with_default_weights() -> None:
    scorer = SignalScoringSystem()
    ai = AIAnalysis(market="X", impact="bullish", urgency=10)
    breakdown = scorer.score(
        ai=ai,
        market=_market(),
        micro=_micro(),
        mispricing=_mp(),
        timing=TimingDecision(phase=2, score=16, label="breaking_reaction", reason=""),
        side="yes",
    )
    assert breakdown.news <= CAP_NEWS
    assert breakdown.liquidity <= CAP_LIQUIDITY
    assert breakdown.mispricing <= CAP_MISPRICING
    assert breakdown.timing <= CAP_TIMING
    assert breakdown.total <= 100.01


def test_phase_4_cannot_pass_trade_gate_even_with_high_score() -> None:
    scorer = SignalScoringSystem(trade_threshold=0)  # force score to pass
    ai = AIAnalysis(market="X", impact="bullish", urgency=10)
    breakdown = scorer.score(
        ai=ai,
        market=_market(),
        micro=_micro(),
        mispricing=_mp(),
        timing=TimingDecision(phase=4, score=0, label="overreaction", reason=""),
        side="yes",
    )
    assert breakdown.passes_trade is False  # phase gate blocks


def test_weights_amplify_up_to_cap() -> None:
    """With clip ceilings, pillar contributions remain ≤ their caps."""
    scorer = SignalScoringSystem(
        weights={
            "news": 1.15,
            "liquidity": 1.15,
            "mispricing": 1.15,
            "timing": 1.15,
        }
    )
    ai = AIAnalysis(market="X", impact="bullish", urgency=10)
    breakdown = scorer.score(
        ai=ai,
        market=_market(),
        micro=_micro(),
        mispricing=_mp(),
        timing=TimingDecision(phase=1, score=20, label="leak", reason=""),
        side="yes",
    )
    assert breakdown.news <= CAP_NEWS
    assert breakdown.liquidity <= CAP_LIQUIDITY
    assert breakdown.mispricing <= CAP_MISPRICING
    assert breakdown.timing <= CAP_TIMING


def test_feature_vector_contains_raw_pillars_for_feedback_loop() -> None:
    scorer = SignalScoringSystem()
    ai = AIAnalysis(market="X", impact="bullish", urgency=8)
    breakdown = scorer.score(
        ai=ai,
        market=_market(),
        micro=_micro(),
        mispricing=_mp(),
        timing=TimingDecision(phase=2, score=16, label="break", reason=""),
        side="yes",
    )
    for key in ("news_raw", "liquidity_raw", "mispricing_raw", "timing_raw"):
        assert key in breakdown.feature_vector
        assert 0.0 <= breakdown.feature_vector[key] <= 1.0
