"""Market-universe cache — Polymarket-first signal grounding.

Most "no trade" outcomes in the news pipeline come from a simple
mismatch: the AI hint or the news headline doesn't correspond to any
*actually tradeable* Polymarket market.  Searching Gamma for each
headline burns API calls and frequently misses, because text search
relies on keyword overlap with markets the AI invented (e.g. "Will
Trump be shot?" — that market does not exist).

This service inverts the funnel: every few minutes we fetch the top
``MARKET_UNIVERSE_SIZE`` markets ranked by 24-h volume and keep them
in memory.  Downstream, :class:`MarketMatchingService` prefers an
in-memory match against this universe over an HTTP search.  Two big
wins:

* **Grounded matching** — we only ever return markets that exist and
  have liquidity *right now*.  News headlines that cannot be mapped
  to any active market get dropped fast, no Gamma round trip.
* **Operational simplicity** — the universe is observable
  (``len(svc.markets)``), refreshable on demand and served by a
  single cheap fetch loop instead of N concurrent searches.

The service is intentionally minimal: it owns the cache, exposes
read-only views, and offers a fast in-memory ranker
(:meth:`find_match`) that mirrors :class:`MarketMatchingService`'s
ranking math but skips the HTTP round trip.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable, Optional

from app.config.settings import settings
from app.integrations.polymarket_client import MarketSnapshot, PolymarketClient
from app.utils.logger import get_logger
from app.utils.text import normalize


logger = get_logger(__name__)


@dataclass
class UniverseMatch:
    """One ranked candidate inside the in-memory universe."""

    market: MarketSnapshot
    score: float
    entity_hits: int


def _tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if len(t) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union)


def _norm_entity(entity: str) -> str:
    return normalize(entity).strip()


class MarketUniverseService:
    """Periodically-refreshed cache of the top active Polymarket markets.

    Parameters
    ----------
    polymarket
        Live :class:`PolymarketClient` instance (already entered).
    size
        How many markets to hold (default ``settings.market_universe_size``).
    refresh_seconds
        Background refresh cadence.
    order
        Gamma sort field (default ``"volume24hr"``).
    """

    def __init__(
        self,
        polymarket: PolymarketClient,
        *,
        size: Optional[int] = None,
        refresh_seconds: Optional[int] = None,
        order: str = "volume24hr",
    ) -> None:
        self._poly = polymarket
        self._size = int(size if size is not None else settings.market_universe_size)
        self._refresh_seconds = int(
            refresh_seconds
            if refresh_seconds is not None
            else settings.market_universe_refresh_seconds
        )
        self._order = order
        self._markets: list[MarketSnapshot] = []
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._last_refresh_ts: float = 0.0

    # ---- read-only views ------------------------------------------------

    @property
    def markets(self) -> list[MarketSnapshot]:
        """Snapshot of the cached market list (returns the live list — do
        not mutate; callers iterate read-only).
        """
        return self._markets

    @property
    def size(self) -> int:
        return len(self._markets)

    def top_questions(self, limit: int = 30) -> list[str]:
        """Top-N market questions, useful for grounding an LLM prompt."""
        return [m.question for m in self._markets[: max(0, int(limit))]]

    # ---- lifecycle ------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def refresh(self) -> int:
        """Fetch the latest universe from Gamma and atomically swap.

        Returns the number of markets in the new universe.  On error
        (HTTP failure, empty response) the previous universe is kept.
        """
        async with self._lock:
            try:
                fresh = await self._poly.list_active_markets(
                    limit=self._size, order=self._order
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("market_universe_refresh_error", error=str(exc))
                return len(self._markets)
            # Filter out markets we cannot actually trade from in-memory
            # matching: zero-liquidity rows pollute the ranker without
            # being executable.
            usable = [
                m
                for m in fresh
                if (m.volume_24h or 0) > 0 or (m.liquidity or 0) > 0
            ]
            if not usable:
                logger.warning(
                    "market_universe_empty",
                    fetched=len(fresh),
                )
                return len(self._markets)
            self._markets = usable
            logger.info(
                "market_universe_refreshed",
                size=len(usable),
                fetched=len(fresh),
                top=usable[0].question[:60] if usable else None,
            )
            return len(usable)

    async def run_refresh_loop(self) -> None:
        """Background task: refresh on start then every ``refresh_seconds``."""
        await self.refresh()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._refresh_seconds
                )
            except asyncio.TimeoutError:
                await self.refresh()

    # ---- in-memory matching --------------------------------------------

    def find_match(
        self,
        *,
        ai_market_hint: Optional[str],
        news_title: str,
        entities: Optional[Iterable[str]] = None,
        category: Optional[str] = None,
        min_score: float = 0.20,
    ) -> Optional[UniverseMatch]:
        """Best in-memory match for the given news context.

        Mirrors :class:`MarketMatchingService.find` ranking math but
        runs against the cached universe, so it costs O(|universe|)
        Python operations and zero HTTP calls.

        Returns ``None`` if no candidate clears ``min_score``.
        """
        if not self._markets:
            return None

        ref_tokens = _tokens(f"{ai_market_hint or ''} {news_title}")
        ent_norms = {_norm_entity(e) for e in (entities or []) if e}
        ent_norms.discard("")

        ranked: list[UniverseMatch] = []
        for market in self._markets:
            market_norm = normalize(market.question)
            market_tokens = _tokens(market.question)

            jaccard = _jaccard(ref_tokens, market_tokens)
            entity_hits = sum(1 for e in ent_norms if e and e in market_norm)
            entity_bonus = min(0.40, 0.15 * entity_hits)

            volume_bonus = 0.05 if (market.volume_24h or 0) > 1000 else 0.0
            liquidity_bonus = 0.05 if (market.liquidity or 0) > 5000 else 0.0
            binary_bonus = 0.02 if len(market.outcomes) == 2 else 0.0

            score = (
                jaccard
                + entity_bonus
                + volume_bonus
                + liquidity_bonus
                + binary_bonus
            )

            # Cheap category sanity — reuse the same stopwords table as
            # the API matcher to keep the two ranking paths consistent.
            if category and _looks_like_category_mismatch(category, market_norm):
                continue

            if entity_hits == 0 and jaccard <= 0.0:
                # No textual or entity overlap at all — pure noise.
                continue

            ranked.append(UniverseMatch(market, score, entity_hits))

        if not ranked:
            return None

        ranked.sort(
            key=lambda r: (r.score, r.entity_hits, r.market.volume_24h or 0.0),
            reverse=True,
        )
        best = ranked[0]
        if best.score < min_score:
            return None
        return best


# Mirror of :func:`app.services.market_matching._looks_like_mismatch`,
# duplicated here to keep the universe service self-contained without
# importing the full matching module (which would create a soft cycle
# once the matcher starts importing the universe service).
_CATEGORY_STOPWORDS: dict[str, set[str]] = {
    "economic": {"nba", "nfl", "ufc", "kicker"},
    "political": {"nba", "nfl", "ufc", "iphone"},
    "geopolitical": {"nba", "nfl", "ufc", "oscar", "grammy"},
    "climate": {"nba", "nfl", "ufc", "election", "president"},
    "sports": {"election", "fed", "cpi", "ceasefire", "sanctions"},
    "crypto": {"nba", "nfl", "ufc", "election", "president", "ceasefire"},
    "social": set(),
    "other": set(),
}


def _looks_like_category_mismatch(category: str, market_norm: str) -> bool:
    stop = _CATEGORY_STOPWORDS.get(category.lower())
    if not stop:
        return False
    return any(s in market_norm for s in stop)
