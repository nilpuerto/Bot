"""Market matching — turn an AI market hint into a concrete Polymarket market.

v3 (universe-first) — ranking blends four signals:

* **Universe pre-match** — every few minutes the bot caches the top-N
  active Polymarket markets in :class:`MarketUniverseService`.  The
  matcher tries this in-memory ranker first; only if nothing scores
  above ``min_confidence`` does it fall back to the Gamma search API.
  This guarantees the bot only ever returns markets that are actually
  tradeable *right now*.
* **Token overlap** with the AI hint + news title (Jaccard, base signal).
* **Entity match** — count of AI-extracted entities that appear verbatim
  in the market question.  Each entity match adds a sizeable bonus.
* **Liquidity tie-breaker** — more active markets win when two
  candidates are otherwise tied.

Expired or zero-liquidity markets are never returned.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from app.config.settings import settings
from app.integrations.polymarket_client import MarketSnapshot, PolymarketClient
from app.services.market_universe import MarketUniverseService
from app.services.match_gates import (
    categories_compatible,
    infer_market_topic,
    normalize_entities,
    passes_entity_gate,
)
from app.utils.logger import get_logger
from app.utils.text import normalize


logger = get_logger(__name__)


@dataclass
class MatchResult:
    market: MarketSnapshot
    confidence: float  # 0..1
    entity_hits: int = 0
    via: str = "search"  # 'universe' | 'search'


class MarketMatchingService:
    def __init__(
        self,
        polymarket: PolymarketClient,
        min_confidence: Optional[float] = None,
        search_limit: int = 12,
        universe: Optional[MarketUniverseService] = None,
    ) -> None:
        self._poly = polymarket
        # When unspecified, defer to the global setting so operators can
        # tune the floor via env without code changes.
        self._min_confidence = (
            float(min_confidence)
            if min_confidence is not None
            else float(settings.match_min_confidence)
        )
        self._search_limit = search_limit
        self._universe = universe

    async def find(
        self,
        *,
        ai_market_hint: Optional[str],
        news_title: str,
        entities: Optional[Iterable[str]] = None,
        category: Optional[str] = None,
    ) -> Optional[MatchResult]:
        # 0. Universe pre-match — fast in-memory ranker against the
        # currently-active Polymarket catalogue.  Wins outright when it
        # clears the confidence floor; otherwise we still fall through
        # to the Gamma search below so an obscure but tradeable market
        # not in the top-N can still be picked up.
        if self._universe is not None and self._universe.size > 0:
            ent_list = list(entities) if entities else []
            uni_match = self._universe.find_match(
                ai_market_hint=ai_market_hint,
                news_title=news_title,
                entities=ent_list,
                category=category,
                min_score=self._min_confidence,
            )
            if uni_match is not None:
                logger.info(
                    "market_matched",
                    via="universe",
                    market_id=uni_match.market.id,
                    question=uni_match.market.question,
                    confidence=round(uni_match.score, 3),
                    entity_hits=uni_match.entity_hits,
                )
                return MatchResult(
                    market=uni_match.market,
                    confidence=min(1.0, uni_match.score),
                    entity_hits=uni_match.entity_hits,
                    via="universe",
                )

        query = ai_market_hint or news_title
        if not query:
            return None

        candidates = await self._poly.search_markets(query, limit=self._search_limit)
        if not candidates:
            if ai_market_hint and ai_market_hint.strip() != news_title.strip():
                candidates = await self._poly.search_markets(
                    news_title, limit=self._search_limit
                )
        # Also try entity-first search when still empty — sometimes the
        # AI hint is prose-y ("Fed meeting") while the market title names
        # the entity directly ("Powell").
        if not candidates and entities:
            for ent in list(entities)[:3]:
                if not ent or len(ent) < 3:
                    continue
                found = await self._poly.search_markets(ent, limit=self._search_limit)
                if found:
                    candidates = found
                    break
        if not candidates:
            return None

        ref_tokens = _tokens(f"{ai_market_hint or ''} {news_title}")
        ent_norms = normalize_entities(entities)
        has_entities = bool(ent_norms)

        require_ent = bool(settings.match_require_entity_hit)
        no_ent_jaccard = float(settings.match_no_entity_jaccard_min)
        topic_gate = bool(settings.match_enforce_topic_gate)

        ranked: list[tuple[float, int, MarketSnapshot]] = []
        gated_out = 0
        for market in candidates:
            market_norm = normalize(market.question)
            market_tokens = _tokens(market.question)
            jaccard = _jaccard(ref_tokens, market_tokens)
            entity_hits = sum(1 for e in ent_norms if e in market_norm)

            # Hard gate 1 — topic compatibility (sports news must not
            # route to crypto markets etc.).
            if topic_gate and category:
                market_topic = infer_market_topic(market.question)
                if market_topic and not categories_compatible(category, market_topic):
                    gated_out += 1
                    continue

            # Hard gate 2 — entity gate (or fallback Jaccard floor when
            # no entities were extracted from the news).
            if not passes_entity_gate(
                entity_hits=entity_hits,
                has_entities=has_entities,
                jaccard=jaccard,
                no_entity_jaccard_min=no_ent_jaccard,
                require_entity_hit=require_ent,
            ):
                gated_out += 1
                continue

            # Legacy stopword sanity (cheap blacklist) on top of the
            # topic gate — kept because it catches obvious nonsense
            # like "iphone" appearing in a "political" candidate that
            # the topic detector classified as "other".
            if category and _looks_like_mismatch(category, market):
                gated_out += 1
                continue

            entity_bonus = min(0.40, 0.15 * entity_hits)
            volume_bonus = 0.05 if market.volume_24h > 1000 else 0.0
            liquidity_bonus = 0.05 if market.liquidity > 5000 else 0.0
            binary_bonus = 0.02 if len(market.outcomes) == 2 else 0.0

            score = jaccard + entity_bonus + volume_bonus + liquidity_bonus + binary_bonus
            ranked.append((score, entity_hits, market))

        if not ranked:
            logger.info(
                "market_match_all_gated",
                query=query,
                category=category,
                candidates=len(candidates),
                gated_out=gated_out,
            )
            return None

        ranked.sort(key=lambda r: (r[0], r[1], r[2].volume_24h), reverse=True)
        best_score, best_entity_hits, best_market = ranked[0]

        if best_score < self._min_confidence:
            logger.info(
                "market_match_low_confidence",
                query=query,
                best_score=round(best_score, 3),
                best_question=best_market.question,
            )
            return None

        logger.info(
            "market_matched",
            via="search",
            query=query,
            market_id=best_market.id,
            question=best_market.question,
            confidence=round(best_score, 3),
            entity_hits=best_entity_hits,
        )
        return MatchResult(
            market=best_market,
            confidence=min(1.0, best_score),
            entity_hits=best_entity_hits,
            via="search",
        )


# --- helpers ---------------------------------------------------------------

_CATEGORY_STOPWORDS: dict[str, set[str]] = {
    # Category → tokens that should rarely appear in a matching market.
    # Empty set = no enforced mismatch (we don't over-filter).
    "economic": {"nba", "nfl", "ufc", "kicker"},
    "political": {"nba", "nfl", "ufc", "iphone"},
    "geopolitical": {"nba", "nfl", "ufc", "oscar", "grammy"},
    "climate": {"nba", "nfl", "ufc", "election", "president"},
    "sports": {"election", "fed", "cpi", "ceasefire", "sanctions"},
    "crypto": {"nba", "nfl", "ufc", "election", "president", "ceasefire"},
    "social": set(),
    "other": set(),
}


def _looks_like_mismatch(category: str, market: MarketSnapshot) -> bool:
    stop = _CATEGORY_STOPWORDS.get(category.lower())
    if not stop:
        return False
    q = normalize(market.question)
    return any(s in q for s in stop)


def _tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if len(t) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union)
