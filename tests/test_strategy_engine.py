"""PrymStrategy — entry gate & sizing math."""
from __future__ import annotations

from app.integrations.mistral_client import AIAnalysis
from app.integrations.polymarket_client import MarketSnapshot
from app.services.signal_scoring import ScoreBreakdown
from app.strategies.prym_strategy import PrymStrategy


def _market(price: float = 0.2, volume: float = 50_000.0) -> MarketSnapshot:
    return MarketSnapshot(
        id="m1",
        slug="m1",
        question="Q",
        outcomes=["YES", "NO"],
        outcome_prices=[price, 1 - price],
        volume_24h=volume,
        liquidity=1_000,
        best_yes_price=price,
        best_no_price=1 - price,
    )


def _score(total: float = 80.0, phase: int = 2) -> ScoreBreakdown:
    return ScoreBreakdown(
        news=20,
        liquidity=18,
        mispricing=20,
        timing=16 if phase == 2 else 20,
        total=total,
        passes_alert=True,
        passes_trade=total >= 75 and phase in (1, 2),
        high_confidence=total >= 85,
        phase=phase,
        phase_label=f"p{phase}",
    )


def test_enters_on_all_conditions_met() -> None:
    strat = PrymStrategy()
    ai = AIAnalysis(market="X", impact="bullish", urgency=8)
    decision = strat.evaluate(ai=ai, market=_market(price=0.2), score=_score())
    assert decision.should_enter is True
    assert decision.side == "yes"


def test_bearish_picks_no_side() -> None:
    strat = PrymStrategy()
    ai = AIAnalysis(market="X", impact="bearish", urgency=8)
    decision = strat.evaluate(ai=ai, market=_market(price=0.2), score=_score())
    assert decision.side == "no"


def test_neutral_is_rejected() -> None:
    strat = PrymStrategy()
    ai = AIAnalysis(market=None, impact="neutral", urgency=10)
    decision = strat.evaluate(ai=ai, market=_market(), score=_score())
    assert decision.should_enter is False


def test_price_out_of_range_rejected() -> None:
    strat = PrymStrategy(min_price=0.05, max_price=0.35)
    ai = AIAnalysis(market="X", impact="bullish", urgency=8)
    decision = strat.evaluate(ai=ai, market=_market(price=0.8), score=_score())
    assert decision.should_enter is False


def test_score_below_threshold_rejected() -> None:
    strat = PrymStrategy()
    ai = AIAnalysis(market="X", impact="bullish", urgency=8)
    decision = strat.evaluate(ai=ai, market=_market(), score=_score(total=60))
    assert decision.should_enter is False


def test_phase_gate_blocks_late_entry() -> None:
    strat = PrymStrategy()
    ai = AIAnalysis(market="X", impact="bullish", urgency=8)
    decision = strat.evaluate(ai=ai, market=_market(), score=_score(phase=4))
    assert decision.should_enter is False


def test_sizing_high_score_uses_high_band() -> None:
    from app.config.settings import settings

    strat = PrymStrategy()
    plan = strat.sizing(balance=200.0, risk_pct=10.0, entry_price=0.2, score=90)
    # High band × balance, under MAX_TRADE_USD.
    assert plan.amount_usd == round(200.0 * settings.band_high_pct / 100.0, 2)
    # Fixed stop-loss is retired — trailing stop handles the downside.
    assert plan.stop_loss is None
    # Hard take-profit is ALSO retired under the repricing exit strategy:
    # exits are path-driven via the partial-TP ladder + trailing runner.
    assert plan.take_profit is None


def test_sizing_low_score_uses_low_band() -> None:
    strat = PrymStrategy()
    hi = strat.sizing(balance=200.0, risk_pct=10.0, entry_price=0.2, score=90)
    lo = strat.sizing(balance=200.0, risk_pct=10.0, entry_price=0.2, score=30)
    assert lo.amount_usd <= hi.amount_usd


def test_sizing_never_emits_fixed_stop_loss_or_take_profit() -> None:
    strat = PrymStrategy()
    # Both with and without the legacy ``stop_loss_enabled`` flag, the
    # resulting plan should never carry a hard-coded stop-loss price.
    # The repricing exit strategy also drops the hard take-profit ceiling
    # — the partial-TP ladder + trailing runner in the monitor are now
    # the sole exit rules (besides hard SL and time exit).
    on = strat.sizing(
        balance=1000.0, risk_pct=10.0, entry_price=0.2, stop_loss_enabled=True, score=80
    )
    off = strat.sizing(
        balance=1000.0, risk_pct=10.0, entry_price=0.2, stop_loss_enabled=False, score=60
    )
    assert on.stop_loss is None
    assert off.stop_loss is None
    assert on.take_profit is None
    assert off.take_profit is None
