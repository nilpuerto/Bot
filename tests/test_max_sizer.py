"""Unit tests for :mod:`app.services.max_sizer`."""
from __future__ import annotations

import pytest

from app.config.settings import settings
from app.services.max_sizer import size_for_entry


@pytest.fixture(autouse=True)
def _restore_settings():
    snapshot = (
        settings.max_min_confidence,
        settings.max_weak_confidence_floor,
        settings.max_weak_trade_fraction,
        settings.max_deadline_delta_abs_pct,
        settings.max_deadline_trade_fraction,
        settings.max_per_trade_cap_pct,
        settings.max_concurrent_cap_pct,
        settings.max_bankroll_fallback_pct,
        settings.min_trade_usd,
    )
    yield
    (
        settings.max_min_confidence,
        settings.max_weak_confidence_floor,
        settings.max_weak_trade_fraction,
        settings.max_deadline_delta_abs_pct,
        settings.max_deadline_trade_fraction,
        settings.max_per_trade_cap_pct,
        settings.max_concurrent_cap_pct,
        settings.max_bankroll_fallback_pct,
        settings.min_trade_usd,
    ) = snapshot


def test_weak_tier_scales_bet() -> None:
    settings.max_per_trade_cap_pct = 50.0
    sizing = size_for_entry(
        balance=1000.0,
        cumulative_profit=500.0,
        confidence=0.25,
    )
    assert sizing.amount_usd == pytest.approx(190.0)


def test_deadline_tier_below_weak_floor() -> None:
    settings.max_per_trade_cap_pct = 100.0
    sizing = size_for_entry(
        balance=1000.0,
        cumulative_profit=0.0,
        confidence=0.10,
        deadline_forced=True,
        window_delta_abs_pct=0.05,
    )
    assert sizing.amount_usd == pytest.approx(66.0)


def test_below_deadline_delta_still_zero_when_sub_weak() -> None:
    sizing = size_for_entry(
        balance=1000.0,
        cumulative_profit=0.0,
        confidence=0.10,
        deadline_forced=True,
        window_delta_abs_pct=0.01,
    )
    assert sizing.amount_usd == 0.0
def test_low_confidence_zeroes_bet() -> None:
    sizing = size_for_entry(
        balance=1_000.0,
        cumulative_profit=100.0,
        confidence=0.05,
    )
    assert sizing.amount_usd == 0.0
    assert "low_confidence" in sizing.reason


def test_no_profit_falls_back_to_30_percent() -> None:
    """Aggressive policy: no profit yet → 30 % of bankroll, hard-capped."""
    settings.max_per_trade_cap_pct = 30.0  # match fallback so cap is not the binder
    sizing = size_for_entry(
        balance=1_000.0,
        cumulative_profit=0.0,
        confidence=0.9,
    )
    assert sizing.fallback_used is True
    assert sizing.amount_usd == pytest.approx(300.0)


def test_negative_profit_treated_as_zero_for_fallback() -> None:
    settings.max_per_trade_cap_pct = 30.0
    sizing = size_for_entry(
        balance=1_000.0,
        cumulative_profit=-50.0,  # losses don't shrink below floor
        confidence=0.9,
    )
    assert sizing.fallback_used is True
    assert sizing.amount_usd == pytest.approx(300.0)


def test_with_profit_bets_exact_profit() -> None:
    """Cumulative profit > 0 → bet exactly the profit (within caps)."""
    sizing = size_for_entry(
        balance=1_000.0,
        cumulative_profit=80.0,
        confidence=0.9,
    )
    assert sizing.fallback_used is False
    assert sizing.amount_usd == pytest.approx(80.0)
    assert "profits" in sizing.reason


def test_per_trade_cap_clips_huge_profit() -> None:
    """Profit larger than per-trade cap → ticket = cap."""
    settings.max_per_trade_cap_pct = 10.0  # 10 % of $1000 = $100
    sizing = size_for_entry(
        balance=1_000.0,
        cumulative_profit=500.0,
        confidence=0.9,
    )
    assert sizing.amount_usd == pytest.approx(100.0)


def test_decisive_window_lifts_cap_25_percent() -> None:
    settings.max_per_trade_cap_pct = 10.0  # base cap = $100
    sizing = size_for_entry(
        balance=1_000.0,
        cumulative_profit=500.0,
        confidence=0.9,
        is_window_decisive=True,
    )
    # 10 % * 1.25 = 12.5 % → $125
    assert sizing.amount_usd == pytest.approx(125.0)


def test_concurrent_cap_clamps_total_exposure() -> None:
    settings.max_per_trade_cap_pct = 100.0
    settings.max_concurrent_cap_pct = 45.0  # $450 cap
    sizing = size_for_entry(
        balance=1_000.0,
        cumulative_profit=500.0,
        confidence=0.9,
        currently_open_usd=400.0,
    )
    # Headroom = 450 - 400 = 50
    assert sizing.amount_usd == pytest.approx(50.0)


def test_below_min_trade_usd_zeroes_amount() -> None:
    settings.min_trade_usd = 10.0
    sizing = size_for_entry(
        balance=10.0,           # 30% = 3 -> below $10 floor
        cumulative_profit=0.0,
        confidence=0.9,
    )
    assert sizing.amount_usd == 0.0
    assert "below_min" in sizing.reason


def test_zero_balance_short_circuits() -> None:
    sizing = size_for_entry(
        balance=0.0,
        cumulative_profit=0.0,
        confidence=0.9,
    )
    assert sizing.amount_usd == 0.0
    assert sizing.reason == "zero_balance"
