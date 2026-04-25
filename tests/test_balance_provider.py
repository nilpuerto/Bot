"""Tests for :mod:`app.services.balance`.

The provider is the single source of truth for "how much money does the
bot get to deploy?".  Breaking this module silently would either
under-spend (missed EV) or over-spend (fails to fill).  These tests pin
down the three rules that matter:

1.  Live + no cap  ⇒  ``effective == liquid_usdc``.
2.  Live + cap > 0 ⇒  ``effective == min(liquid_usdc, cap)``.
3.  RPC failure    ⇒  fall back to last cached value (or 0), never raise.
4.  TTL            ⇒  repeated calls inside ``ttl_seconds`` hit the cache.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from app.services.balance import LiveBalanceProvider


@pytest.fixture(autouse=True)
def _live_mode(monkeypatch):
    """The provider short-circuits in simulation mode; force live."""
    from app.config.settings import settings

    monkeypatch.setattr(settings, "simulation_mode", False)


def _user(balance: float | None) -> SimpleNamespace:
    return SimpleNamespace(balance=balance, id=1)


async def test_effective_uses_full_usdc_when_no_cap():
    poly = SimpleNamespace(get_usdc_balance=AsyncMock(return_value=Decimal("73.21")))
    provider = LiveBalanceProvider(poly, ttl_seconds=60)

    out = await provider.effective_balance(_user(balance=0))

    assert out.liquid_usdc == Decimal("73.21")
    assert out.configured_cap == Decimal("0")
    assert out.effective == Decimal("73.21")


async def test_effective_caps_at_user_balance():
    poly = SimpleNamespace(get_usdc_balance=AsyncMock(return_value=Decimal("500")))
    provider = LiveBalanceProvider(poly, ttl_seconds=60)

    out = await provider.effective_balance(_user(balance=100))

    assert out.liquid_usdc == Decimal("500")
    assert out.configured_cap == Decimal("100")
    assert out.effective == Decimal("100")


async def test_effective_uses_usdc_when_cap_higher():
    poly = SimpleNamespace(get_usdc_balance=AsyncMock(return_value=Decimal("40")))
    provider = LiveBalanceProvider(poly, ttl_seconds=60)

    out = await provider.effective_balance(_user(balance=100))
    # Cap bigger than actual cash ⇒ cash wins.
    assert out.effective == Decimal("40")


async def test_cache_avoids_repeat_rpc():
    mock = AsyncMock(return_value=Decimal("10"))
    provider = LiveBalanceProvider(SimpleNamespace(get_usdc_balance=mock), ttl_seconds=60)

    await provider.liquid_usdc()
    await provider.liquid_usdc()
    await provider.liquid_usdc()

    assert mock.await_count == 1


async def test_rpc_error_returns_last_cached():
    mock = AsyncMock(side_effect=[Decimal("42"), RuntimeError("rpc down")])
    provider = LiveBalanceProvider(
        SimpleNamespace(get_usdc_balance=mock), ttl_seconds=0
    )

    first = await provider.liquid_usdc()
    second = await provider.liquid_usdc()

    assert first == Decimal("42")
    # RPC blew up on the second call but the provider must stay usable.
    assert second == Decimal("42")


async def test_simulation_mode_short_circuits(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "simulation_mode", True)
    mock = AsyncMock(return_value=Decimal("999"))
    provider = LiveBalanceProvider(SimpleNamespace(get_usdc_balance=mock))

    out = await provider.effective_balance(_user(balance=50))

    assert mock.await_count == 0
    assert out.liquid_usdc == Decimal("0")
    assert out.effective == Decimal("50")  # configured value wins in sim
