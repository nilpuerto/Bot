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
    """Decent setup but edge is thin → EV above opp_min, below core → mid."""
    r = compute_ev(
        net_edge_pct=3.0,
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


def test_exploratory_low_price_high_payout() -> None:
    """Entry at a low-prob price with thin but positive EV → exploratory (low tier).

    To be EV-positive with a thin edge, p_edge_real must be high enough to
    overcome the loss estimate.  We use a strong z+context to achieve that
    while keeping net_edge_pct low so EV stays below EV_OPP_MIN.

    EV = 0.80 × 0.7 - 0.20 × 2.0 = 0.56 - 0.40 = 0.16 → between 0 and EV_OPP_MIN.
    """
    low_price = float(settings.low_prob_entry_price) - 0.01
    r = compute_ev(
        net_edge_pct=0.7,       # thin edge — just enough for EV > 0 at high p
        abs_z=2.5,              # z_boost hits cap → 0.20
        context_score=1.0,      # ctx_boost = 0.10 → p_edge_real = 0.80
        entry_price=low_price,
    )
    assert r.payout_ratio >= float(settings.ev_exploratory_payout_min)
    assert r.ev > 0.0
    assert r.ev < float(settings.ev_opp_min)
    assert r.is_exploratory is True
    assert r.tier == "low"


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
