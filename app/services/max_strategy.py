"""MAX Mode strategy — 7-indicator composite for BTC 5-minute Up/Down.

This module is the *signal* layer of MAX Mode.  It takes a window
(open price + 1-minute candles + accumulated tick prints) and returns a
single signed score.  Positive = Up, negative = Down.

The composite is heavily weighted toward the **window delta** — the
percentage move from the window's opening price to current spot — which
is literally the question Polymarket is asking.  Practitioners (notably
Archetapp's public BTC 5-min guide) found that giving short-horizon
indicators (EMA, RSI) equal weight at this scale produces noisy, often
wrong signals; weighting the window delta 5–7× dominantly resolves it.

We never fail open: missing candles, NaNs, empty tick buffers all
collapse to a 0 score so the sniper either skips or fires a coin-flip
trade depending on mode policy.

The seven indicators
--------------------

1. **Window delta (weight 5–7)**  — % move since window-open price.
   Dominates everything else; near the close, BTC rarely reverses a
   clear 0.10 % move in seconds.
2. **Micro momentum (weight 2)** — direction of the last 2 candles.
3. **Acceleration (weight 1.5)** — is momentum building or fading?
4. **EMA 9 / 21 cross (weight 1)** — short-term trend agreement.
5. **RSI(14) extremes (weight 1–2)** — overbought / oversold tilts.
6. **Volume surge (weight 1)** — recent 3-bar avg vs prior 3-bar avg.
7. **Tick trend (weight 2)** — directional consistency of accumulated
   sub-candle tick prints during the snipe loop.

Confidence is ``min(|score| / 7, 1.0)``: a clear single-indicator
window-delta read can hit ~71 % confidence on its own, and full
agreement of all seven approaches 100 %.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

from app.services.ta_confluence import Candle


Side = Literal["yes", "no"]


@dataclass
class MaxSignal:
    score: float
    confidence: float
    side: Side
    reasons: list[str] = field(default_factory=list)
    window_delta_pct: float = 0.0


def _ema(values: Sequence[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def _rsi(values: Sequence[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, curr in zip(values[:-1], values[1:]):
        diff = curr - prev
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _window_delta_score(window_pct: float) -> tuple[float, str]:
    """Map window % delta → (signed weighted score, reason).

    Positive % means BTC moved up vs window open; sign of returned
    score follows.  Magnitude follows the conviction tiers from the
    Archetapp guide: 0.001 / 0.005 / 0.02 / 0.10 → weights 1 / 3 / 5 / 7.
    """
    abs_pct = abs(window_pct)
    sign = 1.0 if window_pct >= 0 else -1.0
    if abs_pct >= 0.10:
        return sign * 7.0, f"window_delta_decisive_{window_pct:+.3f}%"
    if abs_pct >= 0.02:
        return sign * 5.0, f"window_delta_strong_{window_pct:+.3f}%"
    if abs_pct >= 0.005:
        return sign * 3.0, f"window_delta_moderate_{window_pct:+.3f}%"
    if abs_pct >= 0.001:
        return sign * 1.0, f"window_delta_slight_{window_pct:+.3f}%"
    return 0.0, "window_delta_flat"


def _micro_momentum(closes: Sequence[float]) -> tuple[float, str]:
    if len(closes) < 3:
        return 0.0, "momentum_insufficient_candles"
    last_two = closes[-2] - closes[-3], closes[-1] - closes[-2]
    if last_two[0] > 0 and last_two[1] > 0:
        return 2.0, "momentum_up"
    if last_two[0] < 0 and last_two[1] < 0:
        return -2.0, "momentum_down"
    return 0.0, "momentum_mixed"


def _acceleration(closes: Sequence[float]) -> tuple[float, str]:
    if len(closes) < 4:
        return 0.0, "accel_insufficient_candles"
    move_recent = closes[-1] - closes[-2]
    move_prior = closes[-3] - closes[-4]
    if move_recent > 0 and move_recent > move_prior:
        return 1.5, "accel_up"
    if move_recent < 0 and move_recent < move_prior:
        return -1.5, "accel_down"
    if move_recent > 0 and move_recent < move_prior * 0.5:
        return -0.75, "accel_fade_up"
    if move_recent < 0 and move_recent > move_prior * 0.5:
        return 0.75, "accel_fade_down"
    return 0.0, "accel_flat"


def _ema_cross(closes: Sequence[float]) -> tuple[float, str]:
    e9 = _ema(closes, 9)
    e21 = _ema(closes, 21)
    if e9 is None or e21 is None:
        return 0.0, "ema_insufficient"
    if e9 > e21:
        return 1.0, "ema9>ema21"
    if e9 < e21:
        return -1.0, "ema9<ema21"
    return 0.0, "ema_equal"


def _rsi_score(closes: Sequence[float]) -> tuple[float, str]:
    rsi = _rsi(closes, 14)
    if rsi is None:
        return 0.0, "rsi_insufficient"
    if rsi >= 75:
        return -2.0, f"rsi_overbought_{rsi:.0f}"
    if rsi <= 25:
        return 2.0, f"rsi_oversold_{rsi:.0f}"
    if rsi >= 60:
        return -1.0, f"rsi_high_{rsi:.0f}"
    if rsi <= 40:
        return 1.0, f"rsi_low_{rsi:.0f}"
    return 0.0, f"rsi_neutral_{rsi:.0f}"


def _volume_surge(candles: Sequence[Candle]) -> tuple[float, str]:
    if len(candles) < 6:
        return 0.0, "volume_insufficient"
    recent = sum(c.volume for c in candles[-3:]) / 3
    prior = sum(c.volume for c in candles[-6:-3]) / 3
    if prior <= 0:
        return 0.0, "volume_zero_prior"
    ratio = recent / prior
    last = candles[-1]
    direction = 1.0 if last.close >= last.open else -1.0
    if ratio >= 1.5:
        return direction * 1.0, f"volume_surge_{ratio:.2f}x"
    return 0.0, f"volume_normal_{ratio:.2f}x"


def _tick_trend(ticks: Sequence[float]) -> tuple[float, str]:
    """Score the directional consistency of recent tick prints.

    Returns ±2 only when ≥ 60 % of the diffs share a sign and the
    cumulative move is ≥ 0.005 %.  This is what catches micro-trends
    between 1-minute candle updates during the T-10s polling loop.
    """
    if len(ticks) < 5:
        return 0.0, "ticks_insufficient"
    diffs = [b - a for a, b in zip(ticks[:-1], ticks[1:])]
    ups = sum(1 for d in diffs if d > 0)
    downs = sum(1 for d in diffs if d < 0)
    total = max(1, ups + downs)
    consistency = max(ups, downs) / total
    if consistency < 0.6:
        return 0.0, f"ticks_mixed_{consistency:.2f}"
    move_pct = (ticks[-1] - ticks[0]) / max(1e-9, ticks[0]) * 100.0
    if abs(move_pct) < 0.005:
        return 0.0, f"ticks_flat_{move_pct:+.4f}%"
    sign = 1.0 if ups > downs else -1.0
    return sign * 2.0, f"ticks_trend_{move_pct:+.4f}%"


def evaluate(
    *,
    window_open: float,
    spot: float,
    candles: Sequence[Candle],
    tick_prices: Iterable[float] = (),
) -> MaxSignal:
    """Run the seven-indicator composite and return a :class:`MaxSignal`.

    ``window_open`` is the BTC reference price at the start of the
    5-minute Polymarket window (Chainlink oracle when available, falling
    back to the bot's own median spot from
    :class:`~app.integrations.crypto_price_feed.CryptoPriceFeed`).
    """
    if window_open <= 0 or spot <= 0:
        return MaxSignal(
            score=0.0,
            confidence=0.0,
            side="yes",
            reasons=["invalid_prices"],
            window_delta_pct=0.0,
        )

    window_pct = (spot - window_open) / window_open * 100.0
    closes = [c.close for c in candles]
    ticks = list(tick_prices)

    parts: list[tuple[float, str]] = [
        _window_delta_score(window_pct),
        _micro_momentum(closes),
        _acceleration(closes),
        _ema_cross(closes),
        _rsi_score(closes),
        _volume_surge(candles),
        _tick_trend(ticks),
    ]
    score = sum(p[0] for p in parts)
    reasons = [p[1] for p in parts if p[0] != 0.0]
    confidence = min(abs(score) / 7.0, 1.0)
    side: Side = "yes" if score >= 0 else "no"
    return MaxSignal(
        score=score,
        confidence=confidence,
        side=side,
        reasons=reasons,
        window_delta_pct=window_pct,
    )


__all__ = ["MaxSignal", "Side", "evaluate"]
