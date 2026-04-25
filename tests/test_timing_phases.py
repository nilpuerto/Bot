"""Timing phase detector — phase 1..5 classification."""
from __future__ import annotations

from app.services.timing import TimingFeatures, detect_phase, is_tradeable_phase


def test_phase1_leak_when_prevolume_and_price_move_without_headline() -> None:
    f = TimingFeatures(
        news_age_s=None, dvol_1m=100.0, avg_vol_1m=10.0, dprice_1m=0.02
    )
    d = detect_phase(f)
    assert d.phase == 1
    assert d.score == 20.0
    assert is_tradeable_phase(d.phase)


def test_phase2_breaking_reaction() -> None:
    d = detect_phase(TimingFeatures(news_age_s=30.0, avg_vol_1m=10.0))
    assert d.phase == 2
    assert d.score == 16.0
    assert is_tradeable_phase(d.phase)


def test_phase3_retail_influx() -> None:
    d = detect_phase(
        TimingFeatures(
            news_age_s=180.0, dvol_5m=30.0, avg_vol_1m=10.0, dprice_1m=0.01
        )
    )
    assert d.phase == 3


def test_phase4_overreaction_thin_volume_fast_price() -> None:
    d = detect_phase(
        TimingFeatures(
            news_age_s=600.0, dvol_5m=1.0, avg_vol_1m=10.0, dprice_1m=0.05
        )
    )
    assert d.phase == 4
    assert not is_tradeable_phase(d.phase)


def test_phase5_decay_default() -> None:
    d = detect_phase(TimingFeatures(news_age_s=3600.0, avg_vol_1m=10.0))
    assert d.phase == 5
    assert d.score == 0.0
