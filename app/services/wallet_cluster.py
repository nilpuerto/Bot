"""Wallet-cluster scanner — smart-money follow without copy trading.

Detects when ``>= CLUSTER_MIN_WALLETS`` distinct tracked top wallets
pile into the same ``(market, side)`` within the last
``CLUSTER_WINDOW_HOURS`` with combined size ``>= CLUSTER_MIN_CONVICTION_USD``.

The scanner emits a :class:`ClusterCandidate` for each strong cluster.
Downstream, the orchestrator still runs the normal microstructure +
mispricing + scoring gate before any alert fires — the cluster itself
never executes a trade.  We follow **direction of narrative**, not the
individual trades.

Deduplication: once we emit a ``(market_id, side)`` candidate, we skip
it for ``CLUSTER_DEDUP_TTL_SECONDS`` so the user doesn't get hammered
by the same event reappearing every scan.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional

from app.config.settings import settings
from app.database.repositories.traders_repo import ClusterRow, TradersRepository
from app.database.session import session_scope
from app.integrations.polymarket_client import MarketSnapshot, PolymarketClient
from app.utils.logger import get_logger
from app.utils.ttl_cache import TTLCache


logger = get_logger(__name__)


@dataclass
class ClusterCandidate:
    """A cluster that cleared the wallet-count + conviction thresholds."""

    market: MarketSnapshot
    side: str  # 'yes' / 'no'
    wallet_count: int
    total_conviction_usd: float
    first_observed_at: datetime
    last_observed_at: datetime
    reason: str = "smart_money_cluster"

    @property
    def dedup_key(self) -> str:
        return f"{self.market.id}:{self.side}"


ClusterCallback = Callable[[ClusterCandidate], Awaitable[None]]


class WalletClusterScanner:
    def __init__(
        self,
        polymarket: PolymarketClient,
        *,
        min_wallets: Optional[int] = None,
        window_minutes: Optional[int] = None,
        min_conviction_usd: Optional[float] = None,
        scan_interval_seconds: Optional[int] = None,
        dedup_ttl_seconds: Optional[int] = None,
    ) -> None:
        self._poly = polymarket
        effective_min = min_wallets or settings.cluster_min_wallets
        # When the user curates a small whitelist (TRACKED_WALLETS) the
        # configured min_wallets may exceed the list size and the
        # scanner would never fire.  Clamp automatically so the scanner
        # stays useful — still requires *every* whitelisted wallet to
        # converge on the same side, which is a strong signal.
        whitelist_size = len(settings.tracked_wallets)
        if whitelist_size and effective_min > whitelist_size:
            effective_min = max(1, whitelist_size)
        self._min_wallets = effective_min
        self._window_minutes = (
            window_minutes or settings.cluster_window_hours * 60
        )
        self._min_conviction_usd = (
            min_conviction_usd
            if min_conviction_usd is not None
            else settings.cluster_min_conviction_usd
        )
        self._interval = (
            scan_interval_seconds or settings.cluster_scan_interval_seconds
        )
        self._dedup: TTLCache[bool] = TTLCache(
            ttl_seconds=float(
                dedup_ttl_seconds or settings.cluster_dedup_ttl_seconds
            )
        )
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    # ---- main loop ------------------------------------------------------

    async def run(self, callback: ClusterCallback) -> None:
        """Long-running loop.  Swallows per-cycle errors so the scanner
        keeps trying even if the DB flakes momentarily."""
        if not settings.cluster_enabled:
            logger.info("cluster_scanner_disabled")
            return

        while not self._stop.is_set():
            try:
                candidates = await self.scan()
                for cand in candidates:
                    if self._dedup.get(cand.dedup_key) is not None:
                        continue
                    self._dedup.set(cand.dedup_key, True)
                    try:
                        await callback(cand)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "cluster_callback_error",
                            market_id=cand.market.id,
                            error=str(exc),
                        )
            except Exception as exc:  # noqa: BLE001
                logger.exception("cluster_scan_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    # ---- one-shot scan --------------------------------------------------

    async def scan(self) -> list[ClusterCandidate]:
        """Query the DB once and materialise candidates with live market snapshots."""
        async with session_scope() as session:
            rows = await TradersRepository(session).fetch_recent_clusters(
                window_minutes=self._window_minutes,
                min_wallets=self._min_wallets,
                min_conviction_usd=self._min_conviction_usd,
                limit=settings.cluster_max_candidates_per_scan,
            )
        return await self._resolve_markets(rows)

    async def peek(self, limit: int = 5) -> list[ClusterRow]:
        """Lightweight inspection helper used by ``/scanner``.

        Unlike :meth:`scan`, this returns *all* clusters meeting the
        wallet-count threshold regardless of conviction — useful to see
        early formation — and does NOT resolve market snapshots (so the
        command stays cheap).
        """
        async with session_scope() as session:
            return await TradersRepository(session).fetch_recent_clusters(
                window_minutes=self._window_minutes,
                min_wallets=max(2, self._min_wallets - 1),
                min_conviction_usd=0.0,
                limit=limit,
            )

    # ---- helpers --------------------------------------------------------

    async def _resolve_markets(
        self, rows: list[ClusterRow]
    ) -> list[ClusterCandidate]:
        out: list[ClusterCandidate] = []
        for row in rows:
            market = await self._poly.get_market(row.market_id)
            if market is None:
                logger.debug(
                    "cluster_market_unavailable", market_id=row.market_id
                )
                continue
            out.append(
                ClusterCandidate(
                    market=market,
                    side=row.side,
                    wallet_count=row.wallet_count,
                    total_conviction_usd=row.total_conviction_usd,
                    first_observed_at=row.first_observed_at,
                    last_observed_at=row.last_observed_at,
                )
            )
        return out
