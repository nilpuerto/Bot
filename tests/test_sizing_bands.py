"""Sizing engine — balance-% banded sizing."""
from __future__ import annotations

from app.config.settings import settings
from app.services.sizing import band_bounds, band_for_score, band_pct, compute_sizing


def test_low_band_for_low_score() -> None:
    assert band_for_score(10) == "low"
    assert band_pct("low") == settings.band_low_pct


def test_mid_band_for_mid_score() -> None:
    assert band_for_score(60) == "mid"
    assert band_pct("mid") == settings.band_mid_pct


def test_high_band_for_high_score() -> None:
    assert band_for_score(90) == "high"
    assert band_pct("high") == settings.band_high_pct


def test_anchor_scales_with_balance_in_auto_mode() -> None:
    balance = 200.0
    q = compute_sizing(score=90, balance=balance, risk_pct=settings.band_high_pct)
    # 10 % of 200 = 20, under MAX_TRADE_USD (25) — anchor used as-is.
    assert q.band == "high"
    assert q.anchor == round(balance * settings.band_high_pct / 100.0, 2)
    assert q.amount_usd == q.anchor


def test_anchor_capped_by_max_trade_usd_on_large_balance() -> None:
    balance = 10_000.0
    q = compute_sizing(score=90, balance=balance, risk_pct=settings.band_high_pct)
    assert q.anchor == round(balance * settings.band_high_pct / 100.0, 2)
    assert q.amount_usd == settings.max_trade_usd
    assert q.capped_by == "max_trade_usd"


def test_small_balance_scales_down() -> None:
    # Mid-to-large balance: the balance-% anchor survives the floor and
    # ceiling and is returned as-is.  Picks a balance big enough that
    # even the ``high`` band × pct stays above ``MIN_TRADE_USD``.
    balance = max(200.0, settings.min_trade_usd * 200.0)
    q = compute_sizing(score=90, balance=balance, risk_pct=settings.band_high_pct)
    assert q.band == "high"
    anchor = round(balance * settings.band_high_pct / 100.0, 2)
    expected = min(settings.max_trade_usd, anchor)
    assert q.amount_usd == expected


def test_tiny_balance_bumps_to_floor_with_flag() -> None:
    # 15€ balance × 10 % = 1.5€ which is below MIN_TRADE_USD ($2).  We
    # bump to the floor but flag so the caller can skip.
    q = compute_sizing(score=90, balance=15.0, risk_pct=settings.band_high_pct)
    assert q.amount_usd == settings.min_trade_usd
    assert q.capped_by == "min_trade_usd"


def test_risk_pct_tightens_band() -> None:
    # User set risk_pct below the high-band %, so risk_pct wins.
    tighter = max(0.5, settings.band_high_pct - 1.0)
    q = compute_sizing(score=90, balance=200.0, risk_pct=tighter)
    assert q.amount_usd == round(200.0 * tighter / 100.0, 2)
    assert q.capped_by == "risk_pct"


def test_risk_pct_never_widens_band() -> None:
    # User set risk_pct=50 but band is low (3 %) — band wins, not 50 %.
    q = compute_sizing(score=10, balance=200.0, risk_pct=50.0)
    assert q.band == "low"
    # 3 % of 200 = 6 (below MAX_TRADE_USD).
    assert q.amount_usd == round(200.0 * settings.band_low_pct / 100.0, 2)


def test_user_override_respected_within_hard_cap() -> None:
    # Pin ``max_trade_usd`` explicitly so this test does not become a
    # tripwire whenever the deployer tightens their MAX_TRADE_USD in
    # .env (legitimate ops change, not a sizing-logic regression).
    q = compute_sizing(
        score=90,
        balance=1_000.0,
        risk_pct=10.0,
        user_override=22.0,
        max_trade_usd=25.0,
    )
    # Override is within the local guard-rails — respected regardless of
    # the band USD edges derived from balance.
    assert q.amount_usd == 22.0


def test_user_override_can_go_below_band_min() -> None:
    # On a large balance the band's lower USD edge is high, but the user
    # can still override down to MIN_TRADE_USD — they picked the amount.
    q = compute_sizing(
        score=90, balance=1_000.0, risk_pct=10.0, user_override=3.0
    )
    assert q.amount_usd == 3.0


def test_override_clamped_by_hard_cap() -> None:
    q = compute_sizing(
        score=90, balance=100_000.0, risk_pct=50.0, user_override=500.0
    )
    assert q.amount_usd <= settings.max_trade_usd


def test_band_bounds_scale_with_balance() -> None:
    # Balance small enough that MAX_TRADE_USD doesn't kick in.
    lo, hi = band_bounds("high", balance=200.0)
    assert lo == max(settings.min_trade_usd, 200.0 * settings.band_high_pct / 2.0 / 100.0)
    assert hi == min(settings.max_trade_usd, 200.0 * settings.band_high_pct / 100.0)
    assert lo <= hi


def test_band_bounds_collapse_under_max_cap() -> None:
    # Huge balance: both band edges hit MAX_TRADE_USD — they collapse.
    lo, hi = band_bounds("high", balance=1_000_000.0)
    assert hi == settings.max_trade_usd
    assert lo == hi


# ---- LOW_PROB band --------------------------------------------------------

def test_low_prob_band_overrides_edge_tier() -> None:
    """A cheap entry price forces the tiny LOW_PROB band even when the
    edge + |z| would otherwise qualify for the high band.
    """
    from app.services.sizing import tier_from_edge

    band = tier_from_edge(
        net_edge_pct=20.0,
        abs_z=5.0,
        entry_price=settings.low_prob_entry_price - 0.01,
    )
    assert band == "low_prob"
    assert band_pct("low_prob") == settings.band_low_prob_pct


def test_low_prob_band_sizes_tiny() -> None:
    """LOW_PROB band commits ``band_low_prob_pct`` of balance."""
    balance = 1_000.0
    q = compute_sizing(
        balance=balance,
        risk_pct=settings.band_high_pct,
        net_edge_pct=20.0,
        abs_z=5.0,
        entry_price=settings.low_prob_entry_price - 0.01,
    )
    assert q.band == "low_prob"
    expected = round(balance * settings.band_low_prob_pct / 100.0, 2)
    expected = min(expected, settings.max_trade_usd)
    expected = max(expected, settings.min_trade_usd)
    assert q.amount_usd == expected


def test_normal_price_does_not_trigger_low_prob() -> None:
    """Prices above ``LOW_PROB_ENTRY_PRICE`` follow the edge/z tier rules."""
    from app.services.sizing import tier_from_edge

    band = tier_from_edge(
        net_edge_pct=20.0,
        abs_z=5.0,
        entry_price=settings.low_prob_entry_price + 0.05,
    )
    assert band == "high"
