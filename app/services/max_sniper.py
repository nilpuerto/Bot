"""MAX Mode 5-minute BTC Up/Down sniper.

The sniper runs an independent loop that, on every fresh 5-minute clock
boundary, schedules a coroutine to:

1. Wait until ``T-10 s`` before the window closes.
2. Run an inner polling loop every 2 s, evaluating
   :func:`app.services.max_strategy.evaluate` against the latest spot,
   1-minute candles, and accumulated tick prices from the price feed.
3. Track the best signal seen across the loop (highest ``|score|``).
4. Fire the trade as soon as confidence ≥ ``MAX_MIN_CONFIDENCE`` *or*
   a weaker early path (``MAX_EARLY_DELTA_ABS_PCT``),
   detect a "spike" (score jump ≥ threshold with confidence / delta gates),
   or at the ``T-5 s`` hard deadline using the best signal seen (with
   flat-skip and micro-edge guards).
5. If the winning side has no asks, post a GTC limit at $0.95 as a
   fallback (becoming the liquidity).

Market discovery tries ``/events?slug=`` first, then search.  Optional
Polymarket RTDS Chainlink (``MAX_CHAINLINK_ORACLE_ENABLED``) aligns
window-open and spot with the same oracle stream used for resolution.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from app.config.settings import settings
from app.integrations.crypto_price_feed import CryptoPriceFeed
from app.integrations.polymarket_client import MarketSnapshot, PolymarketClient
from app.services.max_strategy import MaxSignal, evaluate
from app.services.ta_confluence import CandleCache
from app.utils.logger import get_logger
from app.utils.time import utcnow


if TYPE_CHECKING:
    from app.integrations.polymarket_chainlink_feed import PolymarketChainlinkBTC


logger = get_logger(__name__)


WINDOW_SECONDS = 300


@dataclass
class SnipeWindow:
    """One scheduled 5-minute window."""

    window_ts: int  # Unix seconds aligned to 300
    close_at: datetime
    open_price: Optional[float] = None
    market: Optional[MarketSnapshot] = None
    best_signal: Optional[MaxSignal] = None
    best_ts: float = 0.0
    fired: bool = False
    deadline_forced: bool = False
    tick_buffer: deque[float] = field(default_factory=lambda: deque(maxlen=200))


OnSnipe = Callable[[SnipeWindow, MaxSignal], Awaitable[None]]


def _aligned_window_ts(now: Optional[datetime] = None) -> int:
    n = (now or utcnow()).replace(tzinfo=timezone.utc)
    epoch = int(n.timestamp())
    return epoch - (epoch % WINDOW_SECONDS)


def _slug_candidates(window_ts: int) -> list[str]:
    """Return slug variants to try for ``window_ts``."""
    raw = (settings.max_slug_templates or "").strip()
    if not raw:
        templates: list[str] = [
            "btc-updown-5m-{ts}",
            "bitcoin-up-or-down-5m-{ts}",
        ]
    else:
        templates = [t.strip() for t in raw.split(",") if t.strip()]
    return [t.format(ts=window_ts) for t in templates]


class MaxSniper:
    """Schedule and execute the per-window snipe coroutines."""

    def __init__(
        self,
        *,
        polymarket: PolymarketClient,
        feed: CryptoPriceFeed,
        candles: CandleCache,
        on_snipe: OnSnipe,
        oracle: Optional["PolymarketChainlinkBTC"] = None,
    ) -> None:
        self._poly = polymarket
        self._feed = feed
        self._candles = candles
        self._on_snipe = on_snipe
        self._oracle = oracle
        self._stop = asyncio.Event()
        self._scheduled: dict[int, asyncio.Task] = {}

    def stop(self) -> None:
        self._stop.set()
        for task in self._scheduled.values():
            if not task.done():
                task.cancel()

    def _oracle_on(self) -> bool:
        return bool(self._oracle and settings.max_chainlink_oracle_enabled)

    async def run(self) -> None:
        logger.info("max_sniper_started")
        while not self._stop.is_set():
            try:
                await self._heartbeat()
            except Exception as exc:  # noqa: BLE001
                logger.exception("max_sniper_heartbeat_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
        logger.info("max_sniper_stopped")

    async def _heartbeat(self) -> None:
        """Schedule the next 1–2 windows so we always have one armed."""
        now = utcnow()
        current_ts = _aligned_window_ts(now)
        for ts in (current_ts, current_ts + WINDOW_SECONDS):
            close_at = datetime.fromtimestamp(ts + WINDOW_SECONDS, tz=timezone.utc)
            seconds_to_close = (close_at - now).total_seconds()
            if seconds_to_close <= settings.max_snipe_lookahead_seconds + 5:
                continue
            if ts in self._scheduled and not self._scheduled[ts].done():
                continue
            window = SnipeWindow(window_ts=ts, close_at=close_at)
            task = asyncio.create_task(self._snipe_window(window), name=f"max_snipe_{ts}")
            self._scheduled[ts] = task
        for ts in list(self._scheduled.keys()):
            t = self._scheduled[ts]
            if t.done():
                self._scheduled.pop(ts, None)

    async def _snipe_window(self, window: SnipeWindow) -> None:
        try:
            window.market = await self._resolve_market(window.window_ts)
            if window.market is None:
                logger.info(
                    "max_window_no_market",
                    window_ts=window.window_ts,
                    slugs=_slug_candidates(window.window_ts),
                )
                return

            await self._capture_window_open(window)

            lookahead = max(2, int(settings.max_snipe_lookahead_seconds))
            now = utcnow()
            sleep_for = (window.close_at - now).total_seconds() - lookahead
            if sleep_for > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                    return
                except asyncio.TimeoutError:
                    pass

            await self._poll_loop(window)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "max_snipe_window_error",
                window_ts=window.window_ts,
                error=str(exc),
            )

    async def _resolve_market(self, window_ts: int) -> Optional[MarketSnapshot]:
        for slug in _slug_candidates(window_ts):
            try:
                from_event = await self._poly.fetch_markets_for_event_slug(slug)
            except Exception as exc:  # noqa: BLE001
                logger.warning("max_resolve_events_failed", slug=slug, error=str(exc))
                from_event = []
            for m in from_event:
                if (m.slug or "").lower() == slug.lower():
                    return m
            if from_event:
                return from_event[0]

            try:
                hits = await self._poly.search_markets(slug, limit=5)
            except Exception as exc:  # noqa: BLE001
                logger.warning("max_resolve_search_failed", slug=slug, error=str(exc))
                continue
            for m in hits:
                if (m.slug or "").lower() == slug.lower():
                    return m
            if hits:
                return hits[0]
        return None

    async def _capture_window_open(self, window: SnipeWindow) -> None:
        """Prefer Chainlink oracle open; fallback to median CEX feed."""
        deadline = time.monotonic() + max(2.0, float(settings.max_open_capture_timeout_s))
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._oracle_on():
                oo = self._oracle.window_open_usd(window.window_ts)  # type: ignore[union-attr]
                if oo is not None:
                    window.open_price = float(oo)
                    return
            snap = self._feed.snapshot()
            if snap.is_fresh and snap.is_warm and snap.spot is not None:
                window.open_price = float(snap.spot)
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
                return
            except asyncio.TimeoutError:
                continue

    def _touch_open_from_oracle(self, window: SnipeWindow) -> None:
        if not self._oracle_on():
            return
        oo = self._oracle.window_open_usd(window.window_ts)  # type: ignore[union-attr]
        if oo is not None:
            window.open_price = float(oo)

    def _tick_and_spot(
        self, window: SnipeWindow, snap
    ) -> tuple[Optional[float], Optional[float]]:
        """Return (tick_sample, spot_for_eval)."""
        self._touch_open_from_oracle(window)

        oracle_spot = (
            float(self._oracle.latest_if_live())  # type: ignore[union-attr]
            if self._oracle_on()
            else None
        )

        cex_ok = snap.is_fresh and snap.spot is not None
        cex_spot = float(snap.spot) if cex_ok else None

        if oracle_spot is not None:
            return oracle_spot, oracle_spot
        tick = cex_spot
        spot = cex_spot
        return tick, spot

    async def _poll_loop(self, window: SnipeWindow) -> None:
        """Run the T-10s → T-5s polling loop and fire when ready."""
        deadline = window.close_at.timestamp() - max(
            1,
            int(settings.max_snipe_deadline_seconds),
        )
        min_conf = float(settings.max_min_confidence)
        weak_floor = float(settings.max_weak_confidence_floor)
        spike_threshold = float(settings.max_snipe_spike_threshold)
        spike_min_d = float(settings.max_spike_min_delta_abs_pct)
        early_delta = float(settings.max_early_delta_abs_pct)
        flat_skip = float(settings.max_flat_deadline_skip_abs_pct)
        deadline_edge = float(settings.max_deadline_delta_abs_pct)

        prev_score: Optional[float] = None

        while not self._stop.is_set():
            now_ts = time.time()
            if now_ts >= deadline:
                break

            snap = self._feed.snapshot()
            tick_s, spot = self._tick_and_spot(window, snap)
            if tick_s is not None:
                window.tick_buffer.append(tick_s)

            if (
                window.open_price is None
                and snap.is_fresh
                and snap.is_warm
                and snap.spot is not None
            ):
                window.open_price = float(snap.spot)

            if window.open_price is None or spot is None:
                await asyncio.sleep(min(2.0, max(0.5, deadline - now_ts)))
                continue

            candles = await self._candles.get()
            sig = evaluate(
                window_open=float(window.open_price),
                spot=float(spot),
                candles=candles,
                tick_prices=list(window.tick_buffer),
            )
            if window.best_signal is None or abs(sig.score) > abs(window.best_signal.score):
                window.best_signal = sig
                window.best_ts = now_ts

            ad = abs(sig.window_delta_pct)
            spike_ok = prev_score is not None and abs(sig.score - prev_score) >= spike_threshold
            spike = spike_ok and (
                sig.confidence >= min_conf
                or (
                    sig.confidence >= weak_floor
                    and ad >= spike_min_d
                )
            )
            prev_score = sig.score

            fire_early = (
                sig.confidence >= min_conf
                or (sig.confidence >= weak_floor and ad >= early_delta)
                or spike
            )

            if fire_early:
                await self._fire(window, sig, deadline_forced=False)
                return

            await asyncio.sleep(min(2.0, max(0.5, deadline - now_ts)))

        if window.fired:
            return
        bs = window.best_signal
        if bs is None:
            return

        ad_b = abs(bs.window_delta_pct)
        wf = float(settings.max_weak_confidence_floor)

        if bs.confidence < min_conf and ad_b < flat_skip:
            logger.info(
                "max_skip",
                reason="flat_deadline",
                window_ts=window.window_ts,
                confidence=round(bs.confidence, 3),
                window_delta_pct=round(bs.window_delta_pct, 5),
            )
            return

        if bs.confidence < wf and ad_b < deadline_edge:
            logger.info(
                "max_skip",
                reason="deadline_micro_edge",
                window_ts=window.window_ts,
                confidence=round(bs.confidence, 3),
                window_delta_pct=round(bs.window_delta_pct, 5),
            )
            return

        await self._fire(window, bs, deadline_forced=True)

    async def _fire(
        self,
        window: SnipeWindow,
        sig: MaxSignal,
        *,
        deadline_forced: bool,
    ) -> None:
        if window.fired:
            return
        window.fired = True
        window.deadline_forced = deadline_forced
        logger.info(
            "max_snipe_fire",
            window_ts=window.window_ts,
            slug=window.market.slug if window.market else None,
            side=sig.side,
            score=round(sig.score, 3),
            confidence=round(sig.confidence, 3),
            window_delta_pct=round(sig.window_delta_pct, 4),
            deadline_forced=deadline_forced,
            seconds_left=int((window.close_at - utcnow()).total_seconds()),
        )
        try:
            await self._on_snipe(window, sig)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "max_snipe_handler_error",
                window_ts=window.window_ts,
                error=str(exc),
            )


__all__ = ["MaxSniper", "SnipeWindow", "OnSnipe"]
