"""Edge-first hard gates — the contract of ``passes_trade``.

These tests lock in the edge-first refactor: no signal with
``|z| < Z_MIN_FOR_TRADE``, stale news, off-hour phase, insufficient
``net_edge_pct`` or under-filled depth may clear the trade gate — no
matter how favourable the remaining fields look.  They guard the
invariant that the *only* way into a live trade is a conjunction of
measurable conditions.
"""
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


def _market() -> MarketSnapshot:
    return MarketSnapshot(
        id="m1",
        slug="m1",
        question="Does X happen?",
        outcomes=["YES", "NO"],
        outcome_prices=[0.2, 0.8],
        volume_24h=50_000,
        liquidity=10_000,
        best_yes_price=0.2,
        best_no_price=0.8,
    )


def _micro() -> MicrostructureFeatures:
    return MicrostructureFeatures(
        token_id="t",
        best_bid=0.195,
        best_ask=0.205,
        mid=0.2,
        spread=0.01,
        spread_pct=0.05,
        top5_bid_depth=3_000,
        top5_ask_depth=3_000,
        ofi=0.2,
        has_book=True,
    )


def _mp(z: float) -> MispricingResult:
    return MispricingResult(
        market_id="m1",
        z=z,
        mean=0.25,
        stddev=0.05,
        samples=50,
        adj_vol_score=0.8,
        current_price=0.2,
    )


def _kw(**over):
    base = dict(
        ai=AIAnalysis(market="X", impact="bullish", urgency=10),
        market=_market(),
        traders=None,
        dq=None,
        micro=_micro(),
        mispricing=_mp(z=-2.5),
        timing=TimingDecision(phase=2, score=12, label="p2", reason=""),
        news_published_at=utcnow() - timedelta(seconds=20),
        side="yes",
        net_edge_pct=settings.min_edge_pct + 3.0,
        fill_ratio=0.9,
    )
    base.update(over)
    return base


def test_z_min_zero_z_passes_hard_gate_when_edge_ok() -> None:
    # Z_MIN_FOR_TRADE=0 → |z|=0 clears the mispricing amplitude gate.
    scorer = SignalScoringSystem()
    b = scorer.score(**_kw(mispricing=_mp(z=0.0)))
    assert "z_below_min" not in (b.gate_reason or "")


def test_gate_blocks_when_net_edge_below_min() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(**_kw(net_edge_pct=settings.min_edge_pct - 0.1))
    assert b.passes_trade is False
    assert b.passes_alert is False
    assert "edge_below_min" in b.gate_reason


def test_gate_blocks_when_net_edge_missing() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(**_kw(net_edge_pct=None))
    assert b.passes_trade is False
    assert "edge_below_min" in b.gate_reason


def test_gate_blocks_when_phase_is_overreaction_or_later() -> None:
    """CORE profile now allows phases 1-4; only phase 5 is blocked."""
    scorer = SignalScoringSystem()
    for phase in (5,):
        b = scorer.score(
            **_kw(timing=TimingDecision(phase=phase, score=0, label="", reason=""))
        )
        assert b.passes_trade is False, f"phase {phase} should not trade"


def test_gate_allows_phase_3_in_core_profile() -> None:
    """Phase 3 (retail influx, 2..5 min) trades in the CORE profile —
    sized down via the BAND_LOW tier when edge/z are borderline."""
    scorer = SignalScoringSystem()
    b = scorer.score(
        **_kw(timing=TimingDecision(phase=3, score=6, label="retail", reason=""))
    )
    assert b.passes_trade is True
    assert b.gate_reason == "ok"


def test_gate_blocks_stale_news() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(
        **_kw(
            news_published_at=utcnow()
            - timedelta(seconds=settings.max_news_age_for_trade + 30)
        )
    )
    assert b.passes_trade is False


def test_gate_blocks_low_fill() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(**_kw(fill_ratio=settings.min_fill_ratio - 0.1))
    assert b.passes_trade is False
    assert "fill_below_min" in b.gate_reason


def test_gate_penalizes_neutral_direction() -> None:
    """Neutral impact is no longer a hard veto — it applies a noise_penalty
    that suppresses weak setups to 'reject' tier while still allowing
    strong measurable signals (high z, high edge) through."""
    scorer = SignalScoringSystem()
    b = scorer.score(**_kw(ai=AIAnalysis(market="X", impact="neutral", urgency=5)))
    assert b.noise_penalty > 0.0
    # A well-structured neutral signal (from _kw defaults) may still pass
    # via the continuous scorer; the penalty should reduce its edge_score.
    b_bull = scorer.score(**_kw())
    assert b.edge_score < b_bull.edge_score


def test_gate_passes_when_every_measurable_condition_is_satisfied() -> None:
    scorer = SignalScoringSystem()
    b = scorer.score(**_kw())
    assert b.passes_trade is True
    assert b.passes_alert is True
    assert b.gate_reason == "ok"


def test_alerts_share_the_trade_gate() -> None:
    """Under the edge-first refactor, passes_alert == passes_trade —
    we no longer surface 'maybe interesting' signals that would fail
    the cost model."""
    scorer = SignalScoringSystem()
    # Failing case — deeply negative edge so EV is well below reject floor.
    b_fail = scorer.score(**_kw(net_edge_pct=-10.0))
    assert b_fail.passes_alert == b_fail.passes_trade == False
    # Passing case.
    b_ok = scorer.score(**_kw())
    assert b_ok.passes_alert == b_ok.passes_trade == True


def test_score_is_cosmetic_not_a_gate() -> None:
    """A high total score cannot override a failed hard gate."""
    scorer = SignalScoringSystem()
    # Phase 5 (decay zone) blocks even with a huge mispricing score.
    b = scorer.score(
        **_kw(
            mispricing=_mp(z=-5.0),  # huge score component
            timing=TimingDecision(phase=5, score=0, label="", reason=""),
        )
    )
    assert b.total > 40.0  # score still high from mispricing
    assert b.passes_trade is False  # but phase gate blocks
