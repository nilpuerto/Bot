"""Tests for the EV estimator — core formula, tier mapping and edge cases."""
from __future__ import annotations

import pytest

from app.config.settings import settings
from app.services.ev_estimator import compute_ev


def test_strong_setup_is_core() -> None:
    """High z, good context, decent edge → EV well above core min → core tier."""
    r = compute_ev(
        net_edge_pct=8.0,
        abs_z=3.0,
        context_score=0.9,
        entry_price=0.25,
    )
    assert r.ev > float(settings.ev_core_min)
    assert r.tier == "core"
    assert r.p_edge_real > 0.5


def test_moderate_setup_is_mid() -> None:
    """Thin net_edge + moderate z → EV above opp_min but below core → mid.

    With the recalibrated boosters (z_boost=0.15/unit, BASE_P=0.55) a signal
    with net_edge_pct=0.8% stays in mid because EV is clearly positive but
    too small to reach EV_CORE_MIN.  This tests that the mid tier is reachable
    without hitting the hard-gate floor (min_edge_pct=2% applies at the
    orchestrator level, not inside the EV estimator itself).
    """
    r = compute_ev(
        net_edge_pct=0.8,   # thin — just enough for positive EV at high p
        abs_z=1.5,
        context_score=0.6,
        entry_price=0.30,
    )
    assert float(settings.ev_opp_min) <= r.ev < float(settings.ev_core_min)
    assert r.tier == "mid"


def test_zero_edge_is_reject() -> None:
    """No edge → EV is negative → reject."""
    r = compute_ev(
        net_edge_pct=0.0,
        abs_z=0.5,
        context_score=0.3,
        entry_price=0.50,
    )
    assert r.ev < 0.0
    assert r.tier == "reject"


def test_negative_edge_is_reject() -> None:
    r = compute_ev(
        net_edge_pct=-2.0,
        abs_z=0.0,
        context_score=0.0,
        entry_price=0.50,
    )
    assert r.ev < 0.0
    assert r.tier == "reject"


def test_exploratory_low_price_thin_ev_is_low_tier() -> None:
    """Low-prob entry with z=0 → EV_NO_Z_PENALTY applied → exploratory low tier.

    With EV_NO_Z_PENALTY=0.65 and EV_BASE_P=0.55:
      p_edge_real = 0.55 × 0.65 = 0.3575
      EV = 0.3575 × 1.5 − 0.6425 × 1.5 ≈ −0.4275

    −0.4275 is between EV_EXPLORATORY_MIN_EV (−0.6) and EV_OPP_MIN (0.0)
    and the payout_ratio for a low-price entry exceeds EV_EXPLORATORY_PAYOUT_MIN,
    so the signal qualifies as the "low" (exploratory) tier.
    """
    low_price = float(settings.low_prob_entry_price) - 0.01
    r = compute_ev(
        net_edge_pct=1.50,  # enough edge to stay above exploratory floor with penalty
        abs_z=0.0,          # no z boost + penalty applied
        context_score=0.0,  # no context boost
        entry_price=low_price,
    )
    assert r.payout_ratio >= float(settings.ev_exploratory_payout_min)
    assert r.ev > float(settings.ev_exploratory_min_ev)
    assert r.ev < float(settings.ev_opp_min)
    assert r.is_exploratory is True
    assert r.tier == "low"


def test_exploratory_with_decent_ev_goes_to_mid() -> None:
    """Low-prob entry + decent EV → mid tier, not exploratory-low.

    When EV > EV_OPP_MIN the signal gets standard mid sizing (not tiny
    exploratory size), even though entry_price is in the low-prob range.
    This is correct: if the EV is genuinely positive, don't penalise it
    with a lottery-ticket stake.
    """
    low_price = float(settings.low_prob_entry_price) - 0.01
    r = compute_ev(
        net_edge_pct=0.7,
        abs_z=2.5,
        context_score=1.0,
        entry_price=low_price,
    )
    assert r.is_exploratory is True
    assert r.ev > float(settings.ev_opp_min)
    assert r.tier == "mid"


def test_exploratory_with_strong_ev_becomes_core() -> None:
    """Low-prob entry + strong EV → is_exploratory flag set but tier is core.

    Asymmetric setups with genuine edge deserve full core sizing, not the
    exploratory 'tiny-size' treatment.
    """
    low_price = float(settings.low_prob_entry_price) - 0.01
    r = compute_ev(
        net_edge_pct=5.0,
        abs_z=2.5,
        context_score=0.7,
        entry_price=low_price,
    )
    assert r.is_exploratory is True
    assert r.ev >= float(settings.ev_core_min)
    assert r.tier == "core"


def test_high_price_never_exploratory() -> None:
    """Prices above LOW_PROB_ENTRY_PRICE are never exploratory."""
    r = compute_ev(
        net_edge_pct=5.0,
        abs_z=2.5,
        context_score=0.7,
        entry_price=0.40,
    )
    assert r.is_exploratory is False


def test_p_edge_real_bounded() -> None:
    """P_edge_real must never exceed 0.95 even for absurd z+context."""
    r = compute_ev(
        net_edge_pct=20.0,
        abs_z=10.0,
        context_score=1.0,
        entry_price=0.25,
    )
    assert r.p_edge_real <= 0.95


def test_ev_increases_with_z() -> None:
    """Higher z → higher p_edge_real → higher EV, all else equal."""
    low_z = compute_ev(net_edge_pct=4.0, abs_z=1.0, context_score=0.5, entry_price=0.3)
    high_z = compute_ev(net_edge_pct=4.0, abs_z=3.0, context_score=0.5, entry_price=0.3)
    assert high_z.ev > low_z.ev
    assert high_z.p_edge_real > low_z.p_edge_real


def test_ev_increases_with_context() -> None:
    """Better context → higher EV, all else equal."""
    low_ctx = compute_ev(net_edge_pct=4.0, abs_z=2.0, context_score=0.1, entry_price=0.3)
    high_ctx = compute_ev(net_edge_pct=4.0, abs_z=2.0, context_score=0.9, entry_price=0.3)
    assert high_ctx.ev > low_ctx.ev


def test_none_entry_price_treated_as_mid_price() -> None:
    """When entry_price is None, no crash and payout_ratio defaults to mid-price assumption."""
    r = compute_ev(net_edge_pct=5.0, abs_z=2.0, context_score=0.7, entry_price=None)
    assert r.is_exploratory is False
    assert r.payout_ratio == pytest.approx(1.0, abs=0.1)
