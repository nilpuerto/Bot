"""Unit tests for :mod:`app.services.crypto_news_overlay`."""
from __future__ import annotations

import pytest

from app.services.crypto_news_overlay import CryptoNewsOverlay


@pytest.fixture(autouse=True)
def _enable_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.crypto_news_overlay.settings.crypto_news_overlay_enabled", True
    )
    monkeypatch.setattr(
        "app.services.crypto_news_overlay.settings.crypto_news_window_minutes", 30
    )


def test_non_btc_news_ignored() -> None:
    o = CryptoNewsOverlay()
    o.record(title="Apple beats earnings", impact="bullish", urgency=8, entities=["AAPL"])
    assert o.sentiment == 0.0


def test_btc_bullish_pushes_sentiment_positive() -> None:
    o = CryptoNewsOverlay()
    o.record(
        title="Bitcoin ETF approved by SEC",
        impact="bullish",
        urgency=9,
        entities=["bitcoin"],
    )
    assert o.sentiment > 0


def test_btc_bearish_pushes_sentiment_negative() -> None:
    o = CryptoNewsOverlay()
    o.record(title="BTC hack drains exchange", impact="bearish", urgency=8, entities=["btc"])
    assert o.sentiment < 0


def test_5m_horizon_always_holds() -> None:
    o = CryptoNewsOverlay()
    o.record(title="Bitcoin ETF approved", impact="bullish", urgency=10, entities=["bitcoin"])
    decision = o.modifier("yes", "5m")
    assert decision.action == "hold"
    assert decision.scale == 1.0


def test_1h_aligned_boosts() -> None:
    o = CryptoNewsOverlay()
    o.record(title="Bitcoin rally accelerates", impact="bullish", urgency=9, entities=["bitcoin"])
    decision = o.modifier("yes", "1h")
    assert decision.action == "boost"
    assert decision.scale > 1.0


def test_1h_opposes_shrinks() -> None:
    o = CryptoNewsOverlay()
    o.record(title="Bitcoin rally accelerates", impact="bullish", urgency=9, entities=["bitcoin"])
    decision = o.modifier("no", "1h")
    assert decision.action == "shrink"
    assert decision.scale < 1.0


def test_1d_strong_contradiction_vetoes() -> None:
    o = CryptoNewsOverlay()
    o.record(title="Bitcoin crashes after exchange hack", impact="bearish", urgency=10, entities=["bitcoin"])
    decision = o.modifier("yes", "1d")
    assert decision.action == "veto"
    assert decision.scale == 0.0


def test_disabled_returns_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.crypto_news_overlay.settings.crypto_news_overlay_enabled", False
    )
    o = CryptoNewsOverlay()
    o.record(title="Bitcoin ETF approved", impact="bullish", urgency=10, entities=["bitcoin"])
    # record() short-circuits when disabled, so no points stored.
    decision = o.modifier("yes", "1h")
    assert decision.action == "hold"
    assert decision.scale == 1.0
