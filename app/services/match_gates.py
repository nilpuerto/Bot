"""Hard gates for news → market matching.

The previous matcher trusted Jaccard token overlap + a small entity
bonus and would happily match "Assefa wins the London Marathon" to
"Will USA win the 2026 FIFA World Cup?" because both share the token
``win`` and both volume- and liquidity-weight to a positive score.
That's a guaranteed money-loser: the news has zero causal impact on
the market.

This module centralises three deterministic gates that both
:class:`MarketUniverseService.find_match` and
:class:`MarketMatchingService.find` apply BEFORE any ranking math:

1. **Entity gate** — when the AI extracted entities from the news, at
   least one of them must appear verbatim in the candidate market's
   question.  Without this, "Marathon → World Cup" passes simply
   because both questions contain the word "win".

2. **Topic gate** — every market is classified by keyword scan into a
   coarse topic (sports, crypto, political, economic, geopolitical,
   climate).  The AI's news category must be in the topic's
   compatibility set; otherwise the candidate is rejected even if it
   has high token overlap.

3. **No-entity Jaccard floor** — when the AI gave us no entities (rare
   but possible), we fall back to a stricter Jaccard floor on tokens.

Both ranking paths import the same gates so behaviour is identical
whether a match comes ``via=universe`` or ``via=search``.
"""
from __future__ import annotations

from typing import Iterable, Optional

from app.utils.text import normalize


# Coarse topic detection from a market question.  These lists are
# intentionally generous — false negatives here are worse than false
# positives because a missed topic just disables the gate for that
# candidate (defaulting to "any compatible"), it does not auto-accept.
_MARKET_TOPIC_KEYWORDS: dict[str, set[str]] = {
    "sports": {
        "win",
        "match",
        "final",
        "championship",
        "league",
        "fifa",
        "uefa",
        "nba",
        "nfl",
        "ufc",
        "mma",
        "boxing",
        "tennis",
        "soccer",
        "football",
        "basketball",
        "baseball",
        "hockey",
        "marathon",
        "olympics",
        "olympic",
        "race",
        "team",
        "manager",
        "coach",
        "score",
        "goals",
        "barcelona",
        "madrid",
        "manchester",
        "liverpool",
        "psg",
        "lakers",
        "warriors",
        "bulls",
        "patriots",
        "chiefs",
        "esports",
        "lol",
        "esl",
        "valorant",
        "dota",
        "tournament",
    },
    "crypto": {
        "bitcoin",
        "btc",
        "ethereum",
        "eth",
        "crypto",
        "stablecoin",
        "etf",
        "tether",
        "usdc",
        "binance",
        "coinbase",
        "solana",
        "altcoin",
        "blockchain",
        "defi",
        "nft",
        "doge",
        "shiba",
        "halving",
        "mining",
        "wallet",
        "exchange",
        "dogecoin",
        "ripple",
        "xrp",
        "ada",
        "cardano",
    },
    "political": {
        "election",
        "president",
        "senator",
        "congress",
        "vote",
        "votes",
        "ballot",
        "primary",
        "nominee",
        "republican",
        "democrat",
        "trump",
        "biden",
        "harris",
        "candidate",
        "house",
        "senate",
        "filibuster",
        "impeach",
        "law",
        "bill",
        "scotus",
        "governor",
        "mayor",
        "parliament",
        "minister",
    },
    "economic": {
        "fed",
        "cpi",
        "inflation",
        "unemployment",
        "gdp",
        "recession",
        "rate",
        "jobs",
        "ppi",
        "pce",
        "fomc",
        "powell",
        "yellen",
        "treasury",
        "bond",
        "yield",
        "ecb",
    },
    "geopolitical": {
        "war",
        "ceasefire",
        "invasion",
        "sanctions",
        "embargo",
        "russia",
        "ukraine",
        "israel",
        "palestine",
        "iran",
        "china",
        "taiwan",
        "korea",
        "nato",
        "treaty",
        "missile",
        "airstrike",
    },
    "climate": {
        "temperature",
        "hurricane",
        "earthquake",
        "tornado",
        "flooding",
        "wildfire",
        "drought",
        "weather",
        "storm",
        "tsunami",
        "blizzard",
    },
}

# Which AI-news categories are allowed to match each detected market
# topic.  Reads as: "if the news is X, the market may be in any of
# these topics."  Permissive on related domains (politics ↔ macro ↔
# geopolitical) but strict on the orthogonal ones (sports vs crypto
# never match).
_CATEGORY_COMPAT: dict[str, set[str]] = {
    "sports": {"sports"},
    "crypto": {"crypto"},
    "political": {"political", "geopolitical", "economic"},
    "geopolitical": {"geopolitical", "political"},
    "economic": {"economic", "political"},
    "climate": {"climate"},
    "social": {"social", "political", "other"},
    "other": {"other", "social", "political", "economic", "geopolitical"},
}

_CATEGORY_TO_CLUSTER: dict[str, str] = {
    "economic": "macro",
    "geopolitical": "macro",
    "political": "macro",
    "climate": "macro",
    "sports": "sports",
    "crypto": "crypto_tech",
    "social": "crypto_tech",
    "other": "macro",
}

_TOPIC_TO_CLUSTER: dict[str, str] = {
    "economic": "macro",
    "geopolitical": "macro",
    "political": "macro",
    "climate": "macro",
    "sports": "sports",
    "crypto": "crypto_tech",
}


def infer_market_topic(market_question: str) -> Optional[str]:
    """Best-effort topic guess for a Polymarket question.

    Returns ``None`` when no topic accumulates at least one keyword
    hit — we never *invent* a topic just to apply the gate.
    """
    norm = normalize(market_question)
    tokens = {t for t in norm.split() if t}
    if not tokens:
        return None

    best_topic: Optional[str] = None
    best_hits = 0
    for topic, keywords in _MARKET_TOPIC_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in tokens or kw in norm)
        if hits > best_hits:
            best_topic = topic
            best_hits = hits
    return best_topic


def categories_compatible(
    news_category: Optional[str], market_topic: Optional[str]
) -> bool:
    """Return ``True`` when the news category is allowed for the topic.

    Unknown sides default to *compatible* — we'd rather risk a borderline
    pass than throw away a good signal because the topic detector
    couldn't classify the market.
    """
    if not news_category or not market_topic:
        return True
    nc = news_category.lower()
    mt = market_topic.lower()
    allowed = _CATEGORY_COMPAT.get(nc)
    if not allowed:
        # Unknown news category — be permissive.
        return True
    return mt in allowed


def infer_news_cluster(news_category: Optional[str]) -> Optional[str]:
    if not news_category:
        return None
    return _CATEGORY_TO_CLUSTER.get(news_category.lower())


def infer_market_cluster(market_question: str) -> Optional[str]:
    topic = infer_market_topic(market_question)
    if not topic:
        return None
    return _TOPIC_TO_CLUSTER.get(topic.lower())


def clusters_compatible(news_cluster: Optional[str], market_cluster: Optional[str]) -> bool:
    # Unknowns stay permissive to avoid false negatives.
    if not news_cluster or not market_cluster:
        return True
    return news_cluster == market_cluster


def passes_entity_gate(
    *,
    entity_hits: int,
    has_entities: bool,
    jaccard: float,
    no_entity_jaccard_min: float,
    require_entity_hit: bool,
) -> bool:
    """Hard veto on candidates with zero topical anchor.

    * If the news has entities and the gate is enabled, demand at
      least one entity to appear in the market question.
    * If the news has no entities, fall back to a stricter Jaccard
      floor on tokens — without this we would happily match "Will it
      rain in Paris?" to almost anything.
    """
    if has_entities and require_entity_hit:
        return entity_hits >= 1
    if not has_entities:
        return jaccard >= no_entity_jaccard_min
    return True


def normalize_entities(entities: Optional[Iterable[str]]) -> set[str]:
    return {normalize(e).strip() for e in (entities or []) if e and e.strip()}
