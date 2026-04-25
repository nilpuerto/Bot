"""Market matching — turn an AI market hint into a concrete Polymarket market.

v2 ranking blends three signals:

* **Token overlap** with the AI hint + news title (Jaccard, base signal).
* **Entity match** — count of AI-extracted entities that appear verbatim
  in the market question.  Each entity match adds a sizeable bonus, on
  the rationale that "Trump wins 2028" + entity `Trump` is a much
  stronger hit than token-level overlap alone.
* **Liquidity tie-breaker** — more active markets win when two
  candidates are otherwise tied.

Expired or zero-liquidity markets are never returned.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from app.integrations.polymarket_client import MarketSnapshot, PolymarketClient
from app.utils.logger import get_logger
from app.utils.text import normalize


logger = get_logger(__name__)


@dataclass
class MatchResult:
    market: MarketSnapshot
    confidence: float  # 0..1
    entity_hits: int = 0


class MarketMatchingService:
    def __init__(
        self,
        polymarket: PolymarketClient,
        min_confidence: float = 0.25,
        search_limit: int = 10,
    ) -> None:
        self._poly = polymarket
        self._min_confidence = min_confidence
        self._search_limit = search_limit

    async def find(
        self,
        *,
        ai_market_hint: Optional[str],
        news_title: str,
        entities: Optional[Iterable[str]] = None,
        category: Optional[str] = None,
    ) -> Optional[MatchResult]:
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
        ent_norms = {_norm_entity(e) for e in (entities or []) if e}
        ent_norms.discard("")
        ranked: list[tuple[float, int, MarketSnapshot]] = []
        for market in candidates:
            market_norm = normalize(market.question)
            market_tokens = _tokens(market.question)
            jaccard = _jaccard(ref_tokens, market_tokens)

            entity_hits = sum(1 for e in ent_norms if e in market_norm)
            entity_bonus = min(0.40, 0.15 * entity_hits)

            volume_bonus = 0.05 if market.volume_24h > 1000 else 0.0
            liquidity_bonus = 0.05 if market.liquidity > 5000 else 0.0
            # Mild preference for binary markets (len(outcomes) == 2).
            binary_bonus = 0.02 if len(market.outcomes) == 2 else 0.0

            score = jaccard + entity_bonus + volume_bonus + liquidity_bonus + binary_bonus
            ranked.append((score, entity_hits, market))

        ranked.sort(key=lambda r: (r[0], r[1], r[2].volume_24h), reverse=True)
        best_score, best_entity_hits, best_market = ranked[0]

        # Category sanity check — if the AI said "economic" but the only
        # match is a sports market, bail out.  We only enforce when a
        # crude mismatch is obvious to avoid over-filtering.
        if category and _looks_like_mismatch(category, best_market):
            logger.info(
                "market_match_category_mismatch",
                category=category,
                question=best_market.question,
            )
            return None

        if best_score < self._min_confidence:
            logger.info(
                "market_match_low_confidence",
                query=query,
                best_score=best_score,
                best_question=best_market.question,
            )
            return None

        logger.info(
            "market_matched",
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
        )


# --- helpers ---------------------------------------------------------------

_CATEGORY_STOPWORDS: dict[str, set[str]] = {
    # Category → tokens that should rarely appear in a matching market.
    "economic": {"nba", "nfl", "ufc", "goal", "champions", "kicker"},
    "political": {"nba", "nfl", "ufc", "tesla", "iphone"},
    "geopolitical": {"nba", "nfl", "ufc", "oscar", "grammy"},
    "climate": {"nba", "nfl", "ufc", "election", "president"},
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


def _norm_entity(entity: str) -> str:
    return normalize(entity).strip()
