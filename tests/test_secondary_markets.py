"""Secondary-markets scout — heuristic helpers."""
from __future__ import annotations

from app.integrations.polymarket_client import MarketSnapshot
from app.services.secondary_markets import _is_low_attention, _looks_binary


def _m(volume: float = 10_000.0, outcomes: int = 2) -> MarketSnapshot:
    return MarketSnapshot(
        id="m",
        slug="m",
        question="Will it rain?",
        outcomes=[f"o{i}" for i in range(outcomes)],
        outcome_prices=[1.0 / outcomes] * outcomes,
        volume_24h=volume,
        liquidity=1_000,
        best_yes_price=0.5,
        best_no_price=0.5,
    )


def test_low_attention_triggers_under_fifty_k() -> None:
    assert _is_low_attention(_m(10_000)) is True
    assert _is_low_attention(_m(200_000)) is False


def test_binary_filter_rejects_multi_outcome() -> None:
    assert _looks_binary(_m(outcomes=2)) is True
    assert _looks_binary(_m(outcomes=5)) is False
