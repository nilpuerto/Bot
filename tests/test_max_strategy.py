"""Unit tests for :mod:`app.services.max_strategy`."""
from __future__ import annotations

from app.services.max_strategy import evaluate
from app.services.ta_confluence import Candle


def _make_candles(closes: list[float], volumes: list[float] | None = None) -> list[Candle]:
    """Build a simple sequence of synthetic candles with monotonic open_time."""
    if volumes is None:
        volumes = [1.0] * len(closes)
    out: list[Candle] = []
    for i, (c, v) in enumerate(zip(closes, volumes)):
        prev = closes[i - 1] if i > 0 else c
        out.append(
            Candle(
                open_time_ms=i * 60_000,
                open=prev,
                high=max(prev, c) * 1.0001,
                low=min(prev, c) * 0.9999,
                close=c,
                volume=v,
            )
        )
    return out


def test_evaluate_returns_yes_on_strong_up_window_delta() -> None:
    """A 0.12% rise from open should dominate: weight 7 + side='yes'."""
    closes = [100.0] * 30
    sig = evaluate(
        window_open=100_000.0,
        spot=100_120.0,  # +0.12 %
        candles=_make_candles(closes),
        tick_prices=[100_100.0, 100_110.0, 100_120.0],
    )
    assert sig.side == "yes"
    assert sig.window_delta_pct > 0.10
    # Window-delta decisive (7) overwhelms; total score must be ≥ 7.
    assert sig.score >= 7.0
    assert sig.confidence >= 0.99


def test_evaluate_returns_no_on_strong_down_window_delta() -> None:
    sig = evaluate(
        window_open=100_000.0,
        spot=99_870.0,  # -0.13 %
        candles=_make_candles([100.0] * 30),
        tick_prices=[100.0, 99.95, 99.90],
    )
    assert sig.side == "no"
    assert sig.score <= -7.0
    assert sig.confidence >= 0.99


def test_evaluate_flat_window_returns_neutral() -> None:
    """Within 0.001 % no window-delta contribution."""
    sig = evaluate(
        window_open=100_000.0,
        spot=100_000.5,
        candles=_make_candles([100.0] * 30),
        tick_prices=[100.0] * 5,
    )
    assert abs(sig.score) < 5.0
    assert "window_delta_flat" in " ".join(sig.reasons) or sig.score == 0.0


def test_evaluate_handles_empty_inputs() -> None:
    sig = evaluate(
        window_open=0.0, spot=0.0, candles=[], tick_prices=[]
    )
    assert sig.score == 0.0
    assert sig.confidence == 0.0


def test_evaluate_window_delta_dominates_contrary_ema() -> None:
    """Even with EMA9 < EMA21, a strong +0.10% window delta should still
    pick YES.  The window-delta weight (≥5) overwhelms the EMA cross (1)."""
    closes = [100.0 + (29 - i) * 0.5 for i in range(30)]  # falling closes
    sig = evaluate(
        window_open=100_000.0,
        spot=100_100.0,  # +0.10 %  (strong)
        candles=_make_candles(closes),
        tick_prices=[100_080.0, 100_090.0, 100_100.0],
    )
    assert sig.side == "yes"
    assert sig.score > 0
