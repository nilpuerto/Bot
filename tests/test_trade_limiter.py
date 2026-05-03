"""Trade-limiter unit tests.

We stub ``TradesRepository`` + ``session_scope`` so these tests run without
a live database.  The limiter is our last-line-of-defence against spam
trading, so each rule gets its own assertion.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.config.settings import settings as app_settings
from app.services import trade_limiter as limiter_mod
from app.services.trade_limiter import TradeLimiter
from app.utils.time import utcnow


class _FakeRepo:
    def __init__(
        self,
        *,
        today_count: int = 0,
        last_trade_at=None,
        last_trade_at_non_crypto=None,
        has_dup: bool = False,
        open_trades: list | None = None,
        open_count: int = 0,
        crypto_open_count: int = 0,
        crypto_open_trades: list | None = None,
        last_close_on_market=None,
    ) -> None:
        self.today_count = today_count
        self.last_trade_at = last_trade_at
        self.last_trade_at_non_crypto = last_trade_at_non_crypto
        self.has_dup = has_dup
        self.open_trades = open_trades or []
        self.open_count = open_count
        self.crypto_open_count = crypto_open_count
        self.crypto_open_trades = crypto_open_trades or []
        self.last_close_on_market = last_close_on_market

    async def get_today_count(self, user_id): return self.today_count
    async def get_last_trade_at(self, user_id): return self.last_trade_at
    async def get_last_trade_at_non_crypto(self, user_id): return (
        self.last_trade_at_non_crypto
        if self.last_trade_at_non_crypto is not None
        else self.last_trade_at
    )
    async def has_open_on_market(self, user_id, market_id): return self.has_dup
    async def list_open(self, user_id): return self.open_trades
    async def list_open_non_crypto(self, user_id): return self.open_trades
    async def list_open_crypto(self, user_id): return self.crypto_open_trades
    async def count_open(self, user_id): return self.open_count
    async def count_open_non_crypto(self, user_id): return self.open_count
    async def count_open_crypto(self, user_id): return self.crypto_open_count
    async def bump_daily_counter(self, user_id, day=None): return None
    async def get_last_close_on_market(self, user_id, market_id):
        return self.last_close_on_market


@asynccontextmanager
async def _fake_session_scope():
    yield None  # session argument is ignored by the fake repo


def _install_fakes(monkeypatch: Any, fake_repo: _FakeRepo) -> None:
    monkeypatch.setattr(limiter_mod, "session_scope", _fake_session_scope)
    monkeypatch.setattr(limiter_mod, "TradesRepository", lambda session: fake_repo)


def _user(max_per_day: int = 4):
    return SimpleNamespace(id=1, max_trades_per_day=max_per_day)


@pytest.mark.asyncio
async def test_daily_limit(monkeypatch):
    monkeypatch.setattr(app_settings, "max_trades_per_day", 4)
    _install_fakes(monkeypatch, _FakeRepo(today_count=4))
    limiter = TradeLimiter(cooldown_seconds=0, max_open_trades=5)
    res = await limiter.check(
        user=_user(4), market_id="m1", market_question="Q1", is_crypto=False
    )
    assert res.allowed is False and res.reason == "daily_limit_reached"


@pytest.mark.asyncio
async def test_daily_limit_env_can_exceed_legacy_user_column(monkeypatch):
    """If .env raises MAX_TRADES_PER_DAY above the stale DB column, honour the ceiling."""
    monkeypatch.setattr(app_settings, "max_trades_per_day", 15)
    _install_fakes(monkeypatch, _FakeRepo(today_count=4))
    limiter = TradeLimiter(cooldown_seconds=0, max_open_trades=20)
    res = await limiter.check(
        user=_user(4), market_id="m1", market_question="Q1", is_crypto=False
    )
    assert res.allowed is True


@pytest.mark.asyncio
async def test_cooldown(monkeypatch):
    _install_fakes(monkeypatch, _FakeRepo(last_trade_at=utcnow() - timedelta(seconds=5)))
    limiter = TradeLimiter(cooldown_seconds=600, max_open_trades=5)
    res = await limiter.check(
        user=_user(), market_id="m1", market_question="Q1", is_crypto=False
    )
    assert res.allowed is False and res.reason.startswith("cooldown_active")


@pytest.mark.asyncio
async def test_duplicate_market(monkeypatch):
    _install_fakes(monkeypatch, _FakeRepo(has_dup=True))
    limiter = TradeLimiter(cooldown_seconds=0, max_open_trades=5)
    res = await limiter.check(
        user=_user(), market_id="m1", market_question="Q1", is_crypto=False
    )
    assert res.allowed is False and res.reason == "duplicate_market"


@pytest.mark.asyncio
async def test_similar_market(monkeypatch):
    open_trade = SimpleNamespace(market_question="Trump wins presidential election 2028")
    _install_fakes(
        monkeypatch,
        _FakeRepo(open_trades=[open_trade]),
    )
    limiter = TradeLimiter(cooldown_seconds=0, max_open_trades=5)
    res = await limiter.check(
        user=_user(),
        market_id="m2",
        market_question="Trump wins presidential election 2028",
        is_crypto=False,
    )
    assert res.allowed is False and res.reason == "similar_open_trade"


@pytest.mark.asyncio
async def test_max_concurrent(monkeypatch):
    _install_fakes(monkeypatch, _FakeRepo(open_count=5))
    limiter = TradeLimiter(cooldown_seconds=0, max_open_trades=5)
    res = await limiter.check(
        user=_user(), market_id="m1", market_question="Q1", is_crypto=False
    )
    assert res.allowed is False and res.reason == "max_open_trades_reached"


@pytest.mark.asyncio
async def test_happy_path(monkeypatch):
    _install_fakes(monkeypatch, _FakeRepo())
    limiter = TradeLimiter(
        cooldown_seconds=0, max_open_trades=5, post_close_reentry_seconds=0
    )
    res = await limiter.check(
        user=_user(), market_id="m1", market_question="Q1", is_crypto=False
    )
    assert res.allowed is True


@pytest.mark.asyncio
async def test_reentry_cooldown_blocks_fresh_close(monkeypatch):
    """Closing a trade on market X should lock out re-entry on X for
    ``post_close_reentry_seconds`` — anti-whipsaw guard."""
    recent_close = utcnow() - timedelta(seconds=120)
    _install_fakes(monkeypatch, _FakeRepo(last_close_on_market=recent_close))
    limiter = TradeLimiter(
        cooldown_seconds=0,
        max_open_trades=5,
        post_close_reentry_seconds=1800,
    )
    res = await limiter.check(
        user=_user(), market_id="m1", market_question="Q1", is_crypto=False
    )
    assert res.allowed is False
    assert res.reason.startswith("reentry_cooldown_active")


@pytest.mark.asyncio
async def test_reentry_cooldown_expires(monkeypatch):
    """Once enough time has passed, re-entry is allowed again."""
    old_close = utcnow() - timedelta(seconds=4000)
    _install_fakes(monkeypatch, _FakeRepo(last_close_on_market=old_close))
    limiter = TradeLimiter(
        cooldown_seconds=0,
        max_open_trades=5,
        post_close_reentry_seconds=1800,
    )
    res = await limiter.check(
        user=_user(), market_id="m1", market_question="Q1", is_crypto=False
    )
    assert res.allowed is True


@pytest.mark.asyncio
async def test_reentry_cooldown_can_be_disabled(monkeypatch):
    """Setting ``post_close_reentry_seconds`` to 0 disables the guard."""
    recent_close = utcnow() - timedelta(seconds=60)
    _install_fakes(monkeypatch, _FakeRepo(last_close_on_market=recent_close))
    limiter = TradeLimiter(
        cooldown_seconds=0,
        max_open_trades=5,
        post_close_reentry_seconds=0,
    )
    res = await limiter.check(
        user=_user(), market_id="m1", market_question="Q1", is_crypto=False
    )
    assert res.allowed is True


@pytest.mark.asyncio
async def test_crypto_ignores_global_daily(monkeypatch):
    monkeypatch.setattr(app_settings, "max_trades_per_day", 4)
    _install_fakes(monkeypatch, _FakeRepo(today_count=99))
    limiter = TradeLimiter(cooldown_seconds=0, max_open_trades=5)
    res = await limiter.check(
        user=_user(4), market_id="m1", market_question="Q1", is_crypto=True
    )
    assert res.allowed is True


@pytest.mark.asyncio
async def test_crypto_ignores_news_cooldown(monkeypatch):
    _install_fakes(monkeypatch, _FakeRepo(last_trade_at=utcnow()))
    limiter = TradeLimiter(cooldown_seconds=600, max_open_trades=5)
    res = await limiter.check(
        user=_user(), market_id="m1", market_question="Q1", is_crypto=True
    )
    assert res.allowed is True


@pytest.mark.asyncio
async def test_news_max_open_ignores_crypto_positions(monkeypatch):
    _install_fakes(monkeypatch, _FakeRepo(open_count=0, crypto_open_count=5))
    limiter = TradeLimiter(cooldown_seconds=0, max_open_trades=5)
    res = await limiter.check(
        user=_user(), market_id="m1", market_question="Q1", is_crypto=False
    )
    assert res.allowed is True


@pytest.mark.asyncio
async def test_crypto_respects_own_concurrent_cap(monkeypatch):
    monkeypatch.setattr(app_settings, "crypto_max_open_trades", 3)
    _install_fakes(monkeypatch, _FakeRepo(crypto_open_count=3))
    limiter = TradeLimiter(cooldown_seconds=0, max_open_trades=5)
    res = await limiter.check(
        user=_user(), market_id="m1", market_question="Q1", is_crypto=True
    )
    assert res.allowed is False and res.reason == "crypto_max_open_trades_reached"


@pytest.mark.asyncio
async def test_news_cooldown_uses_only_non_crypto_timestamps(monkeypatch):
    _install_fakes(
        monkeypatch,
        _FakeRepo(
            last_trade_at=utcnow() - timedelta(seconds=3),
            last_trade_at_non_crypto=utcnow() - timedelta(days=1),
        ),
    )
    limiter = TradeLimiter(cooldown_seconds=600, max_open_trades=5)
    res = await limiter.check(
        user=_user(), market_id="m1", market_question="Q1", is_crypto=False
    )
    assert res.allowed is True
