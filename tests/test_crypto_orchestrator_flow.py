"""End-to-end smoke tests for :mod:`app.core.crypto_orchestrator`.

These tests exercise the *decision pipeline* (snapshot -> pricer ->
TA -> overlay -> sizer) without touching the database or the network.
The full orchestrator's ``_on_new_market`` is mocked at the persistence
boundary; we still call the pure functions to assert end-to-end logic
matches expectations.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.integrations.polymarket_client import MarketSnapshot
from app.services.crypto_market_scanner import classify
from app.services.crypto_news_overlay import CryptoNewsOverlay
from app.services.crypto_sizer import first_entry_size
from app.services.lag_arb_pricer import choose_side, fair_prob_above
from app.services.ta_confluence import Candle, score as ta_score
from app.utils.time import utcnow


def _market(seconds_left: int = 240) -> MarketSnapshot:
    end = (utcnow() + timedelta(seconds=seconds_left)).isoformat().replace("+00:00", "Z")
    return MarketSnapshot(
        id="m1",
        slug="bitcoin-up-or-down-3pm-est",
        question="Bitcoin up or down?",
        outcomes=["Yes", "No"],
        outcome_prices=[0.50, 0.50],
        volume_24h=1000.0,
        liquidity=100.0,
        best_yes_price=0.50,
        best_no_price=0.50,
        end_date=end,
        yes_token_id="t_yes",
        no_token_id="t_no",
    )


def _flat_candles(closes: list[float]) -> list[Candle]:
    out: list[Candle] = []
    for i, c in enumerate(closes):
        out.append(
            Candle(
                open_time_ms=i * 60_000,
                open=c,
                high=c + 0.05,
                low=c - 0.05,
                close=c,
                volume=10.0,
            )
        )
    return out


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_first_anchor_pct", 27.0)
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_per_trade_cap_pct", 12.0)
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_concurrent_cap_pct", 45.0)
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_kelly_fraction", 0.25)
    monkeypatch.setattr("app.services.crypto_sizer.settings.min_trade_usd", 2.0)


def test_full_pipeline_passes_strong_setup() -> None:
    """Spot 5 % above strike, mid-priced market, oversold RSI -> trade."""
    cm = classify(_market(seconds_left=240))
    assert cm is not None

    spot = 105.0
    strike = 100.0
    sigma_per_sec = 0.0002
    seconds_left = cm.seconds_left

    p_fair_yes = fair_prob_above(spot, strike, sigma_per_sec, seconds_left)
    quote = choose_side(
        p_fair_yes, ask_yes=0.50, ask_no=0.50, fee_bps=180.0, slip_bps=60.0
    )
    assert quote is not None
    assert quote.side == "yes"
    assert quote.edge_pct >= 3.5  # default crypto_min_edge_pct

    # TA: declining series prior is irrelevant here — we just want at
    # least one indicator on the LONG side.  Force a very oversold RSI.
    candles = _flat_candles([100.0 - i * 0.5 for i in range(40)])
    ta = ta_score(candles, "long")
    # Long RSI is oversold so confluence >= 1 expected.
    assert ta.confluence >= 1

    # Sizing on a $1000 balance: kelly = edge_pct/100 * 0.25 (fraction).
    sizing = first_entry_size(balance=1_000.0, edge_pct=quote.edge_pct)
    assert sizing.amount_usd > 0
    assert sizing.reason == "ok"


def test_full_pipeline_blocked_by_no_edge() -> None:
    """Spot ~ strike => p_fair ~ 0.5, ask 0.50 => no edge."""
    cm = classify(_market(seconds_left=240))
    assert cm is not None
    p_fair_yes = fair_prob_above(100.0, 100.0, 0.0002, cm.seconds_left)
    quote = choose_side(
        p_fair_yes, ask_yes=0.50, ask_no=0.50, fee_bps=180.0, slip_bps=60.0
    )
    assert quote is None


def test_full_pipeline_news_veto_blocks_1d() -> None:
    """Bearish BTC news + LONG side + 1d horizon -> overlay veto."""
    overlay = CryptoNewsOverlay()
    overlay.record(
        title="Bitcoin crashes after exchange hack",
        impact="bearish",
        urgency=10,
        entities=["bitcoin"],
    )
    decision = overlay.modifier("yes", "1d")
    assert decision.action == "veto"
