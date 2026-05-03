"""Unit tests for :mod:`app.services.lag_arb_pricer`."""
from __future__ import annotations

from math import isclose

import pytest

from app.services.lag_arb_pricer import (
    EwmaSigma,
    annualised_from_per_sec,
    choose_side,
    edge_diagnostic,
    fair_prob_above,
    norm_cdf,
    per_sec_from_annualised,
)


# ---- norm_cdf golden values ----------------------------------------------

def test_norm_cdf_at_zero_is_half() -> None:
    assert isclose(norm_cdf(0.0), 0.5, abs_tol=1e-12)


def test_norm_cdf_one_sigma_band() -> None:
    # P(-1 <= Z <= 1) ~ 0.6826
    p = norm_cdf(1.0) - norm_cdf(-1.0)
    assert isclose(p, 0.6826894921, abs_tol=1e-6)


def test_norm_cdf_two_sigma_band() -> None:
    p = norm_cdf(2.0) - norm_cdf(-2.0)
    assert isclose(p, 0.9544997361, abs_tol=1e-6)


# ---- fair_prob_above edge cases -----------------------------------------

def test_fair_prob_at_strike_is_half() -> None:
    # At spot == strike the BS digital is *almost* 0.5 — the drift term
    # ``-0.5 sigma^2 T`` biases it a hair below.  We allow that.
    p = fair_prob_above(spot=100.0, strike=100.0, sigma_per_sec=0.001, seconds_left=300)
    assert isclose(p, 0.5, abs_tol=5e-3)


def test_fair_prob_above_realised_yes() -> None:
    p = fair_prob_above(spot=110.0, strike=100.0, sigma_per_sec=0.001, seconds_left=0)
    assert p == 1.0


def test_fair_prob_above_realised_no() -> None:
    p = fair_prob_above(spot=90.0, strike=100.0, sigma_per_sec=0.001, seconds_left=0)
    assert p == 0.0


def test_fair_prob_higher_when_in_the_money() -> None:
    deep_itm = fair_prob_above(spot=110.0, strike=100.0, sigma_per_sec=0.0001, seconds_left=300)
    just_itm = fair_prob_above(spot=101.0, strike=100.0, sigma_per_sec=0.0001, seconds_left=300)
    assert deep_itm > just_itm > 0.5


def test_fair_prob_zero_sigma_collapses() -> None:
    # No volatility -> deterministic outcome.
    assert fair_prob_above(spot=110, strike=100, sigma_per_sec=0.0, seconds_left=60) == 1.0
    assert fair_prob_above(spot=90, strike=100, sigma_per_sec=0.0, seconds_left=60) == 0.0


def test_fair_prob_invalid_inputs_return_half() -> None:
    assert fair_prob_above(spot=0, strike=100, sigma_per_sec=0.0001, seconds_left=60) == 0.5
    assert fair_prob_above(spot=100, strike=0, sigma_per_sec=0.0001, seconds_left=60) == 0.5


# ---- choose_side edge gate ----------------------------------------------

def test_choose_side_picks_yes_with_clear_edge() -> None:
    quote = choose_side(
        p_fair_yes=0.60,
        ask_yes=0.50,
        ask_no=0.50,
        fee_bps=180.0,
        slip_bps=60.0,
    )
    assert quote is not None
    assert quote.side == "yes"
    # 60 - 50 - 2.4 = 7.6
    assert quote.edge_pct == pytest.approx(7.6, abs=1e-3)


def test_choose_side_picks_no_when_inverted() -> None:
    quote = choose_side(
        p_fair_yes=0.30,
        ask_yes=0.50,
        ask_no=0.50,
        fee_bps=180.0,
        slip_bps=60.0,
    )
    assert quote is not None
    assert quote.side == "no"


def test_choose_side_returns_none_when_no_edge() -> None:
    quote = choose_side(
        p_fair_yes=0.50,
        ask_yes=0.50,
        ask_no=0.50,
        fee_bps=180.0,
        slip_bps=60.0,
    )
    assert quote is None  # exactly at fair value, fees eat any edge


def test_choose_side_skips_missing_book_side() -> None:
    quote = choose_side(
        p_fair_yes=0.30,
        ask_yes=0.50,
        ask_no=None,
        fee_bps=10.0,
        slip_bps=10.0,
    )
    assert quote is None  # YES has negative edge, NO not quoted


# ---- edge_diagnostic ------------------------------------------------------


def test_edge_diagnostic_both_sides_negative() -> None:
    d = edge_diagnostic(
        p_fair_yes=0.50,
        ask_yes=0.52,
        ask_no=0.52,
        fee_bps=180.0,
        slip_bps=60.0,
    )
    assert d.edge_yes_pct is not None and d.edge_yes_pct < 0
    assert d.edge_no_pct is not None and d.edge_no_pct < 0
    assert d.best_side in ("yes", "no")
    assert d.best_edge_pct is not None and d.best_edge_pct < 0


def test_edge_diagnostic_missing_ask_returns_partial() -> None:
    d = edge_diagnostic(
        p_fair_yes=0.30,
        ask_yes=0.50,
        ask_no=None,
        fee_bps=10.0,
        slip_bps=10.0,
    )
    assert d.edge_yes_pct is not None
    assert d.edge_no_pct is None
    assert d.best_side == "yes"


def test_edge_diagnostic_agrees_with_choose_side_when_positive() -> None:
    pf, ay, ano = 0.60, 0.50, 0.50
    fb, sb = 180.0, 60.0
    d = edge_diagnostic(pf, ask_yes=ay, ask_no=ano, fee_bps=fb, slip_bps=sb)
    q = choose_side(pf, ask_yes=ay, ask_no=ano, fee_bps=fb, slip_bps=sb)
    assert q is not None
    assert d.best_side == q.side
    assert pytest.approx(d.best_edge_pct or 0.0, abs=1e-9) == q.edge_pct


# ---- EwmaSigma -----------------------------------------------------------

def test_ewma_sigma_starts_zero_and_warms_up() -> None:
    e = EwmaSigma()
    assert e.value == 0.0
    assert not e.is_warm
    e.update(100.0)
    assert e.value == 0.0  # one sample = no return
    for _ in range(70):
        e.update(100.5)
    assert e.is_warm


def test_ewma_sigma_reacts_to_volatility_burst() -> None:
    e = EwmaSigma()
    e.update(100.0)
    for _ in range(60):
        e.update(100.0)
    quiet = e.value
    for px in (110.0, 95.0, 105.0, 90.0, 100.0):
        e.update(px)
    noisy = e.value
    assert noisy > quiet


def test_per_sec_round_trip() -> None:
    annual = 0.6
    per_s = per_sec_from_annualised(annual)
    assert isclose(annualised_from_per_sec(per_s), annual, abs_tol=1e-12)
