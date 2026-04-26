"""``MarketUniverseService`` — refresh, ranking, matcher fallback."""
from __future__ import annotations

from typing import Any

import pytest

from app.integrations.polymarket_client import MarketSnapshot
from app.services.market_matching import MarketMatchingService
from app.services.market_universe import MarketUniverseService


def _m(
    id_: str,
    question: str,
    volume: float = 5_000,
    liquidity: float = 10_000,
) -> MarketSnapshot:
    return MarketSnapshot(
        id=id_,
        slug=id_,
        question=question,
        outcomes=["YES", "NO"],
        outcome_prices=[0.5, 0.5],
        volume_24h=volume,
        liquidity=liquidity,
        best_yes_price=0.5,
        best_no_price=0.5,
    )


class _FakePoly:
    def __init__(self, universe: list[MarketSnapshot], search: list[MarketSnapshot] | None = None):
        self._universe = universe
        self._search = search or []
        self.list_calls = 0
        self.search_calls = 0

    async def list_active_markets(
        self, *, limit: int = 200, order: str = "volume24hr", ascending: bool = False
    ) -> list[MarketSnapshot]:
        self.list_calls += 1
        return list(self._universe[:limit])

    async def search_markets(self, query: str, limit: int = 10) -> list[MarketSnapshot]:
        self.search_calls += 1
        return list(self._search[:limit])


@pytest.mark.asyncio
async def test_refresh_caches_active_markets() -> None:
    poly: Any = _FakePoly(
        [
            _m("a", "Will Bitcoin reach 100k?", volume=20_000),
            _m("b", "Will Trump win 2028?", volume=15_000),
            _m("z", "Zero volume market", volume=0, liquidity=0),
        ]
    )
    svc = MarketUniverseService(poly, size=10, refresh_seconds=60)

    n = await svc.refresh()

    # zero-volume + zero-liquidity rows are filtered out.
    assert n == 2
    assert {m.id for m in svc.markets} == {"a", "b"}
    assert poly.list_calls == 1


@pytest.mark.asyncio
async def test_refresh_keeps_old_universe_on_empty_response() -> None:
    poly: Any = _FakePoly([_m("a", "Will Bitcoin reach 100k?")])
    svc = MarketUniverseService(poly, size=10, refresh_seconds=60)
    await svc.refresh()
    assert svc.size == 1

    poly._universe = []
    n = await svc.refresh()
    assert n == 1
    assert svc.size == 1


@pytest.mark.asyncio
async def test_find_match_prefers_entity_overlap() -> None:
    poly: Any = _FakePoly(
        [
            _m("trump", "Will Trump win the 2028 election?", volume=50_000),
            _m("btc", "Will Bitcoin reach 200k by 2028?", volume=80_000),
            _m("rain", "Will it rain in London tomorrow?", volume=2_000),
        ]
    )
    svc = MarketUniverseService(poly, size=10, refresh_seconds=60)
    await svc.refresh()

    hit = svc.find_match(
        ai_market_hint=None,
        news_title="Trump rallies in swing state ahead of vote",
        entities=["Trump"],
        category="political",
    )
    assert hit is not None
    assert hit.market.id == "trump"
    assert hit.entity_hits >= 1


@pytest.mark.asyncio
async def test_find_match_returns_none_for_irrelevant_news() -> None:
    poly: Any = _FakePoly(
        [
            _m("trump", "Will Trump win the 2028 election?"),
            _m("btc", "Will Bitcoin reach 200k by 2028?"),
        ]
    )
    svc = MarketUniverseService(poly, size=10, refresh_seconds=60)
    await svc.refresh()

    hit = svc.find_match(
        ai_market_hint=None,
        news_title="Local florist wins regional bouquet contest",
        entities=[],
        category="other",
    )
    assert hit is None


@pytest.mark.asyncio
async def test_matcher_uses_universe_before_falling_back_to_search() -> None:
    universe = [
        _m("trump", "Will Trump win the 2028 election?", volume=50_000),
        _m("btc", "Will Bitcoin reach 200k by 2028?", volume=80_000),
    ]
    # Search would return a *different* (sentinel) market — proves the
    # matcher never reached the API path.
    search_only = [_m("zzz", "Sentinel market never returned", volume=999)]
    poly: Any = _FakePoly(universe, search=search_only)

    svc = MarketUniverseService(poly, size=10, refresh_seconds=60)
    await svc.refresh()

    matcher = MarketMatchingService(poly, min_confidence=0.15, universe=svc)
    match = await matcher.find(
        ai_market_hint="Trump 2028 election odds",
        news_title="Trump leads polls in 2028 cycle",
        entities=["Trump"],
        category="political",
    )

    assert match is not None
    assert match.market.id == "trump"
    assert match.via == "universe"
    assert poly.search_calls == 0


@pytest.mark.asyncio
async def test_matcher_falls_back_to_search_when_universe_misses() -> None:
    universe = [_m("rain", "Will it rain in Paris?", volume=2_000)]
    search_results = [_m("nfl-mvp", "Will Mahomes win MVP?", volume=10_000)]
    poly: Any = _FakePoly(universe, search=search_results)

    svc = MarketUniverseService(poly, size=10, refresh_seconds=60)
    await svc.refresh()

    matcher = MarketMatchingService(poly, min_confidence=0.15, universe=svc)
    match = await matcher.find(
        ai_market_hint="Mahomes MVP odds",
        news_title="Mahomes throws 5 TDs in playoff win",
        entities=["Mahomes"],
        category="sports",
    )

    assert match is not None
    assert match.market.id == "nfl-mvp"
    assert match.via == "search"
    assert poly.search_calls >= 1


def test_top_questions_returns_in_order() -> None:
    svc = MarketUniverseService(polymarket=None, size=5, refresh_seconds=60)  # type: ignore[arg-type]
    svc._markets = [
        _m("a", "Question A", volume=100),
        _m("b", "Question B", volume=200),
        _m("c", "Question C", volume=300),
    ]
    qs = svc.top_questions(limit=2)
    assert qs == ["Question A", "Question B"]
