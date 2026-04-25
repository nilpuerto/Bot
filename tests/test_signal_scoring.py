"""Scoring math + threshold behaviour (edge-first refactor)."""
from __future__ import annotations

from datetime import timedelta

from app.config.settings import settings
from app.integrations.mistral_client import AIAnalysis
from app.integrations.polymarket_client import MarketSnapshot
from app.services.microstructure import MicrostructureFeatures
from app.services.mispricing import MispricingResult
from app.services.signal_scoring import SignalScoringSystem
from app.services.timing import TimingDecision
from app.utils.time import utcnow


def _market(price: float = 0.2, volume: float = 50_000.0) -> MarketSnapshot:
    return MarketSnapshot(
        id="m1",
        slug="m1",
        question="Does X happen?",
        outcomes=["YES", "NO"],
        outcome_prices=[price, 1 - price],
        volume_24h=volume,
        liquidity=10_000,
        best_yes_price=price,
        best_no_price=1 - price,
    )


def _micro(spread: float = 0.005, depth: float = 3_000.0) -> MicrostructureFeatures:
    best_bid = 0.19
    best_ask = 0.19 + spread
    mid = (best_bid + best_ask) / 2
    return MicrostructureFeatures(
        token_id="tok",
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        spread_pct=spread / mid if mid else None,
        top5_bid_depth=depth,
        top5_ask_depth=depth,
        ofi=0.2,
        has_book=True,
    )


def _mispricing(z: float = -2.5) -> MispricingResult:
    return MispricingResult(
        market_id="m1",
        z=z,
        mean=0.25,
        stddev=0.05,
        samples=50,
        adj_vol_score=0.8,
        current_price=0.2,
    )


def _timing(phase: int = 2) -> TimingDecision:
    scores = {1: 20.0, 2: 16.0, 3: 6.0, 4: 0.0, 5: 0.0}
    return TimingDecision(phase=phase, score=scores[phase], label=f"p{phase}", reason="")


def _strong_kwargs(**overrides):
    """A fully-valid scoring input — every hard gate passes."""
    base = dict(
        ai=AIAnalysis(market="X", impact="bullish", urgency=10),
        market=_market(price=0.18),
        traders=None,
        dq=None,
        micro=_micro(),
        mispricing=_mispricing(z=-2.5),
        timing=_timing(2),
        news_published_at=utcnow() - timedelta(seconds=15),
        side="yes",
        net_edge_pct=settings.min_edge_pct + 2.0,
        fill_ratio=0.95,
    )
    base.update(overrides)
    return base


def test_all_gates_passing_yields_passes_trade() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(**_strong_kwargs())
    assert b.passes_alert is True
    assert b.passes_trade is True
    assert b.gate_reason == "ok"


def test_neutral_impact_fails_direction_gate() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(**_strong_kwargs(ai=AIAnalysis(market="X", impact="neutral", urgency=5)))
    assert b.passes_trade is False
    assert b.gate_reason == "neutral_impact"


def test_phase_outside_1_or_2_blocks_trade() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(**_strong_kwargs(timing=_timing(4)))
    assert b.passes_trade is False
    assert "phase_4" in b.gate_reason


def test_low_z_blocks_trade() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(**_strong_kwargs(mispricing=_mispricing(z=-1.0)))
    assert b.passes_trade is False
    assert "z_below_min" in b.gate_reason


def test_low_edge_blocks_trade() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(**_strong_kwargs(net_edge_pct=settings.min_edge_pct - 0.5))
    assert b.passes_trade is False
    assert "edge_below_min" in b.gate_reason


def test_stale_news_blocks_trade() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(
        **_strong_kwargs(
            news_published_at=utcnow()
            - timedelta(seconds=settings.max_news_age_for_trade + 120)
        )
    )
    assert b.passes_trade is False
    assert "stale" in b.gate_reason


def test_low_fill_blocks_trade() -> None:
    scorer = SignalScoringSystem()
    # Pick a fill ratio strictly below MIN_FILL_RATIO so the gate fires
    # regardless of how the deployer tunes the floor in .env.
    too_low = max(0.0, settings.min_fill_ratio - 0.1)
    b = scorer.score(**_strong_kwargs(fill_ratio=too_low))
    assert b.passes_trade is False
    assert "fill_below_min" in b.gate_reason


def test_news_pillar_contributes_zero() -> None:
    """News is a hard gate, not a score pillar.  It must never add points."""
    scorer = SignalScoringSystem()
    b = scorer.score(**_strong_kwargs())
    assert b.news == 0.0


def test_weights_only_affect_learnable_pillars() -> None:
    """news + timing are pinned at 1.0 — only mispricing + liquidity learn."""
    base = SignalScoringSystem()
    boosted = SignalScoringSystem(
        weights={
            "news": 1.5,
            "liquidity": settings.feedback_clip_high,
            "mispricing": settings.feedback_clip_high,
            "timing": 1.5,
        }
    )
    kwargs = _strong_kwargs()
    assert boosted.score(**kwargs).mispricing >= base.score(**kwargs).mispricing
    assert boosted.score(**kwargs).liquidity >= base.score(**kwargs).liquidity
    # News stays zero regardless of any weight.
    assert boosted.score(**kwargs).news == 0.0


def test_mispricing_is_the_dominant_pillar() -> None:
    """Under the edge-first caps, mispricing has a 60-point ceiling while
    liquidity caps at 25 and timing at 15."""
    scorer = SignalScoringSystem()
    b = scorer.score(**_strong_kwargs())
    assert b.mispricing >= b.liquidity
    assert b.mispricing >= b.timing


# ---- LOW-PROB profile gate ------------------------------------------------

def _low_prob_price() -> float:
    return max(0.02, settings.low_prob_entry_price - 0.02)


def test_low_prob_detected_by_entry_price() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(
        **_strong_kwargs(
            entry_price=_low_prob_price(),
            mispricing=_mispricing(z=-3.0),
            net_edge_pct=settings.low_prob_min_edge_pct + 1.0,
            timing=_timing(1),
        )
    )
    assert b.passes_trade is True
    assert b.feature_vector.get("is_low_prob") is True


def test_low_prob_requires_tighter_z() -> None:
    """CORE would pass at z=1.8 (>= 1.5); LOW-PROB demands z >= 2.5."""
    scorer = SignalScoringSystem()
    b = scorer.score(
        **_strong_kwargs(
            entry_price=_low_prob_price(),
            mispricing=_mispricing(z=-1.8),
            net_edge_pct=settings.low_prob_min_edge_pct + 1.0,
            timing=_timing(1),
        )
    )
    assert b.passes_trade is False
    assert "z_below_min" in b.gate_reason


def test_low_prob_requires_tighter_edge() -> None:
    """CORE would pass at edge=4% (>= 3%); LOW-PROB demands edge >= 8%."""
    scorer = SignalScoringSystem()
    b = scorer.score(
        **_strong_kwargs(
            entry_price=_low_prob_price(),
            mispricing=_mispricing(z=-3.0),
            net_edge_pct=settings.min_edge_pct + 1.0,  # clears CORE, not LOW-PROB
            timing=_timing(1),
        )
    )
    assert b.passes_trade is False
    assert "edge_below_min" in b.gate_reason


def test_low_prob_rejects_phase_two() -> None:
    """Phase 2 is allowed for CORE but rejected for LOW-PROB (phase 1 only)."""
    scorer = SignalScoringSystem()
    b = scorer.score(
        **_strong_kwargs(
            entry_price=_low_prob_price(),
            mispricing=_mispricing(z=-3.0),
            net_edge_pct=settings.low_prob_min_edge_pct + 1.0,
            timing=_timing(2),
        )
    )
    assert b.passes_trade is False
    assert "phase_2" in b.gate_reason


def test_core_profile_allows_phase_two_and_relaxed_thresholds() -> None:
    """At a non-LOW-PROB price, phase 2 + z=1.6 + edge=4% must pass."""
    scorer = SignalScoringSystem()
    b = scorer.score(
        **_strong_kwargs(
            entry_price=0.25,
            mispricing=_mispricing(z=-1.6),
            net_edge_pct=settings.min_edge_pct + 1.0,
            timing=_timing(2),
        )
    )
    assert b.passes_trade is True
    assert b.feature_vector.get("is_low_prob") is False
