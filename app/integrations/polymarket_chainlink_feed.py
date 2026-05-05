"""Polymarket RTDS — Chainlink BTC/USD for MAX Mode window alignment.

Short-horizon Polymarket crypto binaries resolve against Chainlink prices
streamed from ``wss://ws-live-data.polymarket.com`` (topic
``crypto_prices_chainlink``, symbol ``btc/usd``).

We latch the first tick observed on each 300-second unix boundary into
``window_open_usd``.  The latest Chainlink quote is reused as the poll-loop
spot so ``window_delta`` is not mismatched BN+CB open vs CEX prints.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import websockets
import websockets.exceptions

from app.config.settings import settings
from app.utils.logger import get_logger


logger = get_logger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
_WS_GRID = 300

_SUBSCRIBE_JSON = json.dumps(
    {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": '{"symbol":"btc/usd"}',
            }
        ],
    },
    separators=(",", ":"),
)


class PolymarketChainlinkBTC:
    """Background websocket client for btc/usd via Chainlink on RTDS."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

        self._latest_value: Optional[float] = None
        self._latest_ts_ms: int = 0
        self._received_at_mono: float = 0.0

        self._opens: dict[int, float] = {}

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="rtds_chainlink_btc")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def latest_spot_age_ms(self) -> int:
        if self._latest_value is None:
            return 10**9
        return max(0, int((time.monotonic() - self._received_at_mono) * 1000))

    def is_live(self) -> bool:
        return self._latest_value is not None and self.latest_spot_age_ms() <= max(
            2_000,
            int(settings.max_chainlink_stale_ms),
        )

    def latest_if_live(self) -> Optional[float]:
        if not self.is_live():
            return None
        return float(self._latest_value)

    def window_open_usd(self, window_ts: int) -> Optional[float]:
        return self._opens.get(int(window_ts))

    def _maybe_latch_window_open(self, value: float, oracle_ts_ms: int) -> None:
        oracle_sec = oracle_ts_ms // 1000
        W_start = oracle_sec - (oracle_sec % _WS_GRID)
        if W_start not in self._opens:
            self._opens[W_start] = value
            if len(self._opens) > 500:
                for k in sorted(self._opens.keys())[:-340]:
                    self._opens.pop(k, None)

    def _ingest(self, value: float, oracle_ts_ms: int) -> None:
        self._latest_value = value
        self._latest_ts_ms = oracle_ts_ms
        self._received_at_mono = time.monotonic()
        self._maybe_latch_window_open(value, oracle_ts_ms)

    async def _ping_loop(self, ws: object) -> None:
        iv = max(2.0, float(settings.max_chainlink_ping_interval_s))
        try:
            while not self._stop.is_set():
                await asyncio.sleep(iv)
                try:
                    await ws.send("PING")  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    return
        except asyncio.CancelledError:
            return

    async def _run_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ping_task: Optional[asyncio.Task] = None
            try:
                async with asyncio.timeout(30.0):
                    async with websockets.connect(
                        RTDS_URL,
                        ping_interval=None,
                        close_timeout=5,
                        max_size=10_000_000,
                    ) as ws:
                        await ws.send(_SUBSCRIBE_JSON)
                        logger.info("rtds_chainlink_connected")
                        backoff = 1.0
                        ping_task = asyncio.create_task(
                            self._ping_loop(ws),
                            name="rtds_ping",
                        )

                        while not self._stop.is_set():
                            raw_msg = await ws.recv()
                            if isinstance(raw_msg, bytes):
                                text = raw_msg.decode("utf-8", errors="replace")
                            else:
                                text = str(raw_msg)
                            stripped = text.strip()
                            if not stripped or stripped.upper() == "PONG":
                                continue

                            try:
                                data = json.loads(stripped)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(data, dict):
                                continue
                            if data.get("topic") != "crypto_prices_chainlink":
                                continue

                            payload = data.get("payload")
                            if not isinstance(payload, dict):
                                continue

                            if str(payload.get("symbol", "")).lower() != "btc/usd":
                                continue

                            ts_raw = payload.get("timestamp")
                            val_raw = payload.get("value")

                            ts_ms_int = int(ts_raw or data.get("timestamp") or 0)

                            try:
                                float_val_f = float(val_raw) if val_raw is not None else None
                            except (TypeError, ValueError):
                                float_val_f = None

                            if ts_ms_int <= 0 or float_val_f is None or float_val_f <= 0:
                                continue

                            self._ingest(float(float_val_f), ts_ms_int)

            except asyncio.TimeoutError:
                logger.warning("rtds_chainlink_connect_timeout", backoff_s=backoff)
            except asyncio.CancelledError:
                break
            except (OSError, websockets.exceptions.ConnectionClosed) as exc:
                logger.warning("rtds_chainlink_error", error=str(exc), backoff_s=backoff)
            except Exception as exc:  # noqa: BLE001
                logger.warning("rtds_chainlink_fatal", error=str(exc), backoff_s=backoff)
            finally:
                if ping_task is not None:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=min(backoff, 30.0))
                break
            except asyncio.TimeoutError:
                backoff = min(backoff * 1.8, 30.0)


__all__ = ["PolymarketChainlinkBTC", "RTDS_URL"]
