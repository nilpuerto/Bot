"""Token-overlap ranking in ``MarketMatchingService``."""
from __future__ import annotations

from typing import Any

import pytest

from app.integrations.polymarket_client import MarketSnapshot
from app.services.market_matching import MarketMatchingService


class _FakePoly:
    def __init__(self, markets: list[MarketSnapshot]) -> None:
        self._markets = markets

    async def search_markets(self, query: str, limit: int = 10) -> list[MarketSnapshot]:
        return list(self._markets)


def _m(id_: str, question: str, volume: float = 1_000, price: float = 0.2) -> MarketSnapshot:
    return MarketSnapshot(
        id=id_,
        slug=id_,
        question=question,
        outcomes=["YES", "NO"],
        outcome_prices=[price, 1 - price],
        volume_24h=volume,
        liquidity=1_000,
        best_yes_price=price,
        best_no_price=1 - price,
    )


@pytest.mark.asyncio
async def test_picks_best_token_overlap() -> None:
    poly: Any = _FakePoly(
        [
            _m("a", "Will Bitcoin reach 100k?"),
            _m("b", "Will Trump win the 2028 election?"),
            _m("c", "Will it rain in Paris?"),
        ]
    )
    svc = MarketMatchingService(poly, min_confidence=0.1)
    match = await svc.find(
        ai_market_hint="Trump wins election",
        news_title="Trump leads in key swing state election",
        entities=["Trump"],
        category="political",
    )
    assert match is not None
    assert match.market.id == "b"


@pytest.mark.asyncio
async def test_returns_none_below_threshold() -> None:
    poly: Any = _FakePoly([_m("z", "Completely unrelated topic about basketball")])
    svc = MarketMatchingService(poly, min_confidence=0.9)
    match = await svc.find(
        ai_market_hint="war ceasefire announced",
        news_title="war ceasefire",
        entities=["ceasefire"],
        category="geopolitical",
    )
    assert match is None
