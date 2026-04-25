"""Feedback loop — pure math helpers (edge-first constrained variant)."""
from __future__ import annotations

from app.services.feedback_loop import (
    LEARNABLE_COMPONENTS,
    _clip_unit,
    _extract_components,
    _pnl_sign,
)


def test_only_mispricing_and_liquidity_are_learnable() -> None:
    # News + timing are hard gates — never moved by the feedback loop.
    assert set(LEARNABLE_COMPONENTS) == {"mispricing", "liquidity"}


def test_pnl_sign_buckets() -> None:
    assert _pnl_sign(10.0) == 1
    assert _pnl_sign(-10.0) == -1
    assert _pnl_sign(0.5) == 0  # noise band


def test_clip_unit_bounds() -> None:
    assert _clip_unit(-1.0) == 0.0
    assert _clip_unit(2.0) == 1.0
    assert _clip_unit(0.3) == 0.3


def test_extract_components_picks_only_learnable_raw_keys() -> None:
    fv = {
        "news_raw": 0.6,
        "liquidity_raw": 0.4,
        "mispricing_raw": 0.8,
        "timing_raw": 1.0,
        "something_else": 99,
    }
    out = _extract_components(fv)
    # Only mispricing + liquidity survive — news/timing are ignored.
    assert out == {"liquidity": 0.4, "mispricing": 0.8}


def test_extract_components_skips_missing_or_malformed() -> None:
    out = _extract_components({"liquidity_raw": "bad", "mispricing_raw": 0.5})
    assert out == {"mispricing": 0.5}
