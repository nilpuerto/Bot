"""Wallet-cluster scanner — dedup + Telegram rendering.

Everything here runs without the DB / Polymarket by constructing
``ClusterRow`` / ``ClusterCandidate`` directly.  The tests cover:

* the in-memory dedup TTL (same ``(market, side)`` doesn't trigger twice)
* the ``/scanner`` Markdown output (empty vs populated cases)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.database.repositories.traders_repo import ClusterRow
from app.integrations.polymarket_client import MarketSnapshot
from app.services.wallet_cluster import ClusterCandidate, WalletClusterScanner
from app.telegram.handlers.scanner import render_scanner


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _market(mid: str = "mkt-1") -> MarketSnapshot:
    return MarketSnapshot(
        id=mid,
        slug="will-x-happen",
        question="Will X happen?",
        outcomes=["Yes", "No"],
        outcome_prices=[0.55, 0.45],
        volume_24h=50_000,
        liquidity=20_000,
        best_yes_price=0.55,
        best_no_price=0.45,
    )


def _row(
    mid: str = "mkt-1",
    side: str = "yes",
    wallets: int = 4,
    conviction: float = 12_500.0,
    age_minutes: int = 30,
) -> ClusterRow:
    first = _now() - timedelta(minutes=age_minutes)
    return ClusterRow(
        market_id=mid,
        market_slug="will-x-happen",
        side=side,
        wallet_count=wallets,
        total_conviction_usd=conviction,
        first_observed_at=first,
        last_observed_at=_now(),
    )


# ---- rendering -------------------------------------------------------------


def test_render_scanner_empty() -> None:
    body = render_scanner([])
    assert "No active clusters" in body
    assert "SMART" in body


def test_render_scanner_populated_shows_counts_and_conviction() -> None:
    rows = [
        _row(mid="a", side="yes", wallets=5, conviction=17_500),
        _row(mid="b", side="no", wallets=3, conviction=8_200),
    ]
    body = render_scanner(rows)
    assert "5" in body and "17,500" in body
    assert "3" in body and "8,200" in body
    assert "YES" in body and "NO" in body


def test_render_scanner_markers_passing_vs_watchlist() -> None:
    """Rows that clear ``CLUSTER_MIN_WALLETS`` render with a filled
    bullet; watchlist rows (below threshold) use a hollow bullet."""
    rows = [
        _row(mid="strong", wallets=10),
        _row(mid="weak", wallets=2),
    ]
    body = render_scanner(rows)
    assert body.count("▸") >= 1
    assert body.count("◦") >= 1


# ---- dedup -----------------------------------------------------------------


class _FakePoly:
    """Minimal stand-in for :class:`PolymarketClient` — only exposes
    ``get_market`` because the scanner calls it during candidate
    resolution."""

    def __init__(self, market: MarketSnapshot) -> None:
        self._market = market

    async def get_market(self, market_id: str) -> MarketSnapshot:
        return self._market


def _candidate() -> ClusterCandidate:
    return ClusterCandidate(
        market=_market(),
        side="yes",
        wallet_count=5,
        total_conviction_usd=10_000.0,
        first_observed_at=_now() - timedelta(minutes=20),
        last_observed_at=_now(),
    )


@pytest.mark.asyncio
async def test_dedup_key_is_stable() -> None:
    cand = _candidate()
    assert cand.dedup_key == f"{cand.market.id}:yes"


@pytest.mark.asyncio
async def test_callback_fires_once_per_cluster(monkeypatch) -> None:
    """Second time the scanner sees the same ``(market, side)`` while
    the dedup TTL is still live, the callback must NOT fire again."""
    scanner = WalletClusterScanner(
        _FakePoly(_market()),
        min_wallets=3,
        window_minutes=120,
        min_conviction_usd=0.0,
        scan_interval_seconds=1,
        dedup_ttl_seconds=60,
    )

    cand = _candidate()

    async def fake_scan() -> list[ClusterCandidate]:
        return [cand]

    monkeypatch.setattr(scanner, "scan", fake_scan)

    calls: list[str] = []

    async def cb(c: ClusterCandidate) -> None:
        calls.append(c.dedup_key)

    task = asyncio.create_task(scanner.run(cb))
    try:
        await asyncio.sleep(0.1)
        scanner.stop()
        await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()

    assert calls.count(cand.dedup_key) == 1
