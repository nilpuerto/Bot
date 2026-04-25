"""Secondary / opportunistic market scout — DEPRECATED.

The edge-first refactor folds the same mispricing logic into the
primary news pipeline (and the wallet-cluster pipeline) via
:class:`~app.services.mispricing.MispricingService` +
:class:`~app.services.execution_cost.ExecutionCostModel`.  The extra
scout added a parallel daily-budget system and an ad-hoc keyword
sweep with no measurable edge gain over the unified pipeline.

This module is kept for backwards compatibility.  It is disabled by
default (``SECONDARY_ENABLED=false``) and scheduled for removal.  If
you flip the flag, the orchestrator will emit a one-shot deprecation
warning and the scout will run but the handler is intentionally a
no-op — see :meth:`app.core.orchestrator.Orchestrator._handle_secondary`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings
from app.integrations.polymarket_client import MarketSnapshot, PolymarketClient
from app.services.microstructure import MicrostructureFeatures, MicrostructureService
from app.services.mispricing import MispricingResult, MispricingService
from app.utils.logger import get_logger
from app.utils.text import normalize


logger = get_logger(__name__)


@dataclass
class SecondarySignalCandidate:
    """A mispricing-driven candidate produced by the scout.

    The orchestrator decides whether it passes strategy gates / daily
    budget; this dataclass is a plain value container.
    """

    market: MarketSnapshot
    side: str  # "yes" or "no"
    mispricing: MispricingResult
    micro: MicrostructureFeatures
    reason: str


class SecondaryMarketsScout:
    def __init__(
        self,
        polymarket: PolymarketClient,
        *,
        keywords: Optional[list[str]] = None,
        interval_seconds: Optional[int] = None,
        search_limit: int = 10,
        max_markets_per_keyword: int = 5,
    ) -> None:
        self._poly = polymarket
        self._keywords = [k.lower() for k in (keywords or settings.secondary_keywords)]
        self._interval = interval_seconds or settings.secondary_scan_interval_seconds
        self._search_limit = search_limit
        self._max_per_kw = max_markets_per_keyword

        self._mispricing = MispricingService()
        self._micro = MicrostructureService(polymarket)
        self._stop = asyncio.Event()

        if settings.secondary_enabled:
            logger.warning(
                "secondary_scout_deprecated",
                note=(
                    "SecondaryMarketsScout is deprecated under the edge-first "
                    "refactor — the unified news/cluster pipeline now covers "
                    "its mispricing use case.  Flip SECONDARY_ENABLED=false "
                    "to silence this warning."
                ),
            )

    def stop(self) -> None:
        self._stop.set()

    # ---- public API ---------------------------------------------------

    async def scout_once(self) -> list[SecondarySignalCandidate]:
        """Run a single sweep and return all qualifying candidates."""
        if not self._keywords:
            return []

        seen: set[str] = set()
        markets: list[MarketSnapshot] = []
        for kw in self._keywords:
            try:
                found = await self._poly.search_markets(kw, limit=self._search_limit)
            except Exception as exc:  # noqa: BLE001
                logger.warning("secondary_search_error", kw=kw, error=str(exc))
                continue
            for m in found[: self._max_per_kw]:
                if m.id in seen:
                    continue
                if not _is_low_attention(m):
                    continue
                if not _looks_binary(m):
                    continue
                seen.add(m.id)
                markets.append(m)

        candidates: list[SecondarySignalCandidate] = []
        for market in markets:
            cand = await self._evaluate_market(market)
            if cand is not None:
                candidates.append(cand)
        return candidates

    async def run(self, emit) -> None:
        """Continuously scout and invoke ``emit(candidate)`` for each hit.

        ``emit`` may be sync or async; exceptions are caught so the loop
        keeps running.
        """
        while not self._stop.is_set():
            if settings.secondary_enabled:
                try:
                    candidates = await self.scout_once()
                    for cand in candidates:
                        try:
                            out = emit(cand)
                            if asyncio.iscoroutine(out):
                                await out
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "secondary_emit_error",
                                error=str(exc),
                                market_id=cand.market.id,
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("secondary_scout_tick_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    # ---- internals ----------------------------------------------------

    async def _evaluate_market(
        self, market: MarketSnapshot
    ) -> Optional[SecondarySignalCandidate]:
        mp = await self._mispricing.compute(market)
        if mp.z is None or mp.abs_z < 2.0:
            return None

        micro = await self._micro.snapshot(market)
        if not micro.has_book:
            return None
        if micro.spread is not None and micro.spread > settings.microstructure_max_spread:
            return None

        # Direction logic: a z >> 0 means the market is trading rich vs
        # its rolling mean → the cheap side to buy is NO.  Symmetrically,
        # z << 0 means unusually cheap YES.
        side = "no" if mp.z > 0 else "yes"
        reason = (
            f"|z|={mp.abs_z:.2f} samples={mp.samples} "
            f"vol_ratio={mp.adj_vol_score:.2f} spread={micro.spread:.4f}"
            if micro.spread is not None
            else f"|z|={mp.abs_z:.2f} samples={mp.samples}"
        )
        logger.info(
            "secondary_candidate",
            market_id=market.id,
            question=market.question[:80],
            side=side,
            z=round(mp.z, 3),
            spread=micro.spread,
        )
        return SecondarySignalCandidate(
            market=market,
            side=side,
            mispricing=mp,
            micro=micro,
            reason=reason,
        )


# ---- heuristics ----------------------------------------------------------

def _is_low_attention(market: MarketSnapshot) -> bool:
    """<= 50k USD 24h volume means the market is under-traded."""
    vol = market.volume_24h or 0.0
    return vol <= 50_000.0


def _looks_binary(market: MarketSnapshot) -> bool:
    return len(market.outcomes) == 2


def keywords_match(market: MarketSnapshot, keywords: list[str]) -> bool:
    q = normalize(market.question)
    return any(k in q for k in keywords)
