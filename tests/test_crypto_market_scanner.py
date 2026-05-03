"""Unit tests for :mod:`app.services.crypto_market_scanner`."""
from __future__ import annotations

from datetime import timedelta

from app.integrations.polymarket_client import MarketSnapshot
from app.services.crypto_market_scanner import classify
from app.utils.time import utcnow


def _make_market(
    *,
    slug: str,
    question: str = "Bitcoin up or down?",
    end_in_seconds: int = 300,
) -> MarketSnapshot:
    end = (utcnow() + timedelta(seconds=end_in_seconds)).isoformat().replace("+00:00", "Z")
    return MarketSnapshot(
        id="m1",
        slug=slug,
        question=question,
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


def test_classify_5m_window() -> None:
    m = _make_market(slug="bitcoin-up-or-down-300pm-est", end_in_seconds=240)
    cm = classify(m)
    assert cm is not None
    assert cm.horizon == "5m"
    assert cm.strike_kind == "above_open"


def test_classify_1h_window() -> None:
    m = _make_market(slug="bitcoin-up-or-down-1h-march-3", end_in_seconds=45 * 60)
    cm = classify(m)
    assert cm is not None
    assert cm.horizon == "1h"


def test_classify_1d_window() -> None:
    m = _make_market(
        slug="bitcoin-eod-march-15",
        question="Will BTC close above $72,500 today?",
        end_in_seconds=12 * 3600,
    )
    cm = classify(m)
    assert cm is not None
    assert cm.horizon == "1d"
    assert cm.strike_kind == "absolute"
    assert cm.strike == 72_500.0


def test_classify_strike_with_k_suffix() -> None:
    m = _make_market(
        slug="bitcoin-eod",
        question="Will BTC be above 72k by end of day?",
        end_in_seconds=10 * 3600,
    )
    cm = classify(m)
    assert cm is not None
    assert cm.strike == 72_000.0


def test_classify_skips_non_bitcoin() -> None:
    m = _make_market(slug="ethereum-eod", question="Will ETH close above $3,500 today?")
    assert classify(m) is None


def test_classify_skips_expired() -> None:
    m = _make_market(slug="bitcoin-up-or-down-1pm", end_in_seconds=-30)
    assert classify(m) is None


def test_classify_unknown_slug_falls_through() -> None:
    m = _make_market(slug="random-binary", question="Will it rain in Paris?")
    assert classify(m) is None
