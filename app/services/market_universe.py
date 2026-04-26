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
from typing import Awaitable, Callable, Iterable, Optional

from app.config.settings import settings
from app.integrations.polymarket_client import MarketSnapshot, PolymarketClient
from app.services.match_gates import (
    categories_compatible,
    clusters_compatible,
    infer_market_cluster,
    infer_market_topic,
    infer_news_cluster,
    normalize_entities,
    passes_entity_gate,
)
from app.utils.logger import get_logger
from app.utils.text import normalize


logger = get_logger(__name__)


@dataclass
class UniverseMatch:
    """One ranked candidate inside the in-memory universe."""

    market: MarketSnapshot
    score: float
    entity_hits: int
    cluster: str = "unknown"


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
        on_refresh: Optional[
            Callable[[list[MarketSnapshot]], Awaitable[None]]
        ] = None,
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
        self._known_ids: set[str] = set()
        self._last_new_listings: list[MarketSnapshot] = []
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._last_refresh_ts: float = 0.0
        self._on_refresh = on_refresh

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

    def markets_for_cluster(self, cluster: str) -> list[MarketSnapshot]:
        """Return cached markets restricted to one thematic cluster."""
        wanted = (cluster or "").strip().lower()
        if not wanted:
            return list(self._markets)
        out: list[MarketSnapshot] = []
        for m in self._markets:
            m_cluster = infer_market_cluster(m.question)
            if m_cluster == wanted:
                out.append(m)
        return out

    @property
    def last_new_listings(self) -> list[MarketSnapshot]:
        """Markets that appeared in the most recent refresh but were
        not in the previous universe.  Useful for the pending-news
        retry loop to know whether a re-match is even worth attempting.
        """
        return self._last_new_listings

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
            new_ids = {m.id for m in usable}
            new_listings = (
                [m for m in usable if m.id not in self._known_ids]
                if self._known_ids
                else []
            )
            self._markets = usable
            self._last_new_listings = new_listings
            self._known_ids = new_ids
            self._last_refresh_ts = asyncio.get_event_loop().time()
            logger.info(
                "market_universe_refreshed",
                size=len(usable),
                fetched=len(fresh),
                new_listings=len(new_listings),
                top=usable[0].question[:60] if usable else None,
            )
            if new_listings:
                logger.info(
                    "market_universe_new_listings",
                    count=len(new_listings),
                    examples=[m.question[:60] for m in new_listings[:5]],
                )
        # Fire the callback OUTSIDE the lock so subscribers can call
        # back into the universe without deadlocking.
        if self._on_refresh is not None:
            try:
                await self._on_refresh(new_listings)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning("market_universe_on_refresh_error", error=str(exc))
        return len(self._markets)

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
        min_score: Optional[float] = None,
        require_entity_hit: Optional[bool] = None,
        no_entity_jaccard_min: Optional[float] = None,
        enforce_topic_gate: Optional[bool] = None,
    ) -> Optional[UniverseMatch]:
        """Best in-memory match for the given news context.

        Applies three deterministic gates *before* ranking:

        1. **Topic gate** — the candidate market's inferred topic
           (sports/crypto/political/etc.) must be compatible with the
           AI's news category.
        2. **Entity gate** — when the news has entities, at least one
           must appear in the market question; otherwise we fall back
           to a stricter Jaccard floor on tokens.
        3. **Final score floor** — the surviving best must clear
           ``min_score`` (defaults to ``settings.match_min_confidence``).

        Returns ``None`` if no candidate survives all three gates.
        """
        if not self._markets:
            return None

        # Resolve gate parameters from settings when not overridden.
        score_floor = (
            float(min_score)
            if min_score is not None
            else float(settings.match_min_confidence)
        )
        require_ent = (
            bool(require_entity_hit)
            if require_entity_hit is not None
            else bool(settings.match_require_entity_hit)
        )
        no_ent_jaccard = (
            float(no_entity_jaccard_min)
            if no_entity_jaccard_min is not None
            else float(settings.match_no_entity_jaccard_min)
        )
        topic_gate = (
            bool(enforce_topic_gate)
            if enforce_topic_gate is not None
            else bool(settings.match_enforce_topic_gate)
        )
        cluster_gate = bool(settings.match_enforce_cluster_gate)
        news_cluster = infer_news_cluster(category)

        ref_tokens = _tokens(f"{ai_market_hint or ''} {news_title}")
        ent_norms = normalize_entities(entities)
        has_entities = bool(ent_norms)
        scoped_markets = self._markets
        if news_cluster:
            scoped_markets = self.markets_for_cluster(news_cluster)
            if not scoped_markets:
                scoped_markets = self._markets

        ranked: list[UniverseMatch] = []
        for market in scoped_markets:
            market_norm = normalize(market.question)
            market_tokens = _tokens(market.question)

            jaccard = _jaccard(ref_tokens, market_tokens)
            entity_hits = sum(1 for e in ent_norms if e and e in market_norm)

            # Hard gate 1: topic compatibility (e.g. don't route a
            # sports headline to a crypto market).  When the topic
            # detector can't classify the market, this gate no-ops.
            if topic_gate and category:
                market_topic = infer_market_topic(market.question)
                if market_topic and not categories_compatible(category, market_topic):
                    continue
            market_cluster = infer_market_cluster(market.question)
            if cluster_gate and news_cluster:
                if market_cluster and not clusters_compatible(news_cluster, market_cluster):
                    continue

            # Hard gate 2: entity gate.  When the AI gave us entities,
            # at least one must appear in the question; otherwise fall
            # back to a stricter token-overlap floor.
            if not passes_entity_gate(
                entity_hits=entity_hits,
                has_entities=has_entities,
                jaccard=jaccard,
                no_entity_jaccard_min=no_ent_jaccard,
                require_entity_hit=require_ent,
            ):
                continue

            cluster_entity_bonus = 0.15
            if news_cluster == "macro":
                cluster_entity_bonus = float(settings.match_entity_bonus_macro)
            elif news_cluster == "sports":
                cluster_entity_bonus = float(settings.match_entity_bonus_sports)
            elif news_cluster == "crypto_tech":
                cluster_entity_bonus = float(settings.match_entity_bonus_crypto)
            entity_bonus = min(0.40, cluster_entity_bonus * entity_hits)
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

            # Legacy cheap-stopword sanity — keeps the older negative
            # signals (e.g. "nba" inside an "economic" candidate) even
            # when the topic detector says they're compatible.
            if category and _looks_like_category_mismatch(category, market_norm):
                continue

            ranked.append(
                UniverseMatch(market, score, entity_hits, market_cluster or "unknown")
            )

        if not ranked:
            return None

        ranked.sort(
            key=lambda r: (r.score, r.entity_hits, r.market.volume_24h or 0.0),
            reverse=True,
        )
        best = ranked[0]
        if best.score < score_floor:
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
