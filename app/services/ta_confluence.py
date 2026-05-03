"""Technical-analysis confluence layer for Crypto Mode.

This module exists for one purpose: turn ~200 1-minute BTC candles into
a small "how many indicators agree on the requested side?" score that
the lag-arb orchestrator can use as a *filter*.  It is deliberately
not the entry decision — that lives in the lag-arb pricer.  TA here is
a confluence veto so a setup with positive math but a horrible chart
context is skipped instead of fired.

We avoid heavy deps (no ``pandas``, no ``ta-lib``, no ``pandas-ta``).
Every indicator is a tiny pure function over Python ``Candle``
sequences, easy to unit-test against synthetic fixtures.

Indicators returned in the score (max 4 points):

1. **Fib 0.5/0.618 zone**   — last impulse swing-high/low identified
   via a simple 5-candle pivot; "in zone" if current price sits in
   the 0.45 - 0.65 retracement band of that move and the proposed side
   is consistent with the impulse direction.
2. **RSI(14) + divergence** — bullish entry needs RSI < 35 *or*
   regular bullish divergence vs the last swing-low; symmetric for
   short.  RSI rising from oversold beats just being oversold.
3. **Break & Retest**       — price broke last 20-bar S/R and is now
   retesting it within ~0.1 % from the correct side.
4. **Liquidity grab / wick**— last candle has a wick > 1.5x its body
   and pierced (and rejected) a recent S/R level on the wick side.

A 15-minute EMA20 vs EMA50 trend filter is computed from the same 1m
candles (resampled internally) and used as a tie-breaker / amplifier:
when the 15m trend agrees with the requested side, it adds the 4th
"reason" but never on its own — at least one of indicators 1-3 must
hit.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

import httpx

try:  # orjson is optional — fall back to stdlib if missing
    import orjson as _json  # type: ignore[import-not-found]

    def _loads(raw: object) -> object:
        return _json.loads(raw if isinstance(raw, (bytes, bytearray, str)) else str(raw))
except ImportError:  # pragma: no cover - exercised only on slim envs
    import json as _stdlib_json

    def _loads(raw: object) -> object:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        return _stdlib_json.loads(raw)

from app.config.settings import settings
from app.utils.logger import get_logger


logger = get_logger(__name__)


Direction = Literal["long", "short", "flat"]


# ---- candle model --------------------------------------------------------


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


# ---- TA result -----------------------------------------------------------


@dataclass(frozen=True)
class TAScore:
    direction: Direction
    confluence: int
    reasons: list[str] = field(default_factory=list)
    rsi: Optional[float] = None
    trend_15m: Direction = "flat"


_FLAT = TAScore(direction="flat", confluence=0, reasons=[])


# ---- numeric helpers -----------------------------------------------------


def _ema(values: Iterable[float], period: int) -> list[float]:
    """Standard EMA — initialised with the SMA of the first ``period``."""
    values = list(values)
    if len(values) < period or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    sma = sum(values[:period]) / period
    out: list[float] = [float("nan")] * (period - 1) + [sma]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if gains + losses == 0:
        return 50.0
    rs = (gains / period) / (losses / period if losses > 0 else 1e-9)
    return 100.0 - (100.0 / (1.0 + rs))


def _swing_high_low(candles: list[Candle], lookback: int = 30) -> tuple[Optional[Candle], Optional[Candle]]:
    """Return the swing high and swing low of the last ``lookback`` candles."""
    if len(candles) < lookback:
        return None, None
    window = candles[-lookback:]
    high = max(window, key=lambda c: c.high)
    low = min(window, key=lambda c: c.low)
    return high, low


def _resample_15m(candles: list[Candle]) -> list[Candle]:
    """Cheap 1m -> 15m resampler (assumes input is sorted ascending)."""
    if not candles:
        return []
    out: list[Candle] = []
    bucket: list[Candle] = []
    bucket_start: Optional[int] = None
    bucket_size_ms = 15 * 60 * 1_000
    for c in candles:
        slot = c.open_time_ms - (c.open_time_ms % bucket_size_ms)
        if bucket_start is None:
            bucket_start = slot
        if slot != bucket_start and bucket:
            out.append(_aggregate(bucket, bucket_start))
            bucket = []
            bucket_start = slot
        bucket.append(c)
    if bucket and bucket_start is not None:
        out.append(_aggregate(bucket, bucket_start))
    return out


def _aggregate(bucket: list[Candle], open_time_ms: int) -> Candle:
    return Candle(
        open_time_ms=open_time_ms,
        open=bucket[0].open,
        high=max(c.high for c in bucket),
        low=min(c.low for c in bucket),
        close=bucket[-1].close,
        volume=sum(c.volume for c in bucket),
    )


# ---- individual indicators ----------------------------------------------


def _fib_zone(candles: list[Candle], side: Direction) -> Optional[str]:
    """Return a reason string if the current price is inside the 0.5-0.618
    retracement of the last clear impulse, on the side consistent with the
    requested ``side``.  ``None`` means the indicator does not vote.
    """
    if side == "flat" or len(candles) < 30:
        return None
    high, low = _swing_high_low(candles, lookback=30)
    if high is None or low is None:
        return None
    span = high.high - low.low
    if span <= 0:
        return None

    last = candles[-1].close
    if low.open_time_ms < high.open_time_ms:
        # impulse went UP -> retracement is from high back toward low
        retr_618 = high.high - 0.618 * span
        retr_500 = high.high - 0.500 * span
        in_zone = retr_618 <= last <= retr_500 + 0.05 * span
        if side == "long" and in_zone:
            return "fib_0.618_long"
    else:
        # impulse went DOWN -> retracement bounces upward
        retr_618 = low.low + 0.618 * span
        retr_500 = low.low + 0.500 * span
        in_zone = retr_500 - 0.05 * span <= last <= retr_618
        if side == "short" and in_zone:
            return "fib_0.618_short"
    return None


def _rsi_indicator(candles: list[Candle], side: Direction) -> tuple[Optional[float], Optional[str]]:
    """Return (rsi, reason).  Reason is None when RSI does not vote."""
    closes = [c.close for c in candles]
    rsi = _rsi(closes, period=14)
    if rsi is None or side == "flat":
        return rsi, None
    if side == "long" and rsi <= 35.0:
        return rsi, "rsi_oversold"
    if side == "short" and rsi >= 65.0:
        return rsi, "rsi_overbought"
    # Crude divergence: lower price low + higher RSI low (bullish), or
    # higher price high + lower RSI high (bearish).  Uses the last two
    # 5-bar swings.
    if len(closes) >= 30:
        # Build RSI series for the last 25 bars.
        rsi_series = [_rsi(closes[: i + 1]) or 50.0 for i in range(len(closes) - 25, len(closes))]
        recent = closes[-25:]
        if side == "long":
            i_low1 = recent[:12].index(min(recent[:12]))
            i_low2 = 12 + recent[12:].index(min(recent[12:]))
            if recent[i_low2] < recent[i_low1] and rsi_series[i_low2] > rsi_series[i_low1]:
                return rsi, "rsi_bull_div"
        else:
            i_high1 = recent[:12].index(max(recent[:12]))
            i_high2 = 12 + recent[12:].index(max(recent[12:]))
            if recent[i_high2] > recent[i_high1] and rsi_series[i_high2] < rsi_series[i_high1]:
                return rsi, "rsi_bear_div"
    return rsi, None


def _break_and_retest(candles: list[Candle], side: Direction) -> Optional[str]:
    if side == "flat" or len(candles) < 25:
        return None
    # Resistance / support must be measured BEFORE the breakout, so we
    # split the lookback into an "older" half (forms the level) and a
    # "recent" half (where the break must occur).  Without this split
    # the breakout candle was always in its own SR set, making the
    # indicator unable to fire.
    older = candles[-25:-12]   # 13 candles forming the level
    recent = candles[-12:-1]   # window where the break may have happened
    last = candles[-1]
    sr_high = max(c.high for c in older)
    sr_low = min(c.low for c in older)
    tol = 0.002  # 0.2 % retest band
    if side == "long":
        broke = any(c.close > sr_high for c in recent)
        retesting = (
            sr_high * (1 - tol) <= last.low <= sr_high * (1 + tol)
            and last.close >= sr_high * (1 - tol)
        )
        if broke and retesting:
            return "break_retest_long"
    else:
        broke = any(c.close < sr_low for c in recent)
        retesting = (
            sr_low * (1 - tol) <= last.high <= sr_low * (1 + tol)
            and last.close <= sr_low * (1 + tol)
        )
        if broke and retesting:
            return "break_retest_short"
    return None


def _liquidity_grab(candles: list[Candle], side: Direction) -> Optional[str]:
    if side == "flat" or len(candles) < 25:
        return None
    sr_high = max(c.high for c in candles[-25:-1])
    sr_low = min(c.low for c in candles[-25:-1])
    last = candles[-1]
    if last.body <= 0:
        return None
    # Bullish liquidity grab: long lower wick that swept ``sr_low`` and
    # closed above it.
    if side == "long":
        if (
            last.lower_wick > 1.5 * last.body
            and last.low < sr_low
            and last.close > sr_low
        ):
            return "liquidity_grab_long"
    else:
        if (
            last.upper_wick > 1.5 * last.body
            and last.high > sr_high
            and last.close < sr_high
        ):
            return "liquidity_grab_short"
    return None


def _trend_15m(candles: list[Candle]) -> Direction:
    fifteen = _resample_15m(candles)
    if len(fifteen) < 50:
        return "flat"
    closes = [c.close for c in fifteen]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    if not ema20 or not ema50:
        return "flat"
    if ema20[-1] > ema50[-1] * 1.0005:
        return "long"
    if ema20[-1] < ema50[-1] * 0.9995:
        return "short"
    return "flat"


# ---- public scoring ------------------------------------------------------


def score(candles: list[Candle], side: Direction) -> TAScore:
    """Compute a confluence score for the requested ``side``.

    ``confluence`` is the count of independent indicators that fire
    *for that side*.  The 15m EMA trend is a 4th vote that only counts
    when at least one of indicators 1-3 already agrees.
    """
    if not candles or side == "flat":
        return _FLAT

    reasons: list[str] = []

    fib_reason = _fib_zone(candles, side)
    if fib_reason:
        reasons.append(fib_reason)

    rsi_value, rsi_reason = _rsi_indicator(candles, side)
    if rsi_reason:
        reasons.append(rsi_reason)

    br_reason = _break_and_retest(candles, side)
    if br_reason:
        reasons.append(br_reason)

    lg_reason = _liquidity_grab(candles, side)
    if lg_reason:
        reasons.append(lg_reason)

    trend = _trend_15m(candles)
    if reasons and trend == side:
        reasons.append(f"trend_15m_{side}")

    return TAScore(
        direction=side,
        confluence=len(reasons),
        reasons=reasons,
        rsi=rsi_value,
        trend_15m=trend,
    )


# ---- candle source (Binance REST, cached) -------------------------------


_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


class CandleCache:
    """Tiny TTL cache that fetches BTCUSDT 1m candles from Binance REST."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cached: list[Candle] = []
        self._cached_at = 0.0
        self._ttl = max(5, int(settings.crypto_ta_refresh_seconds))
        self._lookback = max(60, int(settings.crypto_ta_lookback_bars))

    async def get(self) -> list[Candle]:
        now = time.monotonic()
        if self._cached and now - self._cached_at < self._ttl:
            return self._cached
        async with self._lock:
            # Re-check after acquiring the lock.
            now = time.monotonic()
            if self._cached and now - self._cached_at < self._ttl:
                return self._cached
            fresh = await self._fetch()
            if fresh:
                self._cached = fresh
                self._cached_at = time.monotonic()
            return self._cached

    async def _fetch(self) -> list[Candle]:
        try:
            async with httpx.AsyncClient(timeout=8.0) as http:
                resp = await http.get(
                    _BINANCE_KLINES_URL,
                    params={
                        "symbol": "BTCUSDT",
                        "interval": "1m",
                        "limit": self._lookback,
                    },
                )
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("binance_klines_error", error=str(exc))
            return []
        try:
            raw = _loads(resp.content)
        except (ValueError, TypeError):
            return []
        out: list[Candle] = []
        if not isinstance(raw, list):
            return out
        for row in raw:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                out.append(
                    Candle(
                        open_time_ms=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                )
            except (TypeError, ValueError):
                continue
        return out


__all__ = [
    "Candle",
    "CandleCache",
    "Direction",
    "TAScore",
    "score",
]
