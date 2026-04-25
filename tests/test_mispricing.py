"""Mispricing result helpers & adj-vol scoring."""
from __future__ import annotations

from app.services.mispricing import MispricingResult


def test_abs_z_handles_none() -> None:
    mp = MispricingResult(
        market_id="m", z=None, mean=None, stddev=None, samples=0, adj_vol_score=0.0
    )
    assert mp.abs_z == 0.0


def test_abs_z_negative() -> None:
    mp = MispricingResult(
        market_id="m", z=-2.5, mean=0.3, stddev=0.04, samples=30, adj_vol_score=0.9
    )
    assert mp.abs_z == 2.5
