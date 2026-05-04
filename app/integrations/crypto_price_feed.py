"""Real-time BTC spot feed for Crypto Mode.

Aggregates two raw WebSocket streams:

* **Binance**:  ``wss://stream.binance.com:9443/ws/btcusdt@aggTrade``
* **Coinbase**: ``wss://ws-feed.exchange.coinbase.com``  (channel
  ``ticker`` for ``BTC-USD``)

Both venues publish trade-by-trade prints; we keep a rolling buffer
of the last few seconds of ticks per venue and expose the *median*
across feeds as the canonical spot.  Using two venues protects against
a single feed gapping or freezing — practitioners' #1 way to lose
money on lag arb.

The feed also accumulates an EWMA estimator of per-second log-return
volatility (see :class:`~app.services.lag_arb_pricer.EwmaSigma`) so the
orchestrator never has to compute it itself.

Public surface::

    feed = CryptoPriceFeed()
    await feed.start()
    snap = feed.snapshot()           # PriceSnapshot
    if snap.is_fresh and snap.is_warm:
        ...
    await feed.stop()

The class is deliberately deps-light: only ``websockets`` (already in
``requirements.txt``) and ``orjson`` for fast parsing.  No ``ccxt.pro``
because it would drag in ~30 MB of unrelated exchange code.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

try:  # orjson is optional; the WS feed uses it for speed when present.
    import orjson as _json  # type: ignore[import-not-found]

    def _loads(raw: object) -> object:
        return _json.loads(raw if isinstance(raw, (bytes, bytearray, str)) else str(raw))

    def _dumps(obj: object) -> bytes:
        return _json.dumps(obj)
except ImportError:  # pragma: no cover - slim envs only
    import json as _stdlib_json

    def _loads(raw: object) -> object:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        return _stdlib_json.loads(raw)

    def _dumps(obj: object) -> bytes:
        return _stdlib_json.dumps(obj).encode("utf-8")

from app.config.settings import settings
from app.services.lag_arb_pricer import EwmaSigma
from app.utils.logger import get_logger


logger = get_logger(__name__)


BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_SUBSCRIBE = _dumps(
    {
        "type": "subscribe",
        "product_ids": ["BTC-USD"],
        "channels": ["ticker"],
    }
)


@dataclass
class _VenueTick:
    price: float
    received_at: float  # monotonic clock seconds


@dataclass
class PriceSnapshot:
    """Cheap immutable struct returned to consumers."""

    spot: Optional[float]
    age_ms: int            # max(age across venues) at snapshot time
    sigma_per_sec: float
    sources: list[str]
    sample_count: int

    @property
    def is_fresh(self) -> bool:
        return self.spot is not None and self.age_ms <= settings.crypto_feed_stale_ms

    @property
    def is_warm(self) -> bool:
        return self.sample_count >= max(1, settings.crypto_feed_warmup_samples)


@dataclass
class _VenueState:
    name: str
    last: Optional[_VenueTick] = None
    task: Optional[asyncio.Task] = None
    backoff_seconds: float = 1.0
    error_count: int = 0


class CryptoPriceFeed:
    """Asyncio service maintaining a fresh BTC spot estimate."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._venues: dict[str, _VenueState] = {}
        self._sigma = EwmaSigma()
        self._last_sigma_update = 0.0
        # We update the EWMA using the median spot, throttled to keep a
        # single venue's tick burst from spuriously inflating sigma.  The
        # default is short so warm-up takes only a few seconds after start.
        self._sigma_interval = max(0.05, float(settings.crypto_sigma_interval_s))

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        sources = [s for s in settings.crypto_price_sources if s in {"binance", "coinbase"}]
        if not sources:
            sources = ["binance", "coinbase"]
        for src in sources:
            self._venues[src] = _VenueState(name=src)
        for src, state in self._venues.items():
            if src == "binance":
                state.task = asyncio.create_task(
                    self._binance_loop(state), name="crypto_feed_binance"
                )
            elif src == "coinbase":
                state.task = asyncio.create_task(
                    self._coinbase_loop(state), name="crypto_feed_coinbase"
                )
        logger.info("crypto_price_feed_started", sources=sources)

    async def stop(self) -> None:
        self._stop.set()
        for state in self._venues.values():
            task = state.task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        logger.info("crypto_price_feed_stopped")

    # ---- public snapshot -----------------------------------------------

    def snapshot(self) -> PriceSnapshot:
        now = time.monotonic()
        prices: list[float] = []
        ages_ms: list[int] = []
        live_sources: list[str] = []
        for state in self._venues.values():
            if state.last is None:
                continue
            age_ms = int((now - state.last.received_at) * 1000)
            # Drop ticks that haven't been refreshed in 2 stale windows;
            # they belong to a frozen connection and should not vote.
            if age_ms > 2 * settings.crypto_feed_stale_ms:
                continue
            prices.append(state.last.price)
            ages_ms.append(age_ms)
            live_sources.append(state.name)
        if not prices:
            return PriceSnapshot(
                spot=None,
                age_ms=10**9,
                sigma_per_sec=self._sigma.value,
                sources=[],
                sample_count=self._sigma._samples,
            )
        return PriceSnapshot(
            spot=statistics.median(prices),
            age_ms=max(ages_ms),
            sigma_per_sec=self._sigma.value,
            sources=live_sources,
            sample_count=self._sigma._samples,
        )

    # ---- venue loops ---------------------------------------------------

    async def _binance_loop(self, state: _VenueState) -> None:
        # ``websockets`` is imported lazily so the module stays importable
        # in tests that don't need the network layer.
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            logger.error("crypto_feed_websockets_missing", error=str(exc))
            return

        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    BINANCE_WS_URL, ping_interval=20, close_timeout=5
                ) as ws:
                    state.backoff_seconds = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        self._handle_binance_msg(state, raw)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                state.error_count += 1
                logger.warning(
                    "crypto_feed_binance_error",
                    error=str(exc),
                    backoff_s=state.backoff_seconds,
                    errors=state.error_count,
                )
            await self._sleep_backoff(state)

    async def _coinbase_loop(self, state: _VenueState) -> None:
        try:
            import websockets
        except ImportError:  # pragma: no cover
            return

        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    COINBASE_WS_URL, ping_interval=20, close_timeout=5
                ) as ws:
                    state.backoff_seconds = 1.0
                    await ws.send(COINBASE_SUBSCRIBE)
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        self._handle_coinbase_msg(state, raw)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                state.error_count += 1
                logger.warning(
                    "crypto_feed_coinbase_error",
                    error=str(exc),
                    backoff_s=state.backoff_seconds,
                    errors=state.error_count,
                )
            await self._sleep_backoff(state)

    async def _sleep_backoff(self, state: _VenueState) -> None:
        if self._stop.is_set():
            return
        delay = state.backoff_seconds
        state.backoff_seconds = min(30.0, state.backoff_seconds * 2.0)
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    # ---- message handling ----------------------------------------------

    def _handle_binance_msg(self, state: _VenueState, raw: object) -> None:
        try:
            data = _loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        # aggTrade payload: ``{"p": "70123.45", ...}``
        try:
            price = float(data.get("p"))
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        state.last = _VenueTick(price=price, received_at=time.monotonic())
        self._maybe_update_sigma()

    def _handle_coinbase_msg(self, state: _VenueState, raw: object) -> None:
        try:
            data = _loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        if data.get("type") != "ticker":
            return
        try:
            price = float(data.get("price"))
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        state.last = _VenueTick(price=price, received_at=time.monotonic())
        self._maybe_update_sigma()

    def _maybe_update_sigma(self) -> None:
        now = time.monotonic()
        if now - self._last_sigma_update < self._sigma_interval:
            return
        snap = self.snapshot()
        if snap.spot is None:
            return
        self._sigma.update(snap.spot)
        self._last_sigma_update = now


__all__ = ["CryptoPriceFeed", "PriceSnapshot"]
