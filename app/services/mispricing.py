"""Mispricing detection via rolling z-score on historical prices.

Every `PRICE_SAMPLER_INTERVAL_SECONDS` the :class:`PriceSampler`
background task records the current price + 24h volume of every market
attached to a recent (<= 3h) signal into :class:`MarketPriceHistory`.
That table is the fuel for `compute_z`, which computes:

    μ, σ = mean / std over the last ``window_days`` samples
    z    = (current_price − μ) / σ

A z of +2 means "priced two standard deviations ABOVE the baseline";
a z of −2 is the symmetric long opportunity.  The Mispricing pillar of
the scoring engine converts |z| plus an adjacent volume signal into a
0..25 score: high |z| combined with LOW volume (adj_vol_score) = strong
mispricing signal.

All math runs in Python/float — no NumPy dependency.
"""
from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings
from app.database.repositories.market_history_repo import MarketHistoryRepository
from app.database.repositories.signals_repo import SignalsRepository
from app.database.session import session_scope
from app.integrations.polymarket_client import MarketSnapshot, PolymarketClient
from app.utils.logger import get_logger


logger = get_logger(__name__)


@dataclass
class MispricingResult:
    market_id: str
    z: Optional[float]
    mean: Optional[float]
    stddev: Optional[float]
    samples: int
    adj_vol_score: float  # 0..1 — HIGH means thin volume (= better edge)
    current_price: Optional[float] = None
    current_volume_24h: Optional[float] = None

    @property
    def abs_z(self) -> float:
        return abs(self.z) if self.z is not None else 0.0


class MispricingService:
    """Reads :class:`MarketPriceHistory` and computes a rolling z-score."""

    def __init__(
        self,
        *,
        window_days: int = 30,
        min_samples: int = 20,
    ) -> None:
        self._window_days = window_days
        self._min_samples = min_samples

    async def compute(
        self,
        market: MarketSnapshot,
        *,
        price: Optional[float] = None,
    ) -> MispricingResult:
        mid = price if price is not None else market.best_yes_price

        async with session_scope() as session:
            repo = MarketHistoryRepository(session)
            rows = await repo.prices_last_days(market.id, days=self._window_days)

        prices = [float(r.price) for r in rows if r.price is not None]
        volumes = [float(r.volume_24h) for r in rows if r.volume_24h is not None]

        if len(prices) < self._min_samples or mid is None:
            # Neutral result: not enough data to call anything mispriced.
            return MispricingResult(
                market_id=market.id,
                z=None,
                mean=(statistics.fmean(prices) if prices else None),
                stddev=None,
                samples=len(prices),
                adj_vol_score=0.0,
                current_price=mid,
                current_volume_24h=market.volume_24h,
            )

        mean = statistics.fmean(prices)
        try:
            stdev = statistics.stdev(prices)
        except statistics.StatisticsError:
            stdev = 0.0
        z: Optional[float]
        if stdev > 1e-6:
            z = (mid - mean) / stdev
        else:
            z = None

        # Adjacent volume score: 0 when current volume >> rolling mean
        # (crowded market → no edge), 1 when far below (thin + mispriced
        # = real opportunity).  Clamped and smooth.
        adj_vol = 0.0
        if volumes and market.volume_24h is not None:
            vol_mean = statistics.fmean(volumes) or 1.0
            ratio = market.volume_24h / vol_mean if vol_mean > 0 else 1.0
            # ratio < 0.5 → 1.0 (very thin), ratio > 2.0 → 0.0 (crowded)
            if ratio <= 0.5:
                adj_vol = 1.0
            elif ratio >= 2.0:
                adj_vol = 0.0
            else:
                adj_vol = max(0.0, min(1.0, (2.0 - ratio) / 1.5))

        return MispricingResult(
            market_id=market.id,
            z=z,
            mean=mean,
            stddev=stdev,
            samples=len(prices),
            adj_vol_score=adj_vol,
            current_price=mid,
            current_volume_24h=market.volume_24h,
        )


class PriceSampler:
    """Background loop — pushes a row into ``market_price_history`` per tick."""

    def __init__(
        self,
        polymarket: PolymarketClient,
        *,
        interval_seconds: Optional[int] = None,
        max_markets: Optional[int] = None,
    ) -> None:
        self._poly = polymarket
        self._interval = interval_seconds or settings.price_sampler_interval_seconds
        self._max_markets = max_markets or settings.price_sampler_max_markets
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # defensive
                logger.exception("price_sampler_tick_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        async with session_scope() as session:
            markets = list(
                await SignalsRepository(session).tracked_market_ids(limit=self._max_markets)
            )
        if not markets:
            return

        # Fetch prices serially (kept simple & cheap).  Samples are async
        # individually; we batch DB writes at the end of the tick.
        snapshots: list[tuple[str, Optional[float], Optional[float]]] = []
        for mid in markets:
            try:
                m = await self._poly.get_market(mid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("price_sampler_market_error", market_id=mid, error=str(exc))
                continue
            if m is None:
                continue
            snapshots.append((mid, m.best_yes_price, m.volume_24h))

        if not snapshots:
            return

        async with session_scope() as session:
            repo = MarketHistoryRepository(session)
            for mid, price, vol in snapshots:
                await repo.record(mid, price, vol)

        logger.debug("price_sampler_tick", records=len(snapshots))
