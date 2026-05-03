"""Unit tests for :mod:`app.services.ta_confluence`."""
from __future__ import annotations

from app.services.ta_confluence import Candle, score


def _build_candles(closes: list[float], *, base_open: float = 100.0) -> list[Candle]:
    """Build a synthetic 1m candle series given a list of closes.

    Wicks are minimal so single-indicator tests don't accidentally trip
    the liquidity-grab detector unless we craft them explicitly.
    """
    candles: list[Candle] = []
    open_time = 0
    last_close = base_open
    for c in closes:
        op = last_close
        high = max(op, c) + 0.01
        low = min(op, c) - 0.01
        candles.append(
            Candle(open_time_ms=open_time, open=op, high=high, low=low, close=c, volume=10.0)
        )
        last_close = c
        open_time += 60_000
    return candles


def test_empty_input_returns_flat() -> None:
    s = score([], "long")
    assert s.confluence == 0
    assert s.reasons == []


def test_flat_side_returns_flat() -> None:
    candles = _build_candles([100.0] * 100)
    s = score(candles, "flat")
    assert s.confluence == 0


def test_rsi_oversold_long() -> None:
    # 19 declining ticks make RSI very low.
    closes = [100.0 - i * 0.5 for i in range(40)]
    candles = _build_candles(closes)
    s = score(candles, "long")
    assert s.rsi is not None and s.rsi < 35.0
    assert any("rsi_oversold" in r for r in s.reasons)


def test_rsi_overbought_short() -> None:
    closes = [100.0 + i * 0.5 for i in range(40)]
    candles = _build_candles(closes)
    s = score(candles, "short")
    assert s.rsi is not None and s.rsi > 65.0
    assert any("rsi_overbought" in r for r in s.reasons)


def test_liquidity_grab_long() -> None:
    closes = [100.0] * 40
    candles = _build_candles(closes)
    # Replace the last candle with a long lower wick that pierced and rejected.
    last = candles[-1]
    sweep = Candle(
        open_time_ms=last.open_time_ms,
        open=100.0,
        high=100.5,
        low=98.0,          # pierces the rolling low (~99.99) hard
        close=100.4,       # closes back up
        volume=20.0,
    )
    candles[-1] = sweep
    s = score(candles, "long")
    assert any("liquidity_grab_long" in r for r in s.reasons)


def test_break_and_retest_long() -> None:
    # 13 sideways candles at 100 form the resistance, then a breakout at
    # 102, then a retest candle whose low touches ~100 and closes above.
    closes = [100.0] * 13 + [102.0] * 11 + [100.05]
    candles = _build_candles(closes)
    # Hand-craft the retest wick to graze the resistance from above.
    retest = candles[-1]
    candles[-1] = Candle(
        open_time_ms=retest.open_time_ms,
        open=102.0,
        high=102.05,
        low=100.0,
        close=100.05,
        volume=10.0,
    )
    s = score(candles, "long")
    assert any("break_retest_long" in r for r in s.reasons)


def test_score_includes_15m_trend_bonus() -> None:
    # Strong uptrend over ~800 1m candles -> 15m EMA20 > EMA50 after
    # resampling (need >= 50 15m bars for the EMA50).
    closes = [100.0 + i * 0.02 for i in range(800)]
    candles = _build_candles(closes)
    s_long = score(candles, "long")
    assert s_long.trend_15m == "long"
    s_short = score(candles, "short")
    # Even with a clear uptrend the trend filter never votes for SHORT.
    assert s_short.trend_15m != "short"
